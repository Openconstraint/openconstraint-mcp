"""Unit tests for pyexec/core.py — all subprocess calls mocked."""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openconstraint_mcp.pyexec.core import (
    _KEY_PATH_MAX_CHARS,
    _MUTATION_ERRORS_MAX_BYTES,
    _compact_mutation_errors,
    _envelope_violation,
    effective_checker_timeout_ms,
    run_cpsat_python,
    run_cpsat_python_file,
    run_cpsat_python_file_checked,
    seed_config_env,
    validate_checker_args,
    validate_cpsat_random_seed,
)
from openconstraint_mcp.pyexec.core import (
    normalize_objective as _normalize_objective,
)
from openconstraint_mcp.pyexec.diagnostics import (
    checker_report_diagnostic,
    cpsat_result_diagnostic,
)
from openconstraint_mcp.pyexec.eligibility import diagnostic_incumbent_eligibility
from openconstraint_mcp.schemas.cpsat import (
    CPSAT_MUTATION_NAMES,
    CpsatCheckerReport,
    CpsatPythonCheckedResult,
    CpsatPythonResult,
)
from openconstraint_mcp.shared.childrun import (
    MAX_OUTPUT_BYTES,
    ChildExecutionResult,
    ChildSpawnError,
)

_VALID_SOLUTION = {"x": 3, "y": 7}
_VALID_STDOUT = json.dumps({"status": "optimal", "objective": 10, "solution": _VALID_SOLUTION})


def _make_fake_proc(
    *,
    returncode: int = 0,
    stdout_content: str = _VALID_STDOUT,
    stderr_content: str = "",
    timeout: bool = False,
    output_size: int | None = None,
) -> MagicMock:
    """Return a fake Popen handle."""
    fake = MagicMock()
    fake.pid = 1234
    fake.returncode = None if timeout or output_size else returncode

    def _poll() -> int | None:
        return fake.returncode

    fake.poll = _poll

    if timeout:
        fake.wait.return_value = returncode
        fake.returncode = returncode
    elif output_size is not None:
        fake.returncode = returncode
        fake.wait.return_value = returncode
    else:
        fake.wait.return_value = returncode
        fake.returncode = returncode

    return fake


def _run_with_mocked_proc(
    source: str = "print('hi')",
    *,
    stdout_content: str = _VALID_STDOUT,
    stderr_content: str = "",
    returncode: int = 0,
    timeout: bool = False,
    large_output: bool = False,
    script_timeout_ms: int = 5000,
    tracker: Any = None,
) -> CpsatPythonResult:
    """Run run_cpsat_python with all subprocess/proc calls patched."""

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = None  # live

        # Simulate file writes
        stdout_file = kwargs.get("stdout")
        stderr_file = kwargs.get("stderr")

        actual_stdout = "x" * (MAX_OUTPUT_BYTES + 1) if large_output else stdout_content
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(actual_stdout)
            stdout_file.flush()
        if stderr_file and hasattr(stderr_file, "write"):
            stderr_file.write(stderr_content)
            stderr_file.flush()

        # Make poll() return None initially (live process)
        _poll_count = [0]

        def _poll() -> int | None:
            _poll_count[0] += 1
            if timeout and _poll_count[0] < 2:
                return None
            if large_output and _poll_count[0] < 2:
                return None
            fake.returncode = returncode
            return returncode

        if timeout:
            # Process never finishes on its own
            def _poll_timeout() -> int | None:
                return None

            fake.poll = _poll_timeout

            # Real Popen.wait() reaps the killed child and sets .returncode (e.g.
            # -15 for SIGTERM). Mirror that so the executor's null-on-timeout
            # override is actually exercised, not masked by a None left on the mock.
            def _wait_sets_returncode(*_a: Any, **_k: Any) -> int:
                fake.returncode = returncode
                return returncode

            fake.wait.side_effect = _wait_sets_returncode
        else:
            fake.poll = _poll
            fake.wait.return_value = returncode

        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree") as mock_kill,
    ):
        result = run_cpsat_python(source, script_timeout_ms=script_timeout_ms, tracker=tracker)
    result._mock_kill = mock_kill  # type: ignore[attr-defined]
    return result


