import contextlib
import json
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from examples.online_printing_shop.audit_instance import audit_instance
from examples.online_printing_shop.checker import (
    _required_processing as _checker_required_processing,
)
from examples.online_printing_shop.checker import check_payload
from examples.online_printing_shop.models import (
    _num_workers,
    _required_processing,
    _solver_config,
    _solver_time_limit_seconds,
    parse_input,
    read_input,
    serialize_solution,
    solve,
)
from openconstraint_mcp.server import create_mcp_server

ROOT = Path(__file__).parents[2]
EXAMPLE_DIR = ROOT / "examples" / "online_printing_shop"
DATA_DIR = EXAMPLE_DIR / "data"
INSTANCE_PATH = DATA_DIR / "data_sops1.json"


def load_instance() -> dict[str, Any]:
    return read_input(INSTANCE_PATH)


def test_sops1_instance_passes_semantic_validation_without_normalization() -> None:
    raw = load_instance()

    validated = parse_input(raw)

    # Python mode, not JSON mode: theta is a Decimal, and JSON mode would render
    # it as a string, which is itself a normalization this test rules out.
    assert validated.model_dump(mode="python", exclude_none=True) == raw


def test_sops1_model_proves_the_known_optimum() -> None:
    result = solve(parse_input(load_instance()))

    assert (result.status, result.objective) == ("optimal", 274)


def _checker_payload(solver_status: str) -> dict[str, Any]:
    """A checker payload built from a real solve of data_sops1.json, so its
    schedule is genuinely well-formed and feasible."""
    envelope = serialize_solution(solve(parse_input(load_instance())))
    return {
        "problem": INSTANCE_PATH.read_text(encoding="utf-8"),
        "solution": envelope["solution"],
        "objective": envelope["objective"],
        "solver_status": solver_status,
    }


def test_checker_accepts_timeout_with_valid_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout with a recovered, well-formed schedule asserts no optimality
    claim, so the checker must grade it like any other feasible solution."""
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)
    result = check_payload(_checker_payload("timeout"))

    assert result["status"] == "accepted", result["errors"]


def test_checker_rejects_timeout_with_infeasible_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the checker actually grades a timeout payload rather than waving
    it through: an infeasible schedule under solver_status="timeout" must still
    be "rejected", not "accepted" or "error"."""
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)
    payload = _checker_payload("timeout")
    payload["solution"]["schedule"][0]["machine"] = "no-such-machine"

    result = check_payload(payload)

    assert result["status"] == "rejected"


