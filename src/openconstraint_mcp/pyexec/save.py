"""CP-SAT Python artifact writer and save orchestrator.

The filesystem leaf behind ``save_verified_cpsat_python``. Generic save-target
policy (validation, manifest I/O, atomic commit) lives in ``save_target``;
this module supplies the CP-SAT-specific writer and the public function.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..schemas.artifacts import SavedArtifactRole, SavedModelArtifact
from ..schemas.cpsat import (
    CpsatCheckerReport,
    CpsatExpectation,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
    CpsatVerificationLevel,
    SaveVerifiedPythonResult,
)
from ..shared.childproc import ChildProcessTracker
from ..shared.hashing import path_sha256
from ..shared.save_target import (
    EXPERIMENT_LOG_FILENAME,
    MANIFEST_FILENAME,
    commit_staged_dir,
    text_sha256,
    validate_save_target,
)
from ..shared.save_target import tool_version as _tool_version
from .checker import run_checker
from .core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    VERIFIED_STATUSES,
    config_sha256,
    effective_checker_timeout_ms,
    replay_env_scope,
    run_cpsat_python,
    validate_checker_args,
    validate_cpsat_random_seed,
)
from .diagnostics import save_failure_diagnostic
from .env_vars import CPSAT_SEED_ENV_VAR
from .script_path import write_python_source

SCRIPT_FILENAME: str = "model.py"
PROBLEM_FILENAME: str = "problem.txt"
CHECKER_FILENAME: str = "checker.py"
SOLUTION_FILENAME: str = "solution.json"
REPLAY_CONFIG_FILENAME: str = "replay-config.json"


def _expectation_passes(
    run_result: CpsatPythonResult, expectation: CpsatExpectation
) -> tuple[bool, str | None]:
    """Check the objective threshold. Returns (passed, reason_if_failed)."""
    obj = run_result.objective
    if obj is None:
        return False, "expectation requires a numeric objective but the script emitted none"
    threshold = expectation.objective_threshold
    if expectation.objective_sense == "maximize":
        if obj >= threshold:
            return True, None
        return False, f"objective {obj} < threshold {threshold} (maximize)"
    else:
        if obj <= threshold:
            return True, None
        return False, f"objective {obj} > threshold {threshold} (minimize)"


def _failure(
    run_result: CpsatPythonResult,
    *,
    reason: str,
    verification_level: CpsatVerificationLevel,
    reported_passed: bool,
    expectation: CpsatExpectation | None,
    expectation_passed: bool | None,
    checker: CpsatCheckerReport | None,
) -> SaveVerifiedPythonResult:
    return SaveVerifiedPythonResult(
        status=run_result.status,
        target_dir=None,
        reason=reason,
        solution=run_result.solution,
        objective=run_result.objective,
        stdout=run_result.stdout,
        stderr=run_result.stderr,
        timed_out=run_result.timed_out,
        truncated=run_result.truncated,
        duration_ms=run_result.duration_ms,
        verification_level=verification_level,
        reported_passed=reported_passed,
        expectation=expectation,
        expectation_passed=expectation_passed,
        checker=checker,
        diagnostic=save_failure_diagnostic(run_result, checker),
    )


def _validate_experiment_result_consistency(
    experiment_result: CpsatPythonExperimentResult,
    *,
    source: str,
    seed: int | None,
    config: dict[str, Any] | None,
) -> None:
    """Eagerly reject an ``experiment_result`` that cannot describe this save request.

    Unlike a bare ``winner_index`` lookup, this matches CONTENT: it accepts any
    ``experiment_result.attempts`` row that is ``accepted`` and whose
    ``source_sha256``/``seed``/``config_sha256`` all match this save request —
    not only the experiment's own ``winner_index``. This lets a client save a
    different accepted attempt than the one the experiment picked as its
    winner (e.g. a faster feasible attempt it prefers over the incumbent-best
    one), while still refusing to attach provenance for a script this
    experiment never accepted. An attempt that ran from ``script_path``
    (``used_script_path=True``) never qualifies on its own: this save's rerun
    is always inline ``source`` with a fresh temp-dir ``cwd``, so it can
    replay neither the attempt's ``args`` nor its ``cwd``-relative sibling
    data. A request whose matches are ALL ``script_path``-derived is rejected;
    one inline match anywhere in the set is enough to accept, regardless of
    list order. This guards only against *accidental* mismatch
    (wrong script attached, a stale experiment, a seed/config that doesn't
    match any accepted attempt) — it is not, and cannot be, a proof that
    ``experiment_result`` is honest. The save decision itself never reads
    ``experiment_result``; only the fresh re-run below and its gates decide
    whether this save succeeds. ``config`` here is already normalized
    (``{}`` -> ``None``) by the caller.
    """
    if experiment_result.status != "winner":
        raise ValueError(
            "experiment_result.status must be 'winner' to attach an experiment log "
            f"(got {experiment_result.status!r}); a no_winner experiment has nothing "
            "to attach"
        )

    source_hash = text_sha256(source)
    expected_config_sha256 = config_sha256(config)

    source_matches = [a for a in experiment_result.attempts if a.source_sha256 == source_hash]
    if not source_matches:
        # A script_path attempt hashes the file's bytes on disk, so a client pasting
        # that script as `source` misses on any whitespace drift and would otherwise
        # be told it attached the wrong experiment — the wrong cause, since such an
        # attempt is unsaveable even on an exact match (the `full_matches` gate
        # below). Same `all` gate as that one, so a mixed experiment still reports
        # the plain mismatch.
        hint = (
            " (every attempt in this experiment ran via script_path, whose "
            "source_sha256 is the file's bytes on disk — if this IS that script, "
            "note that a script_path attempt is unsaveable regardless, because this "
            "save's rerun is always inline source with a fresh temp-dir cwd)"
            if experiment_result.attempts
            and all(a.used_script_path for a in experiment_result.attempts)
            else ""
        )
        raise ValueError(
            "no attempt in experiment_result has source_sha256 matching the supplied "
            "source: the experiment_result was attached to a different script" + hint
        )

    accepted_matches = [a for a in source_matches if a.accepted]
    if not accepted_matches:
        candidate = source_matches[0]
        raise ValueError(
            f"attempt {candidate.name!r} in experiment_result matches the supplied "
            f"source but was not accepted (status={candidate.status!r}) — the save "
            "must replay an accepted attempt"
        )

    # Every seed/config-matching row, not just the first: a lone `next()` pick
    # would make the script_path check below order-dependent, wrongly rejecting
    # an experiment that happens to list a script_path attempt ahead of an
    # otherwise-identical inline one.
    full_matches = [
        a for a in accepted_matches if a.seed == seed and a.config_sha256 == expected_config_sha256
    ]
    if full_matches:
        if all(a.used_script_path for a in full_matches):
            raise ValueError(
                "every experiment_result attempt matching this save request ran via "
                "script_path, which save_verified_cpsat_python cannot replay (its "
                "rerun is always inline source with a fresh temp-dir cwd) — save "
                "without experiment_result provenance, or re-run this script as an "
                "inline `source` attempt if provenance is needed"
            )
        return

    candidate = accepted_matches[0]
    if candidate.seed != seed:
        raise ValueError(
            f"experiment_result's accepted attempt {candidate.name!r} has seed "
            f"({candidate.seed!r}) that does not match the supplied save seed "
            f"({seed!r})"
        )
    raise ValueError(
        f"experiment_result's accepted attempt {candidate.name!r} has config_sha256 "
        f"({candidate.config_sha256!r}) that does not match the canonical hash of the "
        f"supplied save config ({expected_config_sha256!r}); the save must replay "
        "that attempt's config exactly — this rejects both a save that supplies a "
        "config the attempt ran without, and one that omits a config the attempt used"
    )


def _build_experiment_log(experiment_result: CpsatPythonExperimentResult) -> dict[str, Any]:
    """Build the ``experiment-log.json`` content for a ``CpsatPythonExperimentResult``.

    A provenance SUMMARY, not an archive: every attempt row carries only hashes
    and scalar outcomes — never a full ``config`` object for every attempt.
    The saved attempt's own replay config is persisted separately as
    ``replay-config.json`` (see ``_write_staged_artifacts``).
    """
    return {
        "managed_by": "openconstraint-mcp",
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "exploration_type": "cpsat_python_experiment",
        "objective_sense": experiment_result.objective_sense,
        "selection_policy": experiment_result.selection_policy,
        "winner_index": experiment_result.winner_index,
        "winner_name": experiment_result.winner_name,
        "source_sha256": experiment_result.source_sha256,
        "checker_sha256": experiment_result.checker_sha256,
        "problem_sha256": experiment_result.problem_sha256,
        "elapsed_ms": experiment_result.elapsed_ms,
        "attempts": [
            {
                "index": attempt.index,
                "name": attempt.name,
                "seed": attempt.seed,
                "source_sha256": attempt.source_sha256,
                "config_sha256": attempt.config_sha256,
                "used_script_path": attempt.used_script_path,
                "script_timeout_ms": attempt.script_timeout_ms,
                "status": attempt.status,
                "objective": attempt.objective,
                "accepted": attempt.accepted,
                "checker_status": attempt.checker_status,
                "message": attempt.message,
                "timed_out": attempt.timed_out,
                "truncated": attempt.truncated,
                "duration_ms": attempt.duration_ms,
            }
            for attempt in experiment_result.attempts
        ],
    }


def _write_staged_artifacts(
    staging: Path,
    *,
    source: str,
    problem: str | None,
    checker: str | None,
    run_result: CpsatPythonResult,
    verification_level: CpsatVerificationLevel,
    expectation: CpsatExpectation | None,
    expectation_passed: bool | None,
    checker_report: CpsatCheckerReport | None,
    seed: int | None,
    config: dict[str, Any] | None,
    experiment_result: CpsatPythonExperimentResult | None,
    script_timeout_ms: int,
    checker_timeout_ms: int | None,
) -> list[SavedModelArtifact]:
    texts: list[tuple[SavedArtifactRole, str, str]] = [("model", SCRIPT_FILENAME, source)]
    if problem is not None:
        texts.append(("problem", PROBLEM_FILENAME, problem))
    if checker is not None:
        texts.append(("checker", CHECKER_FILENAME, checker))
    if checker_report is not None:
        texts.append(
            (
                "solution",
                SOLUTION_FILENAME,
                json.dumps(run_result.solution or {}, indent=2) + "\n",
            )
        )
    if config is not None:
        # The saved attempt's own replay config, persisted for auditability and
        # best-effort replay — not every attempt's config (see
        # _build_experiment_log).
        texts.append(("replay_config", REPLAY_CONFIG_FILENAME, json.dumps(config, indent=2) + "\n"))
    if experiment_result is not None:
        texts.append(
            (
                "experiment_log",
                EXPERIMENT_LOG_FILENAME,
                json.dumps(_build_experiment_log(experiment_result), indent=2) + "\n",
            )
        )

    artifacts: list[SavedModelArtifact] = []
    for role, filename, text in texts:
        file_path = staging / filename
        if role in {"model", "checker"}:
            write_python_source(file_path, text)
        else:
            file_path.write_text(text, encoding="utf-8")
        artifacts.append(
            SavedModelArtifact(role=role, path=filename, sha256=path_sha256(file_path))
        )

    verification: dict[str, Any] = {
        "level": verification_level,
        "reported_status": run_result.status,
        "objective": run_result.objective,
        "script_timeout_ms": script_timeout_ms,
    }
    if checker_timeout_ms is not None:
        verification["checker_timeout_ms"] = checker_timeout_ms
    if seed is not None:
        # The saved model.py is byte-for-byte the client's script: the seed lives
        # in the manifest, not the code. A manual re-run without the env var hits the
        # script's own fallback and may not reproduce this incumbent.
        verification["replay_seed"] = seed
        verification["reproducibility_note"] = (
            f"This result was verified with the replay seed above. The saved "
            f"{SCRIPT_FILENAME} carries its own seed fallback, not this seed; to "
            f"reproduce the saved incumbent, set {CPSAT_SEED_ENV_VAR}={seed} before "
            f"running {SCRIPT_FILENAME} by hand."
        )
    if expectation is not None:
        verification["expectation"] = {
            "objective_sense": expectation.objective_sense,
            "objective_threshold": expectation.objective_threshold,
            "passed": expectation_passed,
        }
    if checker_report is not None:
        # Only scalar summary — no stdout/stderr/errors/details to avoid leakage.
        verification["checker"] = {
            "status": checker_report.status,
            "error_count": len(checker_report.errors),
            "duration_ms": checker_report.duration_ms,
            "timed_out": checker_report.timed_out,
            "truncated": checker_report.truncated,
        }
    if config is not None:
        verification["replay_config_sha256"] = config_sha256(config)
    if experiment_result is not None:
        # Compact summary only — the full attempt table lives in
        # experiment-log.json, not duplicated here, so the manifest stays skimmable.
        verification["experiment_log"] = {
            "exploration_type": "cpsat_python_experiment",
            "winner_index": experiment_result.winner_index,
            "winner_name": experiment_result.winner_name,
            "attempt_count": len(experiment_result.attempts),
            "accepted_attempt_count": sum(1 for a in experiment_result.attempts if a.accepted),
            "statuses_seen": sorted({a.status for a in experiment_result.attempts}),
            "selection_policy": experiment_result.selection_policy,
        }

    manifest = {
        "managed_by": "openconstraint-mcp",
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "backend": "cpsat_python",
        "verification": verification,
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
    }
    manifest_path = staging / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    artifacts.append(
        SavedModelArtifact(
            role="manifest", path=MANIFEST_FILENAME, sha256=path_sha256(manifest_path)
        )
    )
    return artifacts


def save_verified_cpsat_python(
    source: str,
    *,
    target_dir: Path | None = None,
    problem: str | None = None,
    expectation: CpsatExpectation | None = None,
    checker: str | None = None,
    checker_timeout_ms: int | None = None,
    script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    overwrite: bool = False,
    verify_only: bool = False,
    seed: int | None = None,
    config: dict[str, Any] | None = None,
    experiment_result: CpsatPythonExperimentResult | None = None,
    tracker: ChildProcessTracker | None = None,
) -> SaveVerifiedPythonResult:
    """Re-run ``source`` and persist it when it passes all supplied save gates.

    Gates run in order (reported → expectation → checker) and short-circuit on
    the first failure. Writing nothing on a non-passing run is guaranteed — the
    commit never starts. Read the returned ``SaveVerifiedPythonResult`` as two
    independent fields: ``saved`` reports PERSISTENCE only, while the verdict is
    ``reason`` (None iff every supplied gate passed) plus the per-gate fields and
    the ``verification_level`` of the highest gate that actually passed.

    ``verify_only=True`` runs the same solver and the same gates in the same
    order, but skips save-target validation and every persistent write:
    ``target_dir`` is then not required, and is ignored along with ``overwrite``
    when supplied. A passing verify-only run returns ``reason=None`` with
    ``saved=False``, ``target_dir=None``, and no ``files``; a failing one returns
    through the ordinary gate-failure path unchanged. With ``verify_only=False``,
    omitting ``target_dir`` raises ``ValueError`` before any child process starts.

    When ``seed`` is supplied, the re-run sets ``OPENCONSTRAINT_MCP_CPSAT_SEED`` so
    a cooperating script replays that seed, and the manifest records it. This is a
    single-run replay aid only: the save gates are UNCHANGED, and the reported gate
    still requires ``optimal``/``feasible``.

    ``config`` is likewise a replay aid: a non-empty dict is written to a temp file
    and its path injected via ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` for the re-run,
    then persisted as ``replay-config.json`` on a successful save (``{}`` is
    normalized to "no config", identically to the experiment executor).

    ``experiment_result`` is PROVENANCE ONLY, never verification evidence — like
    ``portfolio_result`` on the MiniZinc save path. When supplied, it is validated
    eagerly for self-consistency with this request (winner status, matching
    ``source``/``seed``/``config`` hashes on at least one accepted attempt that
    did NOT run from a ``script_path`` — see
    ``_validate_experiment_result_consistency``) but every save decision still
    comes from the fresh run/gates below. On a successful save, its attempt table
    is copied into ``experiment-log.json`` as a provenance summary (hashes and
    scalar outcomes only, never a non-winning attempt's full ``config``).
    """
    validate_checker_args(checker=checker, checker_timeout_ms=checker_timeout_ms)
    if seed is not None:
        seed = validate_cpsat_random_seed(seed)
    normalized_config = config if config else None
    if experiment_result is not None:
        _validate_experiment_result_consistency(
            experiment_result,
            source=source,
            seed=seed,
            config=normalized_config,
        )

    effective_checker_timeout = effective_checker_timeout_ms(
        checker_timeout_ms=checker_timeout_ms,
        default_script_timeout_ms=script_timeout_ms,
    )

    target: Path | None = None
    if not verify_only:
        if target_dir is None:
            raise ValueError(
                "target_dir is required unless verify_only=True (a save must know where "
                "to persist the verified artifacts)"
            )
        target = validate_save_target(target_dir, overwrite=overwrite)
    with replay_env_scope(seed=seed, config=normalized_config) as replay_env:
        run_result = run_cpsat_python(
            source, script_timeout_ms=script_timeout_ms, tracker=tracker, env=replay_env
        )

    # --- Reported gate ---
    reported_passed = run_result.status in VERIFIED_STATUSES and bool(run_result.solution)
    if not reported_passed:
        reason_parts = [f"status={run_result.status!r}"]
        if run_result.status in VERIFIED_STATUSES and not run_result.solution:
            reason_parts.append("solution is missing or empty")
        return _failure(
            run_result,
            reason=f"CP-SAT run did not pass the reported gate: {', '.join(reason_parts)}",
            verification_level="none",
            reported_passed=False,
            expectation=expectation,
            expectation_passed=None,
            checker=None,
        )

    # --- Expectation gate ---
    exp_passed: bool | None = None
    if expectation is not None:
        passed, exp_reason = _expectation_passes(run_result, expectation)
        exp_passed = passed
        if not passed:
            return _failure(
                run_result,
                reason=f"CP-SAT run did not pass the expectation gate: {exp_reason}",
                verification_level="reported",
                reported_passed=True,
                expectation=expectation,
                expectation_passed=False,
                checker=None,
            )

    # --- Checker gate ---
    checker_report: CpsatCheckerReport | None = None
    if checker is not None:
        _report: CpsatCheckerReport = run_checker(
            checker=checker,
            run_result=run_result,
            problem=problem,
            timeout_ms=effective_checker_timeout,
            tracker=tracker,
        )
        if _report.status != "accepted":
            return _failure(
                run_result,
                reason=(f"CP-SAT checker did not accept the solution: status={_report.status!r}"),
                verification_level="expectation" if expectation is not None else "reported",
                reported_passed=True,
                expectation=expectation,
                expectation_passed=exp_passed,
                checker=_report,
            )
        checker_report = _report

    # --- All gates passed: commit, unless this was verify-only ---
    final_level: CpsatVerificationLevel
    if checker is not None:
        final_level = "checked"
    elif expectation is not None:
        final_level = "expectation"
    else:
        final_level = "reported"

    # ``target`` is None exactly when verify_only is set; checking it (not the
    # flag) is what narrows the Path | None for commit_staged_dir, and keeps the
    # whole persistence path — writer included — unbuilt on a verify-only run.
    files: list[SavedModelArtifact] = []
    if target is not None:

        def _writer(staging: Path) -> list[SavedModelArtifact]:
            return _write_staged_artifacts(
                staging,
                source=source,
                problem=problem,
                checker=checker,
                run_result=run_result,
                verification_level=final_level,
                expectation=expectation,
                expectation_passed=exp_passed,
                checker_report=checker_report,
                seed=seed,
                config=normalized_config,
                experiment_result=experiment_result,
                script_timeout_ms=script_timeout_ms,
                checker_timeout_ms=checker_timeout_ms,
            )

        files, _ = commit_staged_dir(target, overwrite=overwrite, write_files=_writer)
    return SaveVerifiedPythonResult(
        status=run_result.status,
        target_dir=str(target) if target is not None else None,
        reason=None,
        solution=run_result.solution,
        objective=run_result.objective,
        stdout=run_result.stdout,
        stderr=run_result.stderr,
        timed_out=run_result.timed_out,
        truncated=run_result.truncated,
        duration_ms=run_result.duration_ms,
        files=files,
        verification_level=final_level,
        reported_passed=True,
        expectation=expectation,
        expectation_passed=exp_passed,
        checker=checker_report,
    )