class _SpyTracker:
    """Records register/unregister calls so wiring can be asserted without a kill."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def register(self, proc: Any) -> None:
        self.events.append(("register", proc))

    def unregister(self, proc: Any) -> None:
        self.events.append(("unregister", proc))


# --- shared validation helpers ----------------------------------------------


def test_validate_checker_args_accepts_valid_checker_timeout_pair() -> None:
    validate_checker_args(checker="print('ok')", checker_timeout_ms=100)


def test_validate_checker_args_rejects_timeout_without_checker() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms supplied without checker"):
        validate_checker_args(checker=None, checker_timeout_ms=100)


def test_validate_checker_args_rejects_both_checker_forms() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        validate_checker_args(
            checker="print('ok')", checker_timeout_ms=None, checker_path=Path("checker.py")
        )


def test_validate_checker_args_accepts_timeout_with_only_a_checker_path() -> None:
    validate_checker_args(checker=None, checker_timeout_ms=100, checker_path=Path("checker.py"))


def test_validate_checker_args_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms must be positive"):
        validate_checker_args(checker="print('ok')", checker_timeout_ms=0)


def test_validate_checker_args_rejects_blank_checker() -> None:
    with pytest.raises(ValueError, match="checker must be non-empty"):
        validate_checker_args(checker="   ", checker_timeout_ms=None)


def test_effective_checker_timeout_uses_explicit_value_or_default() -> None:
    explicit = effective_checker_timeout_ms(checker_timeout_ms=250, default_script_timeout_ms=1000)
    fallback = effective_checker_timeout_ms(checker_timeout_ms=None, default_script_timeout_ms=1000)

    assert (explicit, fallback) == (250, 1000)


@pytest.mark.parametrize("seed", [-2_147_483_648, -1, 0, 2_147_483_647])
def test_validate_cpsat_random_seed_accepts_signed_int32(seed: int) -> None:
    assert validate_cpsat_random_seed(seed) == seed


@pytest.mark.parametrize("seed", [True, False, 1.5, "7"])
def test_validate_cpsat_random_seed_rejects_non_integer_values(seed: object) -> None:
    with pytest.raises(ValueError, match="non-bool integer"):
        validate_cpsat_random_seed(seed)


@pytest.mark.parametrize("seed", [-2_147_483_649, 2_147_483_648])
def test_validate_cpsat_random_seed_rejects_out_of_signed_int32_range(seed: int) -> None:
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        validate_cpsat_random_seed(seed)


# (a) valid JSON → parsed status/solution
def test_run_cpsat_python_parses_valid_solution() -> None:
    result = _run_with_mocked_proc(stdout_content=_VALID_STDOUT)

    assert result.status == "optimal"
    assert result.solution == _VALID_SOLUTION
    assert result.objective == 10
    assert result.timed_out is False
    assert result.truncated is False


def test_run_cpsat_python_preserves_source_bytes(
    monkeypatch: pytest.MonkeyPatch,
    windows_text_newlines: None,
) -> None:
    source = "# café\r\nprint('x')\n"
    staged_source: bytes | None = None

    def _fake_execute_child(argv: list[str], **_kwargs: Any) -> ChildExecutionResult:
        nonlocal staged_source
        staged_source = Path(argv[2]).read_bytes()
        return ChildExecutionResult(
            stdout=_VALID_STDOUT,
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=1,
        )

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.execute_child", _fake_execute_child)

    run_cpsat_python(source)

    assert staged_source == source.encode("utf-8")


# (b) non-zero exit → status="error", stderr surfaced
def test_run_cpsat_python_nonzero_exit_yields_error() -> None:
    result = _run_with_mocked_proc(
        stdout_content="bad output",
        stderr_content="something failed",
        returncode=1,
    )

    assert result.status == "error"
    assert "failed" in result.stderr


# (c) timeout → timed_out, status="timeout", tree-kill invoked
def test_run_cpsat_python_timeout_kills_tree_and_sets_status() -> None:
    result = _run_with_mocked_proc(timeout=True, script_timeout_ms=50)

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result._mock_kill.called  # type: ignore[attr-defined]


# (c1) a non-positive timeout is rejected before any child is spawned, matching
# the MiniZinc path's validate_model_and_timeout.
@pytest.mark.parametrize("script_timeout_ms", [0, -1])
def test_run_cpsat_python_non_positive_timeout_raises(script_timeout_ms: int) -> None:
    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="script_timeout_ms must be positive"):
            run_cpsat_python("print('x')", script_timeout_ms=script_timeout_ms)
    fake_popen.assert_not_called()


# (c2) the child interpreter is launched unbuffered (-u) so prints reach the
# capture files in real time and survive a timeout kill.
def test_run_cpsat_python_launches_child_unbuffered() -> None:
    captured: dict[str, list[str]] = {}

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured["cmd"] = cmd
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(_VALID_STDOUT)
            stdout_file.flush()
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        run_cpsat_python("print('hi')", script_timeout_ms=5000)

    cmd = captured["cmd"]
    assert cmd[0] == sys.executable
    assert cmd[1] == "-u"  # unbuffered, before the script path


# (c3) on timeout, an intermediate JSON block (best-so-far from a callback) is
# recovered into solution/objective; status stays the executor-owned "timeout".
def test_run_cpsat_python_timeout_recovers_partial_solution() -> None:
    partial = json.dumps({"status": "feasible", "objective": 3, "solution": {"x": 1}})
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, script_timeout_ms=50)

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.solution == {"x": 1}
    assert result.objective == 3


# (c4) timeout with no parseable JSON keeps solution/objective None.
def test_run_cpsat_python_timeout_without_partial_has_no_solution() -> None:
    result = _run_with_mocked_proc(
        timeout=True, stdout_content="searching...\n", script_timeout_ms=50
    )

    assert result.status == "timeout"
    assert result.solution is None
    assert result.objective is None


# (c5) on timeout the killed child's exit code (SIGTERM -> -15) is reported as null,
# matching the documented contract (README: return_code "null on timeout") so a
# timeout is not misread as a child error. The mock sets returncode=-15 on wait, so
# this fails if the executor forwards it instead of overriding to None.
def test_run_cpsat_python_timeout_return_code_is_none() -> None:
    result = _run_with_mocked_proc(timeout=True, returncode=-15, script_timeout_ms=50)

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.return_code is None


# (d) unparseable stdout → status="error"
def test_run_cpsat_python_unparseable_stdout_yields_error() -> None:
    result = _run_with_mocked_proc(stdout_content="not json at all")

    assert result.status == "error"
    assert result.solution is None


# (f) off-vocabulary status → normalized to "error"
def test_run_cpsat_python_off_vocabulary_status_normalized_to_error() -> None:
    bad_status = json.dumps({"status": "MODEL_INVALID", "objective": None, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=bad_status)

    assert result.status == "error"
    # Must not raise — CpsatPythonResult must be constructable
    assert isinstance(result, CpsatPythonResult)


# (g) a script may not self-report "timeout" — only the executor sets it
def test_run_cpsat_python_script_reported_timeout_normalized_to_error() -> None:
    forged = json.dumps({"status": "timeout", "objective": None, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=forged)

    assert result.status == "error"
    assert result.timed_out is False


# (h) a non-numeric objective is a CONTRACT ERROR, not a silent null: the field
# has always been documented as number-or-null, and permissive normalization hid
# a broken emit block behind a plausible-looking result.
def test_run_cpsat_python_non_numeric_objective_yields_contract_error() -> None:
    payload = json.dumps({"status": "optimal", "objective": "lots", "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "error"
    assert result.solution is None
    assert result.diagnostic is not None
    assert result.diagnostic.details == {
        "field": "objective",
        "reason": "must be a finite number or null",
        "return_code": 0,
    }


# (h3) best_objective_bound is parsed even for status="unknown", where no
# incumbent/objective was found — this is the diagnostic signal the field exists for.
def test_run_cpsat_python_parses_best_objective_bound_for_unknown_status() -> None:
    payload = json.dumps(
        {"status": "unknown", "objective": None, "solution": {}, "best_objective_bound": 5}
    )
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "unknown"
    assert result.objective is None
    assert result.best_objective_bound == 5


# (h4) an old script that never emits best_objective_bound must still parse cleanly.
def test_run_cpsat_python_missing_best_objective_bound_is_none() -> None:
    result = _run_with_mocked_proc(stdout_content=_VALID_STDOUT)

    assert result.status == "optimal"
    assert result.best_objective_bound is None


# (h5) invalid best_objective_bound values (bool, non-numeric) are normalized to None,
# matching normalize_objective's rules exactly.
@pytest.mark.parametrize("raw", [True, "lots"])
def test_run_cpsat_python_invalid_best_objective_bound_becomes_none(raw: object) -> None:
    payload = json.dumps(
        {"status": "unknown", "objective": None, "solution": {}, "best_objective_bound": raw}
    )
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.best_objective_bound is None


# (h6) on timeout, a recovered intermediate JSON block's best_objective_bound is
# carried through exactly like solution/objective.
def test_run_cpsat_python_timeout_recovers_partial_best_objective_bound() -> None:
    partial = json.dumps(
        {"status": "feasible", "objective": 3, "solution": {"x": 1}, "best_objective_bound": 1}
    )
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, script_timeout_ms=50)

    assert result.status == "timeout"
    assert result.best_objective_bound == 1


# --- required stdout envelope ----------------------------------------------
#
# `status`, `objective`, and `solution` are REQUIRED and type-checked on a clean
# exit. A violation is status="error" with no incumbent and a child_process_error
# diagnostic naming the offending field — the only client-visible transport for
# it (there is deliberately no public result field).


def _envelope_error_field(payload: dict) -> str:
    """Run a payload through the executor and return the diagnosed field name."""
    result = _run_with_mocked_proc(stdout_content=json.dumps(payload))
    assert result.status == "error"
    assert result.diagnostic is not None
    details = result.diagnostic.details
    assert details is not None
    return str(details["field"])


def test_run_cpsat_python_missing_status_is_a_contract_error() -> None:
    assert _envelope_error_field({"objective": 1, "solution": {"x": 1}}) == "status"


def test_run_cpsat_python_missing_objective_is_a_contract_error() -> None:
    assert _envelope_error_field({"status": "optimal", "solution": {"x": 1}}) == "objective"


def test_run_cpsat_python_missing_solution_is_a_contract_error() -> None:
    assert _envelope_error_field({"status": "optimal", "objective": 1}) == "solution"


def test_run_cpsat_python_non_string_status_is_a_contract_error() -> None:
    assert _envelope_error_field({"status": 3, "objective": 1, "solution": {"x": 1}}) == "status"


def test_run_cpsat_python_off_vocabulary_status_names_the_status_field() -> None:
    # The status still normalizes to "error" (see the (f) case above); what is new
    # is that the diagnostic says WHICH field was wrong.
    payload = {"status": "MODEL_INVALID", "objective": None, "solution": {}}
    assert _envelope_error_field(payload) == "status"


def test_run_cpsat_python_off_vocabulary_status_drops_the_solution() -> None:
    # A violation yields no incumbent, so an off-vocabulary status no longer
    # carries its solution through the way the pre-envelope normalization did.
    payload = json.dumps({"status": "MODEL_INVALID", "objective": 1, "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.solution is None


def test_run_cpsat_python_oversized_status_is_truncated_in_the_diagnostic() -> None:
    # The status arrives from stdout, which is capped only at MAX_OUTPUT_BYTES, and
    # the reason is copied into BOTH the message and `details` — so echoing it whole
    # would amplify one ~1 MiB string threefold. It is bounded and marked truncated.
    huge_status = "X" * 100_000
    payload = json.dumps({"status": huge_status, "objective": 1, "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.diagnostic is not None
    details = result.diagnostic.details
    assert details is not None
    reason = str(details["reason"])
    assert "(truncated)" in reason
    assert len(reason) < 200
    assert len(result.diagnostic.message) < 300


def test_run_cpsat_python_short_off_vocabulary_status_is_echoed_whole() -> None:
    # The bound must not cost the repair signal for a realistic mistake: a client
    # fixing its emit block needs to see the exact status it printed.
    payload = json.dumps({"status": "OPTIMAL", "objective": 1, "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.diagnostic is not None
    details = result.diagnostic.details
    assert details is not None
    reason = str(details["reason"])
    assert "'OPTIMAL'" in reason
    assert "(truncated)" not in reason


def test_run_cpsat_python_non_object_solution_is_a_contract_error() -> None:
    payload = {"status": "optimal", "objective": 1, "solution": [{"x": 1}]}
    assert _envelope_error_field(payload) == "solution"


def test_run_cpsat_python_null_solution_is_a_contract_error() -> None:
    # `null` is the shape seen in the wild: a run with no incumbent must still
    # emit `{}`, or a legitimate infeasible/unknown result becomes an error.
    payload = {"status": "infeasible", "objective": None, "solution": None}
    assert _envelope_error_field(payload) == "solution"


# --- non-finite numbers nested inside `solution` ---------------------------
#
# `objective` has always been finiteness-checked. `solution` was not, so a NaN
# buried in it reached three consumers that disagree: json.dumps writes a bare
# `NaN` into the checker payload and the saved artifact, while a strict client's
# decoder rejects or nulls it. The gate rejects at any depth.


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_run_cpsat_python_non_finite_solution_value_is_a_contract_error(literal: str) -> None:
    # All three non-finite literals Python's decoder accepts, so an implementation
    # reaching for math.isnan instead of math.isfinite fails two of these cases.
    payload = f'{{"status": "optimal", "objective": 1, "solution": {{"x": {literal}}}}}'
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "error"
    assert result.diagnostic is not None
    assert result.diagnostic.details is not None
    assert result.diagnostic.details["field"] == 'solution["x"]'


def test_run_cpsat_python_non_finite_inside_a_list_names_the_indexed_path() -> None:
    # Proves the walk enters sequences, not just nested objects.
    payload = '{"status": "optimal", "objective": 1, "solution": {"t": [1, {"start": NaN}]}}'
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.diagnostic is not None
    assert result.diagnostic.details is not None
    assert result.diagnostic.details["field"] == 'solution["t"][1]["start"]'


def test_run_cpsat_python_non_finite_solution_drops_the_incumbent() -> None:
    payload = '{"status": "optimal", "objective": 1, "solution": {"x": NaN}}'
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.solution is None


def test_run_cpsat_python_non_finite_timeout_partial_is_not_recovered() -> None:
    # Timeout stays executor-owned: the partial is dropped, not promoted to an error.
    partial = '{"status": "feasible", "objective": 3, "solution": {"t": [{"s": NaN}]}}'
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, script_timeout_ms=50)

    assert result.status == "timeout"
    assert result.solution is None
    assert result.diagnostic is not None
    assert result.diagnostic.details is not None
    assert result.diagnostic.details["rejected_partial_field"] == 'solution["t"][0]["s"]'


def test_envelope_gate_accepts_every_legal_leaf_type_around_finite_floats() -> None:
    # Over-rejection guard. This is the test that fails if the walk reuses
    # normalize_objective, which also rejects bool and non-numeric leaves — all of
    # which are valid decision values (a machine name, a boolean assignment).
    solution = {
        "finite_float": 1.5,
        "huge_int": 2**200,
        "label": "machine-3",
        "flag": True,
        "unset": None,
        "nested": {"deep": [0.0, -1, "x", False, None, {"deeper": 2.25}]},
    }
    payload = json.dumps({"status": "optimal", "objective": 1, "solution": solution})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "optimal"
    assert result.solution == solution


def test_envelope_violation_walks_nesting_deeper_than_the_recursion_limit() -> None:
    # Calls the gate directly with a dict built in Python, NOT through stdout: the
    # decoder's own ceiling is a C-stack artifact that differs by interpreter
    # (~9,997 levels on 3.12, ~40,091 on 3.14 at a limit of 1000), so routing this
    # through JSON would pin the test to a number that can drift out from under it —
    # and the drift would be silent, since a decoder RecursionError looks exactly
    # like the bug being guarded against. Every other case here covers integration.
    depth = 5_000
    assert depth > sys.getrecursionlimit()
    solution: dict[str, Any] = {"leaf": math.nan}
    for _ in range(depth):
        solution = {"a": solution}

    violation = _envelope_violation({"status": "optimal", "objective": 1, "solution": solution})

    assert violation is not None
    field, reason = violation
    assert field.endswith('["leaf"]')
    assert "finite" in reason


def test_envelope_violation_key_path_distinguishes_punctuation_from_nesting() -> None:
    # A literal key containing the path syntax must not read as real nesting: under
    # a dotted encoding this solution and {"tasks": [_, _, _, {"start": nan}]} would
    # both render as `solution.tasks[3].start`.
    violation = _envelope_violation(
        {"status": "optimal", "objective": 1, "solution": {"tasks[3].start": math.nan}}
    )

    assert violation is not None
    assert violation[0] == 'solution["tasks[3].start"]'


def test_envelope_violation_reports_the_first_non_finite_in_payload_order() -> None:
    # A stack walk that pushes children without reversing them pops the LAST
    # sibling first, so it would name `solution["b"]` here — not the offender a
    # client's eye reaches first when scanning its own payload.
    violation = _envelope_violation(
        {
            "status": "optimal",
            "objective": 1,
            "solution": {"a": [0.0, math.inf], "b": math.nan},
        }
    )

    assert violation is not None
    assert violation[0] == 'solution["a"][1]'


def test_envelope_violation_elides_an_over_long_key_path() -> None:
    # The path is built from the CHILD'S OWN key names, so it grows with the
    # payload rather than with a fixed vocabulary: uncapped, a ~412 KB solution
    # nested under 200-char keys yields a ~408 KB `field` that the diagnostic
    # then repeats in its message. Same amplification guard as the status echo.
    solution: dict[str, Any] = {"leaf": math.nan}
    for _ in range(50):
        solution = {"k" * 500: solution}

    violation = _envelope_violation({"status": "optimal", "objective": 1, "solution": solution})

    assert violation is not None
    assert len(violation[0]) <= _KEY_PATH_MAX_CHARS


def test_envelope_violation_elided_path_keeps_the_offending_leaf_key() -> None:
    # Middle elision, not a head cut: the leaf key is the whole repair signal, so
    # a truncation that dropped the tail would leave a client no better off than
    # naming `solution` alone.
    solution: dict[str, Any] = {"start": math.nan}
    for _ in range(50):
        solution = {"k" * 500: solution}

    violation = _envelope_violation({"status": "optimal", "objective": 1, "solution": solution})

    assert violation is not None
    assert violation[0].startswith("solution[")
    assert violation[0].endswith('["start"]')


def test_run_cpsat_python_contract_error_diagnostic_details_are_field_reason_return_code() -> None:
    result = _run_with_mocked_proc(stdout_content=json.dumps({"status": "optimal", "objective": 1}))

    assert result.diagnostic is not None
    assert result.diagnostic.details == {
        "field": "solution",
        "reason": "required key is missing",
        "return_code": 0,
    }


def test_run_cpsat_python_contract_error_preserves_raw_streams() -> None:
    payload = json.dumps({"status": "optimal", "objective": 1})
    result = _run_with_mocked_proc(stdout_content=payload, stderr_content="a warning")

    assert result.stdout == payload
    assert result.stderr == "a warning"


def test_run_cpsat_python_null_objective_is_valid_for_a_feasibility_model() -> None:
    payload = json.dumps({"status": "feasible", "objective": None, "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "feasible"
    assert result.objective is None


def test_run_cpsat_python_extra_envelope_keys_are_accepted() -> None:
    payload = json.dumps(
        {
            "status": "optimal",
            "objective": 10,
            "solution": _VALID_SOLUTION,
            "stats": {"conflicts": 3},
            "result_file": "/tmp/out.json",
        }
    )
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "optimal"
    assert result.diagnostic is None


def test_run_cpsat_python_empty_solution_keeps_the_specific_diagnostic() -> None:
    # `{}` is a WELL-TYPED solution: emptiness is an acceptance rule, so this must
    # stay the more specific "reported a status but emitted no solution" branch,
    # not be reclassified as a malformed envelope.
    payload = json.dumps({"status": "optimal", "objective": 10, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "optimal"
    assert result.diagnostic is not None
    assert result.diagnostic.details == {"status": "optimal"}


def test_run_cpsat_python_empty_solution_fails_the_incumbent_eligibility_gate() -> None:
    payload = json.dumps({"status": "optimal", "objective": 10, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert diagnostic_incumbent_eligibility(result) == (False, "solution is missing or empty")


def test_run_cpsat_python_malformed_timeout_partial_is_not_recovered() -> None:
    partial = json.dumps({"status": "feasible", "solution": {"x": 1}})  # no `objective`
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, script_timeout_ms=50)

    assert result.solution is None


def test_run_cpsat_python_off_vocabulary_timeout_partial_is_not_recovered() -> None:
    # An intermediate block is where a script is most likely to invent a status;
    # the envelope gate drops it rather than recovering an unclassifiable partial.
    partial = json.dumps({"status": "in_progress", "objective": 3, "solution": {"x": 1}})
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, script_timeout_ms=50)

    assert result.solution is None


def test_run_cpsat_python_malformed_timeout_partial_keeps_the_timeout_diagnostic() -> None:
    # Timeout is executor-owned and its diagnostic keeps precedence: a malformed
    # partial must never turn the run into a protocol error.
    partial = json.dumps({"status": "feasible", "solution": {"x": 1}})
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, script_timeout_ms=50)

    assert result.status == "timeout"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "timeout_no_incumbent"


def test_run_cpsat_python_malformed_timeout_partial_reports_the_rejected_field() -> None:
    # Dropping the partial must not drop the repair signal: an experiment attempt
    # row carries no stdout, so the diagnostic is the only place a client can
    # learn a partial existed and why it was rejected.
    partial = json.dumps({"status": "feasible", "solution": {"x": 1}})
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, script_timeout_ms=50)

    assert result.diagnostic is not None
    assert result.diagnostic.details is not None
    assert result.diagnostic.details["rejected_partial_field"] == "objective"


def test_run_cpsat_python_timeout_without_partial_reports_no_rejected_field() -> None:
    # No JSON block at all is not a rejected partial; the key must be absent
    # rather than null, so its presence always means "a block was dropped".
    result = _run_with_mocked_proc(
        timeout=True, stdout_content="searching...", script_timeout_ms=50
    )

    assert result.diagnostic is not None
    assert result.diagnostic.details is not None
    assert "rejected_partial_field" not in result.diagnostic.details


# (h2) trailing output after the JSON block must not defeat parsing, and a nested
# object inside the payload must not be mistaken for the result.
def test_run_cpsat_python_parses_json_with_trailing_output() -> None:
    noisy = _VALID_STDOUT + "\n[INFO] solver shutdown complete\n"
    result = _run_with_mocked_proc(stdout_content=noisy)

    assert result.status == "optimal"
    assert result.solution == _VALID_SOLUTION
    assert result.objective == 10


# (i) a fast-exiting script that still overran the cap is flagged truncated
def test_run_cpsat_python_fast_exit_large_output_is_flagged_truncated() -> None:
    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0  # already exited before the first poll
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write("x" * (MAX_OUTPUT_BYTES + 1))
            stdout_file.flush()
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        result = run_cpsat_python("print('hi')", script_timeout_ms=5000)

    assert result.truncated is True
    assert result.status == "error"


# --- run_cpsat_python_file: path-based variant -----------------------------


def _run_file_with_mocked_proc(
    script_path: Path,
    *,
    stdout_content: str = _VALID_STDOUT,
    returncode: int = 0,
    script_timeout_ms: int = 5000,
    args: list[str] | None = None,
    tracker: Any = None,
    env: dict[str, str] | None = None,
) -> tuple[CpsatPythonResult, dict[str, Any]]:
    """Run run_cpsat_python_file with popen patched; capture the popen call."""
    captured: dict[str, Any] = {}

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured["cmd"] = cmd
        captured.update(kwargs)
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = returncode
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(stdout_content)
            stdout_file.flush()
        fake.poll = lambda: returncode
        fake.wait.return_value = returncode
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        result = run_cpsat_python_file(
            script_path, script_timeout_ms=script_timeout_ms, args=args, tracker=tracker, env=env
        )
    return result, captured


# (k) a valid script file delegates to the same execution/parse path as inline.
def test_run_cpsat_python_file_parses_valid_solution(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('ignored by mock')", encoding="utf-8")

    result, _ = _run_file_with_mocked_proc(script)

    assert result.status == "optimal"
    assert result.solution == _VALID_SOLUTION
    assert result.objective == 10


# (k1) the key value-add: the script runs in its OWN directory (cwd=parent), so a
# relative open()/import resolves — unlike inline, which runs in a throwaway tempdir.
def test_run_cpsat_python_file_runs_in_script_directory(tmp_path: Path) -> None:
    script = tmp_path / "sub" / "model.py"
    script.parent.mkdir()
    script.write_text("print('x')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script)

    assert captured["cwd"] == str(script.parent.resolve())


# (k2) argv runs the real file path unbuffered (-u), not a copy.
def test_run_cpsat_python_file_argv_targets_file_unbuffered(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script)

    assert captured["cmd"] == [sys.executable, "-u", str(script.resolve())]


# (k2a) `args` trail the script path, so the child reads them as sys.argv[1:].
def test_run_cpsat_python_file_appends_args_after_script_path(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script, args=["data_ft10.json"])

    assert captured["cmd"] == [sys.executable, "-u", str(script.resolve()), "data_ft10.json"]


# (k3) tracker is registered then unregistered on the file path too.
def test_run_cpsat_python_file_registers_then_unregisters_child(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")
    tracker = _SpyTracker()

    _run_file_with_mocked_proc(script, tracker=tracker)

    assert [name for name, _ in tracker.events] == ["register", "unregister"]


# (k4) a missing path is rejected before any child is spawned.
def test_run_cpsat_python_file_missing_path_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.py"

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="does not exist"):
            run_cpsat_python_file(missing)
    fake_popen.assert_not_called()


def test_run_cpsat_python_file_nul_arg_raises_before_spawn(tmp_path: Path) -> None:
    """A NUL is caught in validation, not by Popen's own `embedded null byte`."""
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match=r"args\[0\] contains a NUL character"):
            run_cpsat_python_file(script, args=["\0"])
    fake_popen.assert_not_called()