def test_checker_errors_on_malformed_solution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged behavior: a solution that is not a well-formed schedule claim
    stays "error" regardless of solver_status."""
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)
    payload = _checker_payload("optimal")
    payload["solution"] = {"makespan": payload["solution"]["makespan"]}

    result = check_payload(payload)

    assert result["status"] == "error"


def test_missing_successor_reference_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["1"]["successors"][0] = "missing-operation"

    with pytest.raises(ValidationError, match="unknown successor"):
        parse_input(raw)


def test_cyclic_precedence_graph_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["4"]["successors"] = ["1"]

    with pytest.raises(ValidationError, match="must be acyclic"):
        parse_input(raw)


def test_ineligible_fixed_machine_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["6"]["fixed"]["machine"] = "2"

    with pytest.raises(ValidationError, match="fixed machine is not eligible"):
        parse_input(raw)


def test_invalid_unavailability_interval_is_rejected() -> None:
    raw = load_instance()
    raw["machines"]["1"]["unavailability"][0] = {"start": 8, "end": 8}

    with pytest.raises(ValidationError, match="end must be greater than start"):
        parse_input(raw)


def test_unknown_field_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["4"]["objective"] = "makespan"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_input(raw)


def test_incomplete_setup_matrix_is_rejected() -> None:
    raw = load_instance()
    del raw["machines"]["1"]["setup_times"]["transitions"]["1"]["4"]

    with pytest.raises(ValidationError, match="must cover every other eligible operation"):
        parse_input(raw)


def _write_instance_with_theta(tmp_path: Path, theta_literal: str) -> Path:
    """Write a one-operation instance whose theta appears verbatim in the JSON."""

    path = tmp_path / "instance.json"
    path.write_text(
        f"""{{
          "format": "openconstraint.ops.instance",
          "format_version": "1.0",
          "provenance": {{"source": "test", "license": "test"}},
          "machines": {{
            "1": {{
              "unavailability": [],
              "setup_times": {{"first": {{"1": 0}}, "transitions": {{}}}}
            }}
          }},
          "operations": {{
            "1": {{
              "job": "1",
              "successors": [],
              "machine_options": {{"1": {{"processing_time": 2}}}},
              "release_time": 0,
              "theta": {theta_literal}
            }}
          }}
        }}""",
        encoding="utf-8",
    )
    return path


def test_theta_beyond_float_precision_survives_reading(tmp_path: Path) -> None:
    path = _write_instance_with_theta(tmp_path, "0.50000000000000000001")

    theta = parse_input(read_input(path)).operations["1"].theta

    assert theta == Decimal("0.50000000000000000001")


def test_theta_beyond_float_precision_keeps_the_checker_ceiling(tmp_path: Path) -> None:
    # A float round-trip would collapse this theta to 0.5 and require only 1
    # tick, while checker.py parses the literal as a Decimal and requires 2.
    path = _write_instance_with_theta(tmp_path, "0.50000000000000000001")

    operation = parse_input(read_input(path)).operations["1"]

    assert _required_processing(operation.theta, 2) == 2


def test_theta_beyond_context_precision_keeps_the_model_ceiling(tmp_path: Path) -> None:
    # 29 significant digits: Decimal multiplication at the default 28-digit
    # context precision rounds the product down onto 1, but the exact ceiling is 2.
    path = _write_instance_with_theta(tmp_path, "0.50000000000000000000000000001")

    operation = parse_input(read_input(path)).operations["1"]

    assert _required_processing(operation.theta, 2) == 2


def test_checker_ceiling_matches_the_model_beyond_context_precision() -> None:
    # The checker is an independent re-derivation; it must not repeat a rounding
    # the model avoids, or it would accept a schedule that violates precedence.
    theta = Decimal("0.50000000000000000000000000001")

    assert _checker_required_processing(theta, 2) == _required_processing(theta, 2)


def test_required_processing_ignores_the_active_decimal_context() -> None:
    # Context precision is process-global mutable state that neither call site
    # sets, so the ceiling must not depend on it.
    theta = Decimal("0.50000000000000000000000000001")

    with localcontext() as context:
        context.prec = 6
        result = _required_processing(theta, 2)

    assert result == 2


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> None:
    """Point the config env var at a JSON file, the way run_cpsat_python_* does."""

    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", str(path))


def test_no_config_file_yields_an_empty_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)

    assert _solver_config() == {}


def test_no_config_file_leaves_the_solve_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)

    assert _solver_time_limit_seconds(_solver_config()) is None


def test_config_without_a_time_limit_leaves_the_solve_unbounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch, {"num_workers": 4})

    assert _solver_time_limit_seconds(_solver_config()) is None


def test_config_time_limit_is_read_as_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch, {"solver_time_limit_seconds": 20})

    assert _solver_time_limit_seconds(_solver_config()) == 20.0


def test_no_config_file_keeps_the_single_reproducible_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)

    assert _num_workers(_solver_config()) == 1


def test_config_without_workers_keeps_the_single_reproducible_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch, {"solver_time_limit_seconds": 20})

    assert _num_workers(_solver_config()) == 1


def test_unbounded_solve_streams_recoverable_incumbents(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a self-imposed limit the child can only be stopped by a timeout
    # kill, and these intermediate blocks are all the executor can recover.
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)

    solve(parse_input(load_instance()))

    assert capsys.readouterr().out.strip() != ""


def test_time_limited_solve_still_streams_recoverable_incumbents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CP-SAT limit bounds SEARCH only — not input parsing, model building, or
    # serialization — and the script cannot see the executor's own deadline, so it
    # can never prove it will exit first. Suppressing the stream here loses every
    # solution CP-SAT found whenever the limit does not fit inside the executor
    # budget (see test_search_limit_above_the_script_timeout_still_recovers).
    _write_config(tmp_path, monkeypatch, {"solver_time_limit_seconds": 30})

    solve(parse_input(load_instance()))

    assert capsys.readouterr().out.strip() != ""


def test_solve_callback_honors_the_intermediate_byte_budget(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    budget: int = 4 * 1024
    monkeypatch.delenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", raising=False)
    monkeypatch.setattr(
        "examples.online_printing_shop.models._MAX_INTERMEDIATE_OUTPUT_BYTES", budget
    )

    solve(parse_input(load_instance()))

    output: str = capsys.readouterr().out
    assert 0 < len(output.encode("utf-8")) <= budget


def test_config_workers_raise_the_cpsat_portfolio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # data_lops1.json finds no incumbent on one worker; the caller needs this key
    # to reach a checkable solution at all.
    _write_config(tmp_path, monkeypatch, {"solver_time_limit_seconds": 20, "num_workers": 8})

    assert _num_workers(_solver_config()) == 8


def test_float_theta_is_rejected_rather_than_silently_rounded() -> None:
    raw = load_instance()
    raw["operations"]["1"]["theta"] = 0.58

    with pytest.raises(ValidationError, match="Decimal"):
        parse_input(raw)


def test_integer_theta_is_accepted_as_an_exact_decimal(tmp_path: Path) -> None:
    path = _write_instance_with_theta(tmp_path, "1")

    theta = parse_input(read_input(path)).operations["1"].theta

    assert theta == Decimal(1)


def test_boolean_theta_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["1"]["theta"] = True

    with pytest.raises(ValidationError, match="Decimal"):
        parse_input(raw)


def test_legacy_instance_audit_accepts_complete_materialization() -> None:
    upstream = {
        "resources": [
            {
                "id": 1,
                "setup_size": [2, 3],
                "setup_color": 4,
                "setup_varnish": 5,
                "availability": [0, 10, 12, 20],
            }
        ],
        "jobs": [
            {
                "id": 1,
                "topology": [
                    {
                        "id": 1,
                        "starting": -1,
                        "release": 0,
                        "overlap": 1.0,
                        "size": 1,
                        "color": 1,
                        "varnish": 1,
                        "resources": [1],
                        "time": [7],
                        "sucessors": [],
                    }
                ],
            }
        ],
    }
    local = {
        "machines": {
            "1": {
                "unavailability": [{"start": 10, "end": 12}],
                "setup_times": {"first": {"1": 12}, "transitions": {}},
            }
        },
        "operations": {
            "1": {
                "job": "1",
                "successors": [],
                "machine_options": {"1": {"processing_time": 7}},
                "release_time": 0,
                "theta": 1.0,
            }
        },
    }

    assert audit_instance(upstream, local) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sops1_model_and_checker_reach_the_known_optimum_through_mcp() -> None:
    mcp = create_mcp_server("full")

    call_result = await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": str(EXAMPLE_DIR / "models.py"),
            "checker_path": str(EXAMPLE_DIR / "checker.py"),
            "args": ["data_sops1.json"],
            "problem": "data_sops1.json",
            "script_timeout_ms": 30_000,
            "test_checker": True,
        },
    )
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content

    assert result["status"] == "optimal"
    assert result["objective"] == 274
    assert result["checker"]["status"] == "accepted", result["checker"]["errors"]
    assert result["checker_test"]["rejected_count"] == 4
    assert result["checker_test"]["accepted_count"] == 0

    payload = json.loads(result["stdout"].strip().splitlines()[-1])
    assert payload.keys() == {"status", "objective", "solution", "best_objective_bound"}
    schedule = payload["solution"]["schedule"]
    assert {entry["operation"] for entry in schedule} == set(load_instance()["operations"])
    fixed = next(entry for entry in schedule if entry["operation"] == "6")
    assert (fixed["machine"], fixed["start"]) == ("1", 79)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_limit_above_the_script_timeout_still_recovers() -> None:
    # The regression this pins: a CP-SAT search limit ABOVE the executor's own
    # deadline leaves the child tree-killed mid-search, so the final envelope
    # main() would print never happens. The streamed incumbents are then the only
    # record of what CP-SAT found. Nothing validates the two limits against each
    # other, and the script cannot see the executor's, so gating the stream on
    # "a limit was set" reported timeout_no_incumbent and discarded a schedule the
    # solver had already proven feasible.
    mcp = create_mcp_server("full")

    call_result = await mcp.call_tool(
        "run_cpsat_python_file",
        {
            "script_path": str(EXAMPLE_DIR / "models.py"),
            "args": ["data_mops1.json"],
            "script_timeout_ms": 15_000,
            "config": {"solver_time_limit_seconds": 300, "num_workers": 8},
        },
    )
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content

    assert result["status"] == "timeout"

    # The premise is that CP-SAT streams at least one feasible envelope inside the
    # executor deadline; a slow or contended runner (Windows CI) can stream none,
    # and there is then no incumbent to recover. Skip on that — but only after
    # proving the stream really was empty. A stream that HAS envelopes while the
    # tool reports no incumbent is the regression above, and must still fail.
    streamed: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        stripped: str = line.strip()
        if not stripped.startswith("{"):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            streamed.append(json.loads(stripped))
    if not any(payload.get("status") == "feasible" for payload in streamed):
        pytest.skip("CP-SAT streamed no feasible envelope within the executor deadline")

    assert result["diagnostic"]["category"] == "timeout_with_incumbent"
    assert result["solution"]["schedule"]
