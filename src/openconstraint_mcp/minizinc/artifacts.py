"""MiniZinc-specific artifact layout and file-writer for saved verified models.

The filesystem leaf behind ``core.save_verified_model``. It never runs
MiniZinc and never decides *whether* to save; the verification gate lives in
``core``. Generic save-target policy (validation, manifest I/O, atomic commit)
lives in ``save_target``; this module supplies the MiniZinc-specific
``_write_staged_artifacts`` writer and the thin ``write_verified_model_dir``
wrapper.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..schemas.artifacts import SavedArtifactRole, SavedModelArtifact
from ..schemas.minizinc import CheckResult, SolveResult
from ..schemas.portfolio import (
    PortfolioAttempt,
    PortfolioSolveResult,
)
from ..shared.hashing import path_sha256
from ..shared.save_target import (
    EXPERIMENT_LOG_FILENAME,
    MANIFEST_FILENAME,
    commit_staged_dir,
)
from ..shared.save_target import (
    tool_version as _tool_version,
)

# The fixed artifact layout of a saved verified-model directory. Filenames are
# part of the user-facing contract (stable, never LLM-controlled).
MODEL_FILENAME: str = "model.mzn"
DATA_FILENAME: str = "data.dzn"
CHECKER_FILENAME: str = "checker.mzc.mzn"
PROBLEM_FILENAME: str = "problem.md"
SOLVE_RESULT_FILENAME: str = "solve-result.json"


def _winning_attempt(portfolio_result: PortfolioSolveResult) -> PortfolioAttempt:
    """Return ``portfolio_result``'s winning attempt, narrowing ``winner_index``.

    Both invariants this relies on are already enforced before a
    ``portfolio_result`` reaches this module: ``status == "winner"`` implies
    ``winner_index is not None`` (``PortfolioSolveResult``'s own model
    validator), and ``winner_index`` indexes into ``attempts`` (checked
    eagerly by ``core._validate_portfolio_result_consistency``, run by
    ``save_verified_model`` before the fresh check/solve, let alone this write
    step). This helper only narrows the type for its two call sites below; it
    re-checks nothing.
    """
    winner_index = portfolio_result.winner_index
    assert winner_index is not None
    return portfolio_result.attempts[winner_index]


def _build_experiment_log(portfolio_result: PortfolioSolveResult) -> dict[str, object]:
    """Build the ``experiment-log.json`` content for a ``portfolio_result``.

    Deliberately not a ``model_dump`` passthrough of ``PortfolioSolveResult`` —
    the log is a considered export shape, not an implementation detail of the
    portfolio schema, so it is built explicitly field by field. MiniZinc's
    ``PortfolioSolveResult`` has no top-level ``objective_sense`` or
    per-run-budget field (each attempt row already carries its own
    ``timeout_ms``) and no top-level ``winner_seed`` (read instead off the
    winning attempt).
    """
    winner = _winning_attempt(portfolio_result)
    return {
        "managed_by": "openconstraint-mcp",
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "exploration_type": "minizinc_portfolio",
        "models_sha256": portfolio_result.models_sha256,
        "data_sha256": portfolio_result.data_sha256,
        "checker_sha256": portfolio_result.checker_sha256,
        "solve_controls": {
            "free_search": portfolio_result.solve_controls.free_search,
            "parallel": portfolio_result.solve_controls.parallel,
            "all_solutions": portfolio_result.solve_controls.all_solutions,
            "num_solutions": portfolio_result.solve_controls.num_solutions,
        },
        "selection_policy": portfolio_result.selection_policy,
        "winner_index": portfolio_result.winner_index,
        "winner_seed": winner.seed,
        "winner_solver": winner.solver,
        "winner_model_index": winner.model_index,
        "elapsed_ms": portfolio_result.elapsed_ms,
        "attempts": [
            {
                "index": attempt.index,
                "model_index": attempt.model_index,
                "solver": attempt.solver,
                "seed": attempt.seed,
                "timeout_ms": attempt.timeout_ms,
                "state": attempt.state,
                "job_state": attempt.job_state,
                "result_status": attempt.result_status,
                "checker_status": attempt.checker_status,
                "objective": attempt.objective,
                "elapsed_ms": attempt.elapsed_ms,
                "message": attempt.message,
            }
            for attempt in portfolio_result.attempts
        ],
    }


def _write_staged_artifacts(
    staging: Path,
    *,
    model: str,
    data: str | None,
    checker: str | None,
    problem: str | None,
    check: CheckResult,
    solve: SolveResult,
    solve_controls: dict[str, Any],
    portfolio_result: PortfolioSolveResult | None,
) -> list[SavedModelArtifact]:
    """Write every artifact into ``staging`` and return the saved-file list.

    Text artifacts are written verbatim as UTF-8 and hashed from disk after
    the write. The manifest is written last: it lists every *other* file (it
    cannot hash itself), then joins the returned list as a final entry hashed
    after its own write.
    """
    texts: list[tuple[SavedArtifactRole, str, str]] = [("model", MODEL_FILENAME, model)]
    if data is not None:
        texts.append(("data", DATA_FILENAME, data))
    if checker is not None:
        texts.append(("checker", CHECKER_FILENAME, checker))
    if problem is not None:
        texts.append(("problem", PROBLEM_FILENAME, problem))
    texts.append(
        (
            "solve_result",
            SOLVE_RESULT_FILENAME,
            json.dumps(solve.model_dump(mode="json"), indent=2) + "\n",
        )
    )
    if portfolio_result is not None:
        texts.append(
            (
                "experiment_log",
                EXPERIMENT_LOG_FILENAME,
                json.dumps(_build_experiment_log(portfolio_result), indent=2) + "\n",
            )
        )

    artifacts: list[SavedModelArtifact] = []
    for role, filename, text in texts:
        file_path = staging / filename
        file_path.write_text(text, encoding="utf-8")
        artifacts.append(
            SavedModelArtifact(role=role, path=filename, sha256=path_sha256(file_path))
        )

    verification: dict[str, Any] = {
        "check_status": check.status,
        "solve_status": solve.status,
        "checker_status": solve.checker.status if solve.checker is not None else None,
    }
    if portfolio_result is not None:
        # Compact summary only — the full attempt table lives in
        # experiment-log.json, not duplicated here, so the manifest stays
        # skimmable. No per-state counts: attempt states are a snapshot taken when the
        # race settled, before its losers were cancelled.
        winner = _winning_attempt(portfolio_result)
        verification["experiment_log"] = {
            "exploration_type": "minizinc_portfolio",
            "winner_index": portfolio_result.winner_index,
            "winner_seed": winner.seed,
            "winner_solver": winner.solver,
            "winner_model_index": winner.model_index,
            "attempt_count": len(portfolio_result.attempts),
            "statuses_seen": sorted(
                {
                    attempt.result_status
                    for attempt in portfolio_result.attempts
                    if attempt.result_status is not None
                }
            ),
            "attempt_states_seen": sorted({attempt.state for attempt in portfolio_result.attempts}),
            "selection_policy": portfolio_result.selection_policy,
        }

    manifest = {
        "managed_by": "openconstraint-mcp",
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "backend": "minizinc",
        "solver": solve.solver,
        "solve_controls": solve_controls,
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


def write_verified_model_dir(
    target: Path,
    *,
    model: str,
    data: str | None,
    checker: str | None,
    problem: str | None,
    check: CheckResult,
    solve: SolveResult,
    solve_controls: dict[str, Any],
    overwrite: bool,
    portfolio_result: PortfolioSolveResult | None = None,
) -> tuple[list[SavedModelArtifact], str | None]:
    """Stage, re-gate, and commit a verified-model directory at ``target``.

    ``target`` must already be the resolved path ``validate_save_target``
    returned. Delegates the generic staging/commit to ``commit_staged_dir``
    from ``save_target``; supplies the MiniZinc-specific file writer.
    Returns the saved artifact list and an optional post-commit cleanup warning.

    ``portfolio_result``, when supplied, is copied into ``experiment-log.json``
    (see ``_build_experiment_log``) and summarized in the manifest's
    ``verification.experiment_log``; the caller (``core.save_verified_model``)
    has already validated it is self-consistent with this save and has no say
    over whether the save happens — it is only ever passed here after the
    fresh verification gate has already passed.
    """

    def _writer(staging: Path) -> list[SavedModelArtifact]:
        return _write_staged_artifacts(
            staging,
            model=model,
            data=data,
            checker=checker,
            problem=problem,
            check=check,
            solve=solve,
            solve_controls=solve_controls,
            portfolio_result=portfolio_result,
        )

    return commit_staged_dir(target, overwrite=overwrite, write_files=_writer)