# --- on_start hook -----------------------------------------------------------


def test_run_cpsat_python_no_on_start_default_is_none() -> None:
    """Omitting on_start (default None) behaves identically to the old API."""
    result = _run_with_mocked_proc()

    assert result.status == "optimal"


def test_run_cpsat_python_file_on_start_called_once(tmp_path: Path) -> None:
    """on_start works on the file-path entry point too."""
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")
    received: list[Any] = []

    _, _ = _run_file_with_mocked_proc(script)  # baseline: no on_start

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 7777
        fake.returncode = 0
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(_VALID_STDOUT)
            stdout_file.flush()
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        run_cpsat_python_file(script, script_timeout_ms=5000, on_start=lambda p: received.append(p))

    assert len(received) == 1
    assert received[0].pid == 7777


# (k5) a directory is not a runnable script.
def test_run_cpsat_python_file_directory_raises(tmp_path: Path) -> None:
    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="not a file"):
            run_cpsat_python_file(tmp_path)
    fake_popen.assert_not_called()


# (k6) an empty/whitespace-only script is rejected with a clear error.
def test_run_cpsat_python_file_empty_file_raises(tmp_path: Path) -> None:
    script = tmp_path / "empty.py"
    script.write_text("   \n", encoding="utf-8")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="is empty"):
            run_cpsat_python_file(script)
    fake_popen.assert_not_called()


