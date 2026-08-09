import itertools
import json
import os

from ortools.sat.python import cp_model

FANO_LINES = (
    (0, 1, 2),
    (0, 3, 4),
    (0, 5, 6),
    (1, 3, 5),
    (1, 4, 6),
    (2, 3, 6),
    (2, 4, 5),
)
BRANCHING = {
    "AUTOMATIC_SEARCH": cp_model.AUTOMATIC_SEARCH,
    "FIXED_SEARCH": cp_model.FIXED_SEARCH,
    "PORTFOLIO_SEARCH": cp_model.PORTFOLIO_SEARCH,
    "PORTFOLIO_WITH_QUICK_RESTART_SEARCH": cp_model.PORTFOLIO_WITH_QUICK_RESTART_SEARCH,
}


def read_config() -> dict[str, object]:
    path = os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(schedule: list[list[list[int]]]) -> None:
    seen: set[tuple[int, int]] = set()
    for groups in schedule:
        if sorted(p for group in groups for p in group) != list(range(1, 22)):
            raise ValueError("week is not a partition of players 1..21")
        for group in groups:
            if len(group) != 3:
                raise ValueError("group does not have 3 players")
            for pair in itertools.combinations(sorted(group), 2):
                if pair in seen:
                    raise ValueError(f"pair meets twice: {pair}")
                seen.add(pair)
    if len(seen) != 210:
        raise ValueError(f"expected 210 unique pairs, saw {len(seen)}")


cfg = read_config()
incident = {point: [] for point in range(7)}
edge_line = {}
for line_index, line in enumerate(FANO_LINES):
    for point in line:
        incident[point].append(line_index)
    for left, right in itertools.combinations(line, 2):
        edge_line[(left, right)] = line_index
slot = {
    (point, line): incident[point].index(line) for point in range(7) for line in incident[point]
}
perms = list(itertools.permutations(range(3)))
model = cp_model.CpModel()
choices = [
    [model.new_int_var(0, len(perms) - 1, f"choice_{week}_{point}") for point in range(7)]
    for week in range(9)
]
values = {}
for week in range(9):
    for point in range(7):
        for line in incident[point]:
            value = model.new_int_var(0, 2, f"value_{week}_{point}_{line}")
            model.add_element(
                choices[week][point],
                [perm[slot[(point, line)]] for perm in perms],
                value,
            )
            values[(week, point, line)] = value
for left, right in itertools.combinations(range(7), 2):
    line = edge_line[(left, right)]
    codes = []
    for week in range(9):
        code = model.new_int_var(0, 8, f"pair_{week}_{left}_{right}")
        model.add(code == 3 * values[(week, left, line)] + values[(week, right, line)])
        codes.append(code)
    model.add_all_different(codes)
for point in range(7):
    model.add(choices[0][point] == 0)
week_codes = []
for week in range(9):
    code = model.new_int_var(0, len(perms) ** 7 - 1, f"week_code_{week}")
    model.add(code == sum((len(perms) ** point) * choices[week][point] for point in range(7)))
    week_codes.append(code)
for left, right in zip(week_codes, week_codes[1:], strict=False):
    model.add(left < right)
model.add_decision_strategy(
    [choices[week][point] for week in range(9) for point in range(7)],
    cp_model.CHOOSE_MIN_DOMAIN_SIZE,
    cp_model.SELECT_MIN_VALUE,
)
solver = cp_model.CpSolver()
solver.parameters.max_time_in_seconds = float(cfg.get("solver_time_limit_seconds", 60.0))
solver.parameters.num_workers = int(cfg.get("num_workers", 8))
solver.parameters.random_seed = int(
    os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", cfg.get("random_seed", 21))
)
solver.parameters.search_branching = BRANCHING[
    cfg.get("search_branching", "PORTFOLIO_WITH_QUICK_RESTART_SEARCH")
]
solver.parameters.randomize_search = bool(cfg.get("randomize_search", True))
solver.parameters.use_lns = bool(cfg.get("use_lns", True))
solver.parameters.diversify_lns_params = bool(cfg.get("diversify_lns_params", True))
solver.parameters.symmetry_level = int(cfg.get("symmetry_level", 2))
status = solver.solve(model)
if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    print(json.dumps({"status": "unknown", "objective": None, "solution": {}}))
    raise SystemExit
schedule = [[[3 * group + slot + 1 for slot in range(3)] for group in range(7)]]
for week in range(9):
    groups = []
    for line_index, line in enumerate(FANO_LINES):
        groups.append(
            sorted(
                3 * point + solver.value(values[(week, point, line_index)]) + 1 for point in line
            )
        )
    schedule.append(groups)
validate(schedule)
solution = {f"week_{week}": groups for week, groups in enumerate(schedule, start=1)}
print(f"Solver status: {solver.status_name(status)}")
for week, groups in enumerate(schedule, start=1):
    print(
        "Week {}: {}".format(
            week,
            "  ".join(f"[{' '.join(map(str, group))}]" for group in groups),
        )
    )
print(
    json.dumps(
        {
            "status": "optimal" if status == cp_model.OPTIMAL else "feasible",
            "objective": None,
            "solution": solution,
        }
    )
)
