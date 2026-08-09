"""Integration tests for pyexec/core.py — runs real ortools scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.core import (
    VERIFIED_STATUSES,
    run_cpsat_python,
    run_cpsat_python_file,
    run_cpsat_python_file_checked,
)

_EXAMPLES = Path(__file__).parent.parent / "fixtures" / "cpsat_python"


@pytest.mark.integration
def test_run_cpsat_python_solves_assignment_example() -> None:
    source = (_EXAMPLES / "assignment.py").read_text()
    result = run_cpsat_python(source)

    assert result.status in VERIFIED_STATUSES
    assert result.solution is not None
    assert len(result.solution) > 0
    assert result.timed_out is False
    assert result.truncated is False


@pytest.mark.integration
def test_run_cpsat_python_solves_scheduling_example() -> None:
    source = (_EXAMPLES / "scheduling.py").read_text()
    result = run_cpsat_python(source)

    assert result.status in VERIFIED_STATUSES
    assert result.solution is not None
    assert "makespan" in result.solution
    assert result.timed_out is False
    assert result.truncated is False


@pytest.mark.integration
def test_run_cpsat_python_timeout_recovers_unflushed_partial() -> None:
    # The intermediate JSON is printed WITHOUT flush=True: it only survives the
    # timeout kill because the executor launches the child with -u (unbuffered).
    # Drop the -u and this returns solution=None — the test proves it is load-bearing.
    source = (
        "import json, time\n"
        "print(json.dumps({'status': 'feasible', 'objective': 5, 'solution': {'x': 2}}))\n"
        "time.sleep(30)\n"
    )
    result = run_cpsat_python(source, script_timeout_ms=300)

    assert result.timed_out is True
    assert result.status == "timeout"
    assert result.solution == {"x": 2}
    assert result.objective == 5
    # The child is killed (SIGTERM); its exit code (-15 on POSIX) must not leak —
    # the contract reports null on timeout. This asserts the override over a real kill.
    assert result.return_code is None


@pytest.mark.integration
def test_run_cpsat_python_file_resolves_relative_sibling_file(tmp_path: Path) -> None:
    # The file tool's reason to exist: the script runs in its own directory, so a
    # relative open() of a sibling data file resolves. Inline run_cpsat_python runs
    # in a throwaway tempdir where this read would fail, so this proves cwd=parent.
    (tmp_path / "bound.txt").write_text("7", encoding="utf-8")
    script = tmp_path / "model.py"
    script.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "from ortools.sat.python import cp_model\n"
        "bound = int(Path('bound.txt').read_text())\n"
        "model = cp_model.CpModel()\n"
        "x = model.new_int_var(0, bound, 'x')\n"
        "model.maximize(x)\n"
        "solver = cp_model.CpSolver()\n"
        "status = solver.solve(model)\n"
        "status_map = {0: 'unknown', 1: 'error', 2: 'infeasible', 3: 'feasible', 4: 'optimal'}\n"
        "print(json.dumps({\n"
        "    'status': status_map.get(status, 'error'),\n"
        "    'objective': solver.objective_value,\n"
        "    'solution': {'x': solver.value(x)},\n"
        "}))\n",
        encoding="utf-8",
    )

    result = run_cpsat_python_file(script)

    assert result.status in VERIFIED_STATUSES
    assert result.solution == {"x": 7}


@pytest.mark.integration
def test_run_cpsat_python_file_forwards_args_to_child_argv(tmp_path: Path) -> None:
    # The mocked unit test only proves the command list was built; this proves
    # the values actually land in a real interpreter's sys.argv[1:].
    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({\n"
        "    'status': 'optimal',\n"
        "    'objective': None,\n"
        "    'solution': {'argv': sys.argv[1:]},\n"
        "}))\n",
        encoding="utf-8",
    )

    result = run_cpsat_python_file(script, args=["--flag", "value"])

    assert result.solution is not None
    assert result.solution["argv"] == ["--flag", "value"]


@pytest.mark.integration
def test_checker_self_test_reports_rejections_through_real_children(tmp_path: Path) -> None:
    # The mocked unit tests grade a stubbed `run_checker_file`; this proves the
    # whole probe survives real children — the mutant payload really reaches a
    # separate interpreter through the temp payload file, and its verdict really
    # comes back through the stdout envelope.
    script = tmp_path / "model.py"
    script.write_text(
        "import json\n"
        "from ortools.sat.python import cp_model\n"
        "model = cp_model.CpModel()\n"
        "xs = [model.new_int_var(0, 5, f'x{i}') for i in range(2)]\n"
        "model.add(xs[0] + xs[1] == 5)\n"
        "solver = cp_model.CpSolver()\n"
        "solver.parameters.max_time_in_seconds = 5\n"
        "solver.solve(model)\n"
        "print(json.dumps({\n"
        "    'status': 'optimal',\n"
        "    'objective': 5,\n"
        "    'solution': {'items': [{'value': solver.value(x)} for x in xs]},\n"
        "}))\n",
        encoding="utf-8",
    )
    # A feasibility-only checker: it grades the item list and ignores the
    # objective, so it tolerates `objective_perturbed` and rejects the other
    # three.
    checker = tmp_path / "checker.py"
    checker.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.loads(Path(sys.argv[1]).read_text())\n"
        "items = (payload.get('solution') or {}).get('items')\n"
        "errors = []\n"
        "if not isinstance(items, list) or len(items) != 2:\n"
        "    errors.append('expected exactly 2 items')\n"
        "elif sum(item['value'] for item in items) != 5:\n"
        "    errors.append('item values do not sum to 5')\n"
        "print(json.dumps({\n"
        "    'status': 'rejected' if errors else 'accepted',\n"
        "    'errors': errors,\n"
        "}))\n",
        encoding="utf-8",
    )

    result = run_cpsat_python_file_checked(
        script, checker, script_timeout_ms=15_000, checker_timeout_ms=10_000, test_checker=True
    )

    assert result.checker_test is not None
    assert result.checker_test.rejected_count == 3