# (k7) a non-UTF-8 file surfaces a clear ValueError, not an opaque decode traceback.
def test_run_cpsat_python_file_non_utf8_raises(tmp_path: Path) -> None:
    script = tmp_path / "latin1.py"
    script.write_bytes(b"print('caf\xe9')")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="not valid UTF-8"):
            run_cpsat_python_file(script)
    fake_popen.assert_not_called()


# (k8) a non-positive timeout is rejected before any child is spawned.
@pytest.mark.parametrize("script_timeout_ms", [0, -1])
def test_run_cpsat_python_file_non_positive_timeout_raises(
    tmp_path: Path, script_timeout_ms: int
) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="script_timeout_ms must be positive"):
            run_cpsat_python_file(script, script_timeout_ms=script_timeout_ms)
    fake_popen.assert_not_called()


# --- _normalize_objective tests -------------------------------------------


def test_normalize_objective_accepts_int() -> None:
    assert _normalize_objective(42) == 42


def test_normalize_objective_accepts_float() -> None:
    assert _normalize_objective(3.14) == 3.14


def test_normalize_objective_accepts_zero() -> None:
    assert _normalize_objective(0) == 0


def test_normalize_objective_rejects_bool_true() -> None:
    assert _normalize_objective(True) is None


def test_normalize_objective_rejects_bool_false() -> None:
    assert _normalize_objective(False) is None


def test_normalize_objective_rejects_nan() -> None:
    assert _normalize_objective(math.nan) is None


def test_normalize_objective_rejects_positive_inf() -> None:
    assert _normalize_objective(math.inf) is None


def test_normalize_objective_rejects_negative_inf() -> None:
    assert _normalize_objective(-math.inf) is None


def test_normalize_objective_rejects_string() -> None:
    assert _normalize_objective("10") is None


def test_normalize_objective_rejects_none() -> None:
    assert _normalize_objective(None) is None


def test_normalize_objective_accepts_huge_int_without_overflow() -> None:
    # A CP-SAT objective too large to convert to a float must not crash
    # (math.isfinite would raise OverflowError); the exact int is preserved.
    big = 10**400
    assert _normalize_objective(big) == big


# --- internal env overlay ----------------------------------------------------


def _capture_popen_env(source: str, *, env: dict[str, str | None] | None) -> dict[str, str] | None:
    """Run run_cpsat_python with a fake Popen and return the env kwarg it received."""
    captured: dict[str, Any] = {}

    def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured["env"] = kwargs.get("env")
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch("openconstraint_mcp.shared.childrun.popen_process_group", side_effect=_fake_popen),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        run_cpsat_python(source, script_timeout_ms=1000, env=env)
    return captured["env"]


def test_seed_config_env_always_returns_both_keys() -> None:
    # Both protocol keys are always present, set to the requested value or
    # explicit None — never omitted — so a caller can't accidentally build an
    # overlay that leaves an unrequested key to whatever the parent process
    # happens to have inherited.
    assert seed_config_env(seed=None, config_path=None) == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }
    assert seed_config_env(seed=7, config_path=None) == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "7",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


def test_env_overlay_none_value_clears_stale_parent_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: a server process launched from a shell that already
    # exports OPENCONSTRAINT_MCP_CPSAT_CONFIG (e.g. leftover from manual
    # testing) must not leak that stale value into a child whose caller
    # explicitly requested no config. Before the fix, execute_child's env
    # overlay only ever added keys on top of os.environ, so an unrequested key
    # silently passed through from the parent's environment; seed_config_env
    # now emits an explicit None for it, and execute_child must delete it.
    monkeypatch.setenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", "/stale/leftover-config.json")

    env = _capture_popen_env(
        "print('x')",
        env=seed_config_env(seed=None, config_path=None),
    )

    assert env is not None
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" not in env
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" not in env
    # Unrelated inherited variables are untouched.
    assert "PATH" in env


def test_env_overlay_none_value_clears_stale_var_even_with_other_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same leak, but for the "seed requested, config not" combination: only
    # setting the seed key in the overlay must not let a stale config var
    # ride along from the parent's environment.
    monkeypatch.setenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", "/stale/leftover-config.json")

    env = _capture_popen_env(
        "print('x')",
        env=seed_config_env(seed=7, config_path=None),
    )

    assert env is not None
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "7"
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" not in env


def test_run_cpsat_python_file_forwards_env_overlay(tmp_path: Path) -> None:
    # run_cpsat_python_file mirrors run_cpsat_python's env overlay: same execute_child,
    # so the same OPENCONSTRAINT_MCP_CPSAT_SEED-style overlay must reach the child here too.
    script = tmp_path / "model.py"
    script.write_text("print('ignored by mock')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script, env={"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"})

    assert captured["env"]["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "7"
    assert "PATH" in captured["env"]


# --- run_cpsat_python_file_checked -------------------------------------------


def _checked_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Write a valid model script and checker script; return both paths."""
    script = tmp_path / "model.py"
    script.write_text("print('ignored by mock')", encoding="utf-8")
    checker = tmp_path / "checker.py"
    checker.write_text("print('ignored by mock')", encoding="utf-8")
    return script, checker


def _checked_result(
    status: str,
    *,
    solution: dict | None,
    objective: float | int | None = 10,
    timed_out: bool = False,
) -> CpsatPythonResult:
    result = CpsatPythonResult(
        status=status,  # type: ignore[arg-type]
        solution=solution,
        objective=objective,
        stdout="",
        stderr="",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=False,
        duration_ms=5,
    )
    result.diagnostic = cpsat_result_diagnostic(result)
    return result


def _checker_report(status: str) -> CpsatCheckerReport:
    report = CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[] if status == "accepted" else ["nope"],
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=False,
        truncated=False,
    )
    report.diagnostic = checker_report_diagnostic(report)
    return report


def _patch_checked(
    monkeypatch: pytest.MonkeyPatch,
    run_result: CpsatPythonResult,
    checker_outcome: CpsatCheckerReport | Exception,
) -> list[dict[str, Any]]:
    """Stub the model run and the checker run; return the checker call log."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_cpsat_python_file",
        lambda script, **kw: run_result,
    )

    def _fake_run_checker_file(checker: Path, result: Any, **kw: Any) -> CpsatCheckerReport:
        calls.append({"checker": checker, "result": result, **kw})
        if isinstance(checker_outcome, Exception):
            raise checker_outcome
        return checker_outcome

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_checker_file", _fake_run_checker_file)
    return calls


def test_checked_run_forwards_args_and_env_to_the_model_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dropping `args=` or `env=` on the inner call would silently no-op a
    # seed/config replay while still returning a plausible-looking result.
    script, checker = _checked_pair(tmp_path)
    model_kw: dict[str, Any] = {}

    def _fake_run(script_path: Path, **kw: Any) -> CpsatPythonResult:
        model_kw.update(kw)
        return _checked_result("optimal", solution={"x": 1})

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_cpsat_python_file", _fake_run)
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_checker_file",
        lambda *args, **kw: _checker_report("accepted"),
    )

    run_cpsat_python_file_checked(
        script, checker, args=["data.json"], env={"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"}
    )

    assert model_kw["args"] == ["data.json"]
    assert model_kw["env"] == {"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"}


def test_checked_run_accepted_carries_the_checker_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.checker is not None
    assert result.checker.status == "accepted"


def test_checked_run_accepted_leaves_the_top_level_diagnostic_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.diagnostic is None


def test_checked_run_envelope_violation_keeps_the_offending_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `checked_result_diagnostic` recomposes the top-level diagnostic from the
    # run and the checker, so this route could silently downgrade the
    # field-specific contract error to the generic child-process message. The
    # violated run is never checker-eligible, so the run's own diagnostic is
    # what must survive the recomposition.
    script, checker = _checked_pair(tmp_path)
    violated = _run_with_mocked_proc(stdout_content=json.dumps({"status": "optimal"}))
    _patch_checked(monkeypatch, violated, _checker_report("accepted"))

    result = run_cpsat_python_file_checked(script, checker)

    assert result.diagnostic is not None
    assert result.diagnostic.details is not None
    assert result.diagnostic.details["field"] == "objective"


def test_checked_run_rejected_carries_the_rejected_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("rejected")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.checker is not None
    assert result.checker.status == "rejected"


def test_checked_run_rejected_but_optimal_sets_the_top_level_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D8: `diagnostic: null` is the clean-success signal, so an optimal run the
    # checker rejected must NOT come back with a null top-level diagnostic.
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("rejected")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.diagnostic is not None
    assert result.diagnostic.category == "checker_failed"


def test_checked_run_rejected_preserves_the_model_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("rejected")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.status == "optimal"
    assert result.solution == {"x": 1}


def test_checked_run_timeout_with_incumbent_runs_the_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D5: `timeout` IS a diagnostic-accept status, so a recovered incumbent is
    # still checkable.
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch,
        _checked_result("timeout", solution={"x": 1}, timed_out=True),
        _checker_report("accepted"),
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert len(calls) == 1
    assert result.checker is not None
    assert result.checker_skipped_reason is None


def test_checked_run_timeout_without_incumbent_skips_the_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other side of the D5 boundary: same status, no solution -> not checkable.
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch,
        _checked_result("timeout", solution=None, timed_out=True),
        _checker_report("accepted"),
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert calls == []
    assert result.checker is None
    assert result.checker_skipped_reason == "solution is missing or empty"


def test_checked_run_infeasible_skips_the_checker_naming_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("infeasible", solution=None), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert calls == []
    assert result.checker_skipped_reason == "status='infeasible'"


def test_checked_run_checker_infrastructure_error_yields_a_diagnosed_error_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D4: a post-run infrastructure failure (temp-file write, spawn) becomes an
    # `error` report — it must never discard the completed model result.
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch,
        _checked_result("optimal", solution={"x": 1}),
        OSError("no space left on device"),
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.status == "optimal"
    assert result.checker is not None
    assert result.checker.status == "error"
    assert any("checker infrastructure error" in e for e in result.checker.errors)
    assert result.checker.diagnostic is not None
    assert result.checker.diagnostic.category == "checker_failed"


def test_checked_run_defaults_the_checker_timeout_to_the_run_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker, script_timeout_ms=12_345)

    assert calls[0]["timeout_ms"] == 12_345
    assert result.checker_timeout_ms == 12_345


def test_checked_run_self_test_caps_the_implicit_checker_timeout_to_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch,
        _checked_result("optimal", solution={"tasks": [{"start": 0}, {"start": 5}]}),
        _checker_report("accepted"),
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert [call["timeout_ms"] for call in calls] == [8_100] * 5
    assert result.checker_timeout_ms == 8_100


def test_checked_run_explicit_checker_timeout_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(
        script, checker, script_timeout_ms=12_345, checker_timeout_ms=999
    )

    assert calls[0]["timeout_ms"] == 999
    assert result.checker_timeout_ms == 999


def test_checked_run_forwards_problem_to_the_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    run_cpsat_python_file_checked(script, checker, problem='{"jobs": []}')

    assert calls[0]["problem"] == '{"jobs": []}'


def test_checked_run_rejects_a_non_positive_checker_timeout(tmp_path: Path) -> None:
    script, checker = _checked_pair(tmp_path)

    with pytest.raises(ValueError, match="checker_timeout_ms must be positive"):
        run_cpsat_python_file_checked(script, checker, checker_timeout_ms=0)


@pytest.mark.parametrize("test_checker", [False, True], ids=["plain", "self-test"])
def test_checked_run_rejects_a_non_positive_timeout_before_the_model_runs(
    tmp_path: Path, test_checker: bool
) -> None:
    script, checker = _checked_pair(tmp_path)

    with patch("openconstraint_mcp.pyexec.core.run_cpsat_python_file") as fake_run:
        with pytest.raises(ValueError, match="script_timeout_ms must be positive"):
            run_cpsat_python_file_checked(
                script, checker, script_timeout_ms=0, test_checker=test_checker
            )

    fake_run.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"test_checker": True, "checker_timeout_ms": 30_000},
        {"test_checker": True, "script_timeout_ms": 600_000},
        # The derived checker timeout is the BASELINE checker's budget too, so a
        # model timeout that squeezes it under the floor must reject rather than
        # let an opt-in probe time out the primary verdict.
        {"test_checker": True, "script_timeout_ms": 65_000},
    ],
    ids=[
        "explicit-checker-timeout",
        "model-leaves-no-checker-budget",
        "model-leaves-a-checker-budget-under-the-floor",
    ],
)
def test_checked_run_rejects_an_over_budget_self_test_before_the_model_runs(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    script, checker = _checked_pair(tmp_path)

    with patch("openconstraint_mcp.pyexec.core.run_cpsat_python_file") as fake_run:
        with pytest.raises(ValueError, match="MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS"):
            run_cpsat_python_file_checked(script, checker, **kwargs)

    fake_run.assert_not_called()


def test_checked_run_without_a_self_test_has_no_wall_clock_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain checked run is two children and keeps its historical freedom to
    # ask for the solve time the problem needs; capping the synchronous path is
    # a separate decision that has not been taken.
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker, script_timeout_ms=600_000)

    assert result.checker is not None


def _assert_no_child_spawned(script: Path, checker: Path, match: str) -> None:
    """Both spawn helpers are mocked: a rejection must reach neither."""
    with (
        patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen,
        patch("openconstraint_mcp.pyexec.core.execute_child") as fake_execute,
        patch("openconstraint_mcp.pyexec.checker.execute_child") as fake_checker_execute,
    ):
        with pytest.raises(ValueError, match=match):
            run_cpsat_python_file_checked(script, checker)
    fake_popen.assert_not_called()
    fake_execute.assert_not_called()
    fake_checker_execute.assert_not_called()


def test_checked_run_invalid_checker_path_spawns_nothing(tmp_path: Path) -> None:
    script, _ = _checked_pair(tmp_path)
    _assert_no_child_spawned(script, tmp_path / "nope.py", r"checker_path does not exist")


def test_checked_run_invalid_script_path_spawns_nothing(tmp_path: Path) -> None:
    _, checker = _checked_pair(tmp_path)
    _assert_no_child_spawned(tmp_path / "nope.py", checker, r"script_path does not exist")


def test_inline_run_spawn_failure_returns_structured_error(tmp_path: Path) -> None:
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(7, "Argument list too long"),
    ):
        result = run_cpsat_python("print(1)", script_timeout_ms=1000)
    assert result.status == "error"


def test_spawn_failure_reports_no_return_code(tmp_path: Path) -> None:
    # No child existed, so there is no exit status to report — never a synthesized code.
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(7, "Argument list too long"),
    ):
        result = run_cpsat_python("print(1)", script_timeout_ms=1000)
    assert result.return_code is None


def test_spawn_failure_surfaces_the_os_error_in_stderr() -> None:
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(7, "Argument list too long"),
    ):
        result = run_cpsat_python("print(1)", script_timeout_ms=1000)
    assert "failed to start the Python child process" in result.stderr
    assert "Argument list too long" in result.stderr


def test_file_run_spawn_failure_returns_structured_error(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print(1)", encoding="utf-8")
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(24, "Too many open files"),
    ):
        result = run_cpsat_python_file(script, script_timeout_ms=1000)
    assert result.status == "error"
    assert "Too many open files" in result.stderr


def test_spawn_failure_result_carries_a_diagnostic() -> None:
    # The error path must be as inspectable as any other result the tools return.
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(12, "Cannot allocate memory"),
    ):
        result = run_cpsat_python("print(1)", script_timeout_ms=1000)
    assert result.diagnostic is not None


# --- run_cpsat_python_file_checked: checker self-test ----------------------------

# A solution the mutation generator can mutate every way: a finite objective
# (from `_checked_result`) plus a non-empty list of objects with an int field.
_MUTABLE_SOLUTION = {"tasks": [{"start": 0}, {"start": 5}]}


def _patch_checker_test(
    monkeypatch: pytest.MonkeyPatch,
    run_result: CpsatPythonResult,
    verdict: Callable[[CpsatPythonResult], str],
) -> list[CpsatPythonResult]:
    """Stub the model run and grade each checker call by the payload it receives.

    ``verdict`` maps the ``CpsatPythonResult`` handed to the checker onto a
    checker status, so a test can accept the real solution and reject a chosen
    mutation. Returns the log of results the checker was called with.
    """
    # These tests isolate mutation/report behavior; admission has dedicated
    # coverage above.
    monkeypatch.setattr("openconstraint_mcp.pyexec.core.MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS", 10**9)
    seen: list[CpsatPythonResult] = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_cpsat_python_file",
        lambda script, **kw: run_result,
    )

    def _fake_run_checker_file(checker: Path, result: Any, **kw: Any) -> CpsatCheckerReport:
        seen.append(result)
        return _checker_report(verdict(result))

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_checker_file", _fake_run_checker_file)
    return seen


def _accept_everything(result: CpsatPythonResult) -> str:
    return "accepted"


def _reject_a_dropped_task(result: CpsatPythonResult) -> str:
    """A checker that grades task count: only `element_dropped` is rejected."""
    tasks = (result.solution or {}).get("tasks", [])
    return "accepted" if len(tasks) >= 2 else "rejected"


def test_a_checked_run_that_did_not_opt_in_reports_no_checker_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.checker_test is None


def test_a_checked_run_that_did_not_opt_in_spawns_exactly_one_checker_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    seen = _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    run_cpsat_python_file_checked(script, checker)

    assert len(seen) == 1


def test_one_checker_child_runs_per_applied_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every mutation applies for this payload, so the cost is the baseline plus four.
    script, checker = _checked_pair(tmp_path)
    seen = _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert len(seen) == 5


def test_every_mutation_is_reported_as_its_own_row_in_a_fixed_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert [m.name for m in result.checker_test.mutations] == [
        "objective_perturbed",
        "element_dropped",
        "element_duplicated",
        "numeric_field_perturbed",
    ]


def test_only_rejected_mutants_are_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _reject_a_dropped_task,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert result.checker_test.rejected_count == 1


def test_errored_mutants_remain_graded_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A checker fault against a mutant is an indeterminate graded outcome, not
    # a skipped probe.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        lambda result: (
            "accepted"
            if (result.solution, result.objective) == (_MUTABLE_SOLUTION, 10)
            else "error"
        ),
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert [m.status for m in result.checker_test.mutations if m.status is not None] == [
        "error"
    ] * 4


def test_a_checker_that_swallows_every_mutation_reports_them_as_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The signal the probe exists to produce: a rubber-stamp checker accepts
    # every corruption it is handed, and `accepted_count` says so directly.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch, _checked_result("optimal", solution=_MUTABLE_SOLUTION), _accept_everything
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert result.checker_test.accepted_count == 4


def test_a_faulted_mutation_probe_keeps_other_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = 0

    class _FaultsOnTheSecondMutant(CpsatPythonResult):
        def model_copy(self, **kwargs: Any) -> CpsatPythonResult:
            nonlocal built
            built += 1
            if built == 2:
                raise RuntimeError("mutant build exploded")
            return super().model_copy(**kwargs)

    script, checker = _checked_pair(tmp_path)
    base = _checked_result("optimal", solution=_MUTABLE_SOLUTION)
    _patch_checker_test(
        monkeypatch,
        _FaultsOnTheSecondMutant(**base.model_dump()),
        lambda result: "accepted" if result.solution == _MUTABLE_SOLUTION else "rejected",
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert result.checker_test.rejected_count == 2
    assert result.checker_test.mutations[1].skipped_reason == (
        "mutation probe failed: RuntimeError: mutant build exploded"
    )


def _deeply_nested_solution() -> dict:
    """A solution whose nesting outruns `copy.deepcopy` but nothing else.

    Built in Python rather than decoded from stdout, for the same reason as
    `test_envelope_violation_walks_nesting_deeper_than_the_recursion_limit`: the
    JSON decoder's own ceiling is a C-stack artifact that moves between
    interpreters, so routing this through the transport would pin the test to a
    number that can drift. `deepcopy` recurses in Python at ~2 frames per level,
    so a depth of the whole recursion limit overruns it whatever that limit is.
    """
    node: list = []
    inner = node
    for _ in range(sys.getrecursionlimit()):
        nested: list = []
        inner.append(nested)
        inner = nested
    return {"tasks": [node, node]}


def test_a_solution_too_deep_to_deepcopy_does_not_void_the_checked_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Generation runs before any mutation row exists, so the per-mutation guard
    # cannot cover it. Without its own boundary the `RecursionError` from
    # `generate_mutations`' `deepcopy` escapes `run_cpsat_python_file_checked`
    # entirely, costing the caller a finished model result and an accepted
    # baseline in exchange for an opt-in diagnostic.
    script, checker = _checked_pair(tmp_path)
    solution = _deeply_nested_solution()
    _patch_checker_test(
        monkeypatch, _checked_result("optimal", solution=solution), _accept_everything
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.status == "optimal"
    assert result.checker is not None
    assert result.checker.status == "accepted"


def test_a_generation_fault_reports_every_mutation_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The row set keeps its fixed shape even when no mutation was ever built, so
    # a client that indexes the report by name is not broken by the degraded path.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_deeply_nested_solution()),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert [m.name for m in result.checker_test.mutations] == list(CPSAT_MUTATION_NAMES)
    assert all(
        m.status is None and (m.skipped_reason or "").startswith("mutation generation failed:")
        for m in result.checker_test.mutations
    )


def test_a_generation_fault_costs_no_mutant_checker_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    seen = _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_deeply_nested_solution()),
        _accept_everything,
    )

    run_cpsat_python_file_checked(script, checker, test_checker=True)

    # The baseline only: every mutation was skipped before it could be run.
    assert len(seen) == 1


def test_a_mutation_that_cannot_apply_is_skipped_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A flat numeric solution supports the numeric mutation but not list mutations.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _accept_everything
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    skipped = [m for m in result.checker_test.mutations if m.status is None]
    assert [m.name for m in skipped] == [
        "element_dropped",
        "element_duplicated",
    ]


def test_a_skipped_mutation_costs_no_checker_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    seen = _patch_checker_test(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _accept_everything
    )

    run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert len(seen) == 3


def test_the_number_of_mutations_graded_reflects_the_solution_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `_accept_everything` grades every mutation it receives as "accepted", so
    # `accepted_count` here doubles as "how many mutations this solution shape
    # produced" — a flat `{"x": 1}` supports only the objective and numeric
    # mutations, not the two element mutations (no list to target). One shape is
    # enough at THIS layer: what each shape yields is `mutation.py`'s contract,
    # covered per shape in `tests/pyexec/test_mutation.py`; what the orchestrator
    # owes is that the shape's mutation count reaches the checker unaltered.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _accept_everything
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert result.checker_test.accepted_count == 2


def test_a_solution_with_nothing_to_mutate_reports_zero_of_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `rejected_count: 0, accepted_count: 0` alone cannot distinguish "the
    # checker tolerated every corruption" from "nothing was corruptible"; this
    # payload — no list, no top-level int, no objective — is the second case,
    # confirmed by every row in `mutations` carrying a `skipped_reason`.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution={"note": "x"}, objective=None),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    assert (result.checker_test.rejected_count, result.checker_test.accepted_count) == (0, 0)
    assert all(m.skipped_reason is not None for m in result.checker_test.mutations)


def test_a_graded_row_projects_the_mutant_verdict_without_its_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Four mutant reports' worth of stdout/stderr/details would flood a client;
    # the row keeps only what the probe is asked to report.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _reject_a_dropped_task,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    rejected = [m for m in result.checker_test.mutations if m.status == "rejected"]
    assert [(m.name, m.errors, m.duration_ms) for m in rejected] == [
        ("element_dropped", ["nope"], 1)
    ]


def test_a_graded_row_caps_oversized_checker_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    baseline = _checked_result("optimal", solution=_MUTABLE_SOLUTION)
    oversized_errors = ["x" * (_MUTATION_ERRORS_MAX_BYTES + 1), "second error"]
    monkeypatch.setattr("openconstraint_mcp.pyexec.core.MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS", 10**9)
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_cpsat_python_file", lambda script, **kw: baseline
    )

    def _checker(_path: Path, result: CpsatPythonResult, **kw: Any) -> CpsatCheckerReport:
        if (result.solution, result.objective) == (_MUTABLE_SOLUTION, 10):
            return _checker_report("accepted")
        report = _checker_report("rejected")
        report.errors = oversized_errors
        return report

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_checker_file", _checker)

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is not None
    errors = result.checker_test.mutations[0].errors
    assert (
        len(json.dumps(errors, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        <= _MUTATION_ERRORS_MAX_BYTES
    )
    assert errors[-1] == "... checker errors truncated (2 affected)"


def test_capped_errors_keep_whole_rows_before_the_one_that_overran() -> None:
    """Rows that fit are carried over byte-identical; only the overrunning one is cut."""
    errors = [f"row {index:03d} " + "e" * 90 for index in range(200)]

    compact = _compact_mutation_errors(errors)

    kept = compact[:-2]  # every row but the truncated prefix and the marker
    assert kept and kept == errors[: len(kept)]


def test_capped_errors_truncate_the_overrunning_row_to_a_prefix() -> None:
    """The row that broke the budget is kept as a non-empty proper prefix, not dropped."""
    errors = ["e" * 100] * 200

    compact = _compact_mutation_errors(errors)

    assert compact[-2] in {"e" * length for length in range(1, 100)}


def test_capped_errors_drop_a_row_that_cannot_fit_even_one_character() -> None:
    """With room for the marker but not one more character, the row vanishes entirely.

    Sizing the fillers so the budget lands in that four-byte window is the whole
    point: a retained character costs its own quotes and separator, so this is
    the one path where the marker replaces a row instead of following a prefix
    of it.
    """
    marker = "... checker errors truncated (1 affected)"
    filler = "x" * 12
    # A compact JSON array of n equal ASCII strings costs n * (len(item) + 3) + 1
    # bytes, so this is the most fillers that still leave room for the marker.
    count = (_MUTATION_ERRORS_MAX_BYTES - len(marker) - 4) // (len(filler) + 3)
    errors = [filler] * count + ["y" * 5000]

    compact = _compact_mutation_errors(errors)

    assert compact == [*errors[:count], marker]


def test_the_report_does_not_repeat_the_baseline_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The accepted baseline is already returned once, in full, as `checker`.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker is not None and result.checker.status == "accepted"
    assert "baseline" not in result.model_dump()["checker_test"]


@pytest.mark.parametrize("baseline_verdict", ["rejected", "error", "timeout"])
def test_a_non_accepted_baseline_produces_no_report(
    baseline_verdict: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-accepted verdict of ANY kind leaves nothing to test the checker
    # against: `rejected` graded the real solution and failed it, `error` and
    # `timeout` reached no verdict at all. In every case the mutants' verdicts
    # would be evidence about a checker that never worked on the real answer.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        lambda result: baseline_verdict,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is None


def test_a_rejected_baseline_spawns_no_mutant_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    seen = _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        lambda result: "rejected",
    )

    run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert len(seen) == 1


def test_a_checker_that_never_ran_produces_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch, _checked_result("infeasible", solution=None), _accept_everything
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.checker_test is None


def test_the_mutants_leave_the_runs_own_solution_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The self-test observes copies, never the run's own result.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.solution == {"tasks": [{"start": 0}, {"start": 5}]}


def test_the_mutants_leave_the_runs_own_status_and_objective_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert (result.status, result.objective) == ("optimal", 10)


def test_mutants_reuse_the_baseline_checker_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls: list[tuple[Path, str | None, int, Any]] = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_cpsat_python_file",
        lambda script_path, **kw: _checked_result("optimal", solution=_MUTABLE_SOLUTION),
    )

    def _fake(checker_path: Path, result: Any, **kw: Any) -> CpsatCheckerReport:
        calls.append((checker_path, kw["problem"], kw["timeout_ms"], kw["tracker"]))
        return _checker_report("accepted")

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_checker_file", _fake)
    tracker = _SpyTracker()
    problem = '{"jobs": []}'

    run_cpsat_python_file_checked(
        script,
        checker,
        problem=problem,
        checker_timeout_ms=777,
        tracker=tracker,
        test_checker=True,
    )

    assert calls == [(checker.resolve(), problem, 777, tracker)] * 5


def _run_with_a_failing_mutant_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CpsatPythonCheckedResult:
    """Check a run whose baseline succeeds but whose every MUTANT child raises.

    A spawn/temp-file fault on a mutant is the fault the per-mutant boundary
    absorbs; the shared setup lets each consequence be asserted on its own.
    """
    script, checker = _checked_pair(tmp_path)
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_cpsat_python_file",
        lambda script_path, **kw: _checked_result("optimal", solution=_MUTABLE_SOLUTION),
    )
    calls: list[int] = []

    def _fake(checker_path: Path, result: Any, **kw: Any) -> CpsatCheckerReport:
        calls.append(1)
        if len(calls) > 1:
            raise OSError("no space left on device")
        return _checker_report("accepted")

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_checker_file", _fake)

    return run_cpsat_python_file_checked(
        script, checker, checker_timeout_ms=1_000, test_checker=True
    )


def test_a_mutant_infrastructure_error_leaves_the_runs_status_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_with_a_failing_mutant_child(tmp_path, monkeypatch)

    assert result.status == "optimal"


def test_a_mutant_infrastructure_error_keeps_the_accepted_baseline_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_with_a_failing_mutant_child(tmp_path, monkeypatch)

    assert result.checker is not None
    assert result.checker.status == "accepted"


def test_mutants_that_all_faulted_are_reported_as_errored_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The per-mutation rows stay the detailed signal even with no diagnostic.
    result = _run_with_a_failing_mutant_child(tmp_path, monkeypatch)

    assert result.checker_test is not None
    assert [m.status for m in result.checker_test.mutations if m.status is not None] == [
        "error"
    ] * 4


def test_zero_rejected_generic_mutations_are_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mutations are domain-agnostic and not known-invalid, so a checker that
    # tolerates all of them may still be correct: the run stays diagnostic-free.
    # That this setup yields zero rejections is
    # test_a_checker_that_swallows_every_mutation_reports_them_as_tolerated.
    script, checker = _checked_pair(tmp_path)
    _patch_checker_test(
        monkeypatch,
        _checked_result("optimal", solution=_MUTABLE_SOLUTION),
        _accept_everything,
    )

    result = run_cpsat_python_file_checked(script, checker, test_checker=True)

    assert result.diagnostic is None
