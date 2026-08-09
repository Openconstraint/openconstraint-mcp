"""Prompt templates exposed by the MCP server.

Editorial copy for the server's MCP prompts lives here, separate from the
MCPServer wiring in ``server.py``. This is a leaf module: ``server`` imports it,
and it imports nothing internal.
"""

from __future__ import annotations

# The single CP-SAT output-contract block, spliced verbatim into BOTH the
# backend-neutral prompt and the detailed CP-SAT workflow so the two can never
# drift. It deliberately contains NO braces: every prompt body is `str.format`ted
# with the user's problem text, and a brace-free fragment needs no `{{`/`}}`
# escaping and reaches the client byte-for-byte as written here. It also names no
# full-only tool, because the backend-neutral prompt is served in the core
# profile. Prompts are fetched on demand and carry no byte budget, so the fuller
# wording lives here rather than in the metadata-budgeted tool descriptions.
#
# Everything up to the execution-role bullet is profile-independent. That bullet
# is composed per profile (see `_CPSAT_CONTRACT_ROLE_CORE`), and the full profile
# alone appends the checker mandates after the shared advisory tail.
_CPSAT_OUTPUT_CONTRACT_HEAD = """\
CP-SAT OUTPUT CONTRACT — the transport the server parses, and the form any
checker must be able to grade:
- Emit a FINAL JSON object as the LAST stdout line, carrying all three REQUIRED
  keys: `status` (str), `objective` (a number or null — the key is present
  even for a pure feasibility model), and `solution` (a JSON object; an empty
  object when there is no incumbent). Extra keys are ignored. Same-shaped
  intermediate objects ARE allowed and encouraged — printing one per improved
  solution is what lets the server recover a partial answer when the run hits
  its timeout; only the last one is read as the final result.
- On a CLEAN EXIT, a missing or invalid required key makes the whole run
  `status="error"` with no solution and a `child_process_error` diagnostic
  naming the offending field. On a TIMEOUT the status stays `"timeout"`: the
  malformed partial is discarded rather than recovered, and the drop is
  reported through `rejected_partial_field` / `rejected_partial_reason` in the
  timeout diagnostic's details — look there, not for `child_process_error`.
- Every number anywhere in the payload must be FINITE. `NaN`, `Infinity`, and
  `-Infinity` are rejected in `objective` and at any depth inside `solution`,
  because `json.dumps` emits them as bare `NaN`/`Infinity` tokens that are not
  valid JSON for a strict client. Emit null, or a real number.
- `json.dumps` only serializes a Python object into a STRING that `print`
  sends to stdout. It creates no file and saves nothing. Writing the script's
  `.py` source file is a separate act, and persisting a verified artifact is
  an explicit save step the user has to ask for.
- `solution` must carry the COMPLETE, problem-specific answer: every decision
  value needed to grade it against the problem, keyed so it can be graded
  independently. Never prose, never statistics alone, and never only a path to
  a result file the script wrote. A supplementary `result_file` key is allowed,
  but it can never replace the in-band answer.
- Variants of the SAME problem must share ONE `solution` schema, so one grading
  standard applies to every variant.
"""

# The one profile-dependent clause. CP-SAT checker EXECUTION is full-only —
# `run_cpsat_python_file_checked`, the experiment, save, and job tools all live
# in `_FULL_ONLY_TOOL_NAMES`; core exposes only `run_cpsat_python` and
# `run_cpsat_python_file`, neither of which takes a checker. So the core variant
# must not promise the server will run one, the same core/full split
# `_RUN_CPSAT_PYTHON_FILE_SHAPE_CORE`/`_FULL` already make in the tool
# descriptions. Naming no full-only TOOL is not enough on its own: the core
# leak-guard test matches names, and a promised capability names nothing.
#
# The SHARED HEAD above is bound by the same rule, and that is easy to forget
# because it reads as profile-neutral prose. It may describe a checker as the
# STANDARD a `solution` has to satisfy ("the form any checker must be able to
# grade", "one grading standard applies to every variant") but must never say
# the server runs, supplies, or invokes one — that claim is only true in the
# full profile, and the head reaches core verbatim.
#
# For the same reason the checker MANDATES live in the CP-SAT workflow prompt's
# checker-gate step (7c), which the full profile alone registers — not here. This
# fragment is spliced into `solve_constraint_problem`, which BOTH profiles serve
# and which routes to no checker-taking tool, so a checker mandate here would be
# dead weight in one prompt and a false promise in the other.
_CPSAT_CONTRACT_ROLE_FULL = """\
- You generate and repair the script; the server only executes it and runs the
  checker you supply.
"""

_CPSAT_CONTRACT_ROLE_CORE = """\
- You generate and repair the script; the server only executes it.
"""

# Shared tail of the execution-role bullet: true in both profiles, so it stays
# one string rather than being duplicated into each variant. Its two-space indent
# makes it a CONTINUATION of that bullet, so it must stay the LAST thing in the
# fragment — anything appended after it renders as new top-level bullets, outside
# what "this guidance" refers to.
_CPSAT_CONTRACT_ADVISORY = """\
  This guidance is advisory — the deterministic guarantee starts only when an
  MCP execution tool actually runs the script, so a script you write and never
  run carries no server guarantee at all.
"""

CPSAT_OUTPUT_CONTRACT_GUIDANCE = (
    _CPSAT_OUTPUT_CONTRACT_HEAD + _CPSAT_CONTRACT_ROLE_FULL + _CPSAT_CONTRACT_ADVISORY
)

CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE = (
    _CPSAT_OUTPUT_CONTRACT_HEAD + _CPSAT_CONTRACT_ROLE_CORE + _CPSAT_CONTRACT_ADVISORY
)

# The CP-SAT SCRIPT STRUCTURE block, split by CONCERN into the two fragments
# below: the script's own SHAPE (`CPSAT_SCRIPT_SPINE_GUIDANCE`) and where its
# PROBLEM INSTANCE comes from (`CPSAT_SCRIPT_INPUT_GUIDANCE`). The split is not
# a profile split — unlike the output-contract fragments above, no clause here
# varies by profile, and every route takes both halves through the composed
# `CPSAT_SCRIPT_STRUCTURE_GUIDANCE`. Each half is public rather than `_`-private
# because either one is coherent spliced alone, which a profile *half* of the
# output contract is not.
#
# One layout, taught once, so every route that teaches a layout teaches the same
# one. Both halves are written to be spliced VERBATIM at column 0, never
# re-indented, so presence assertions can stay plain `in` checks wherever they
# land, and they concatenate into ONE contiguous bullet list — neither half may
# open or close with a blank line.
#
# Two constraints they share with the output-contract fragment above. They are
# brace-free, because every host prompt is `str.format`ted with the user's
# problem text and a stray brace surfaces as an unrelated-looking `KeyError`.
# And they name no full-only tool — `run_cpsat_python_file`, never
# `run_cpsat_python_file_checked` — because a route both profiles serve renders
# this text in core, where the checked variant does not exist.
#
# They are also PROSE ONLY, with no code fence: the CP-SAT workflow prompt's two
# fences are its copyable examples, and the example tests both count them and
# reject placeholders inside them.
CPSAT_SCRIPT_SPINE_GUIDANCE = """\
CP-SAT SCRIPT STRUCTURE — the default layout for a generated script, unless
the user asks for a different one:
- Give the script one ordered spine: `read_input()` returns the raw problem
  instance, `parse_input()` turns it into a typed instance record, `solve()`
  builds and solves the model and returns a typed solution record,
  `serialize_solution()` maps that record onto the stdout envelope, and
  `write_output()` prints it. `main()` calls them in that order, and an
  `if __name__ == "__main__":` guard is the only thing that calls `main()`.
- Keep the boundary TYPED: `parse_input()` hands `solve()` a typed instance
  record, `solve()` hands `serialize_solution()` a typed solution record —
  never loose dicts threaded from step to step. `solve()` applies the status
  guards BEFORE it builds the record, so every value field of that solution
  record is either a real value or absent (`None`).
- `serialize_solution()` produces the FINAL stdout JSON object the CP-SAT
  output contract requires: it renames the record's fields, maps an absent
  PROBLEM-SPECIFIC field to an empty JSON object, and passes an absent number
  through as null. Envelope shaping is its whole job: the `status` it prints
  is the one `solve()` already decided.
"""

# The INPUT half: what `read_input()` is allowed to reach for. This is a
# property of the TOOL that runs the script, not of the script's shape, which is
# why it is its own fragment — the spine above is the same under every execution
# tool, while these two bullets are the part that changes with the tool.
CPSAT_SCRIPT_INPUT_GUIDANCE = """\
- NEVER read stdin. The child process runs with stdin closed, so a script that
  calls `input()` or reads `sys.stdin` gets an immediate EOF, prints no
  envelope, and the whole run comes back as an error.
- `read_input()` supplies the PROBLEM INSTANCE only, and which sources it may
  read depends on the tool that runs the script. INLINE execution embeds the
  instance in the script itself: `run_cpsat_python` passes no arguments and
  runs from a fresh temporary directory holding nothing but the script, so
  `sys.argv` carries no entry to read and a relative `open()` finds no file.
  ARGV or RELATIVE-FILE input requires `run_cpsat_python_file` with
  `script_path` and `args`, which runs the script from its own directory.
"""

CPSAT_SCRIPT_STRUCTURE_GUIDANCE = CPSAT_SCRIPT_SPINE_GUIDANCE + CPSAT_SCRIPT_INPUT_GUIDANCE

_SOLVE_CONSTRAINT_PROBLEM_HEAD = """\
You are the MCP client's reasoning model, helping the user solve a constraint
or discrete-optimization problem with openconstraint-mcp. The server calls no
LLM and embeds no agent framework: you write the model or the script, and its
deterministic local tools verify and run it.

Everything runs on the user's machine — MiniZinc through the managed local
runtime, OR-Tools CP-SAT Python in a bounded child process under the server's
own interpreter. The server wrapper makes no network calls and uploads
nothing, but it does not sandbox the CP-SAT code you generate.

User problem:
{problem}

1. Analyze the problem: the decision variables and their domains, the hard
   constraints, and the objective (minimize / maximize, or "satisfy" for a
   pure feasibility problem). Ask concise clarifying questions only about
   material missing information — sizes, bounds, the objective,
   tie-breakers — and do not silently invent values.

2. Choose a backend by problem shape, and say which one you chose and why:
   - MiniZinc for expressive global constraints, a declarative formulation,
     or `.dzn` data files.
   - OR-Tools CP-SAT Python for pure integer and scheduling problems,
     imperative pre-processing, custom data structures, or when no managed
     MiniZinc runtime is installed (`check_runtime` reports that).
   Neither backend dominates every problem shape.

3. Draft a COMPLETE artifact, never prose: a MiniZinc model with every
   declaration, every constraint, exactly one `solve` statement, and an
   `output` block; or a full OR-Tools CP-SAT Python script that prints a final
   JSON object as its last stdout line, with `status`, `objective`, and
   `solution`.
   SAFETY: generate only modeling code — no network access, no file writes
   or deletes, no subprocess spawning — unless the user explicitly requested
   it. The server executes this code locally and does not sandbox it.

"""

_SOLVE_CONSTRAINT_PROBLEM_TAIL = """
4. Verify and execute, using the file sibling instead of pasting contents
   whenever the user already has the artifact on disk:
   - MiniZinc: call `check_minizinc_model(model=<model text>, data=<dzn
     text, omitted when there is none>, solver=<chosen solver id>)` first and
     never solve before it returns `"ok"`; on `"error"`, repair from the
     `stderr` diagnostics and re-check. Then call `solve_minizinc_model` with
     the same `data` and `solver`. For files on disk, run the same
     check-then-solve order through `check_minizinc_files` and
     `solve_minizinc_files`, passing `model_path` (and `data_path` when a
     data file exists). Never recommend a bare `minizinc` from the user's
     PATH — that bypasses the managed runtime.
   - CP-SAT: call `run_cpsat_python(source=<complete script>,
     script_timeout_ms=<milliseconds>)`, or `run_cpsat_python_file(script_path=<the
     existing script's path>)` for a script on disk.

5. Present the result in the user's own terms: a plain-language status, then
   the solution stated as the entities of their problem (shifts, bins, routes)
   rather than a raw JSON dump. Distinguish a proven optimum from a feasible
   but unproven result, and say plainly when the status is unsatisfiable,
   infeasible, unknown, or a timeout. For a MiniZinc solve, include the
   complete model-visible `Statistics:` section verbatim whenever it is
   present — never condense it to selected fields.
"""

# The one backend-neutral prompt, served by BOTH profiles, so it gets the same
# core/full treatment `server.create_mcp_server` already applies to the
# profile-varying tool descriptions. Only the spliced output-contract fragment
# differs; full's wording is unchanged (the advisory sentence re-wrapped onto
# its own line when it became a shared fragment, which is prose layout only).
SOLVE_CONSTRAINT_PROBLEM_PROMPT = (
    _SOLVE_CONSTRAINT_PROBLEM_HEAD
    + CPSAT_SCRIPT_STRUCTURE_GUIDANCE
    + "\n"
    + CPSAT_OUTPUT_CONTRACT_GUIDANCE
    + _SOLVE_CONSTRAINT_PROBLEM_TAIL
)

SOLVE_CONSTRAINT_PROBLEM_PROMPT_CORE = (
    _SOLVE_CONSTRAINT_PROBLEM_HEAD
    + CPSAT_SCRIPT_STRUCTURE_GUIDANCE
    + "\n"
    + CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE
    + _SOLVE_CONSTRAINT_PROBLEM_TAIL
)

MINIZINC_SOLUTION_WORKFLOW_PROMPT = """\
You are the MCP client's reasoning model, helping the user solve a
constraint-programming or optimization problem with openconstraint-mcp.

openconstraint-mcp calls no LLM and embeds no agent framework. Its
deterministic local tools run MiniZinc on the user's machine: you draft the
model, the local managed runtime verifies and solves it.

User problem:
{problem}

If the model already exists on disk as MiniZinc files (a `.mzn` plus an
optional `.dzn` data file), do not draft one. Review the existing files
(revise them only with the user's agreement) and run the same
validate -> solve -> present loop through the path-based tools:
`check_minizinc_files` first, then `solve_minizinc_files`, passing
`model_path` (and `data_path` when a data file exists) rather than pasting
file contents into the string tools — the path-based tools run from the
model's own directory, so a relative `include` resolves. They return the
same `CheckResult` / `SolveResult` shapes, so follow steps 4-7
substituting the TOOL, not just the argument: wherever step 4 says
`check_minizinc_model`, call `check_minizinc_files(model_path=<path>,
data_path=<path, when a data file exists>, solver=<chosen solver id>)`;
wherever steps 5-6 say `solve_minizinc_model`, call
`solve_minizinc_files(model_path=<path>, data_path=<path, when a data
file exists>, solver=<solver id>, timeout_ms=<milliseconds>)`. Never
pass `model_path` to the string tools.

Otherwise:

1. Analyze the problem: decision variables and their domains, hard
   constraints, and the objective (minimize / maximize, or "satisfy" for a
   pure feasibility problem).

2. If anything important is missing (sizes, bounds, the objective,
   tie-breakers), ask a few concise clarifying questions first. Do not
   silently invent values.

3. Draft a complete MiniZinc model: every variable and parameter
   declaration, every constraint, exactly one `solve` statement
   (`solve satisfy;`, `solve minimize <expr>;`, or `solve maximize <expr>;`),
   and an `output` block that prints the solution self-describingly.
   Default to the `cp-sat` solver unless the user says otherwise. For a
   specific number of distinct satisfaction solutions, pass `num_solutions`
   with `org.gecode.gecode` or `org.chuffed.chuffed` — the default `cp-sat`
   does not support it. For multiple optimal solutions, first solve the
   optimization to a proven optimum; then add a constraint fixing the
   objective expression to that value, switch to `solve satisfy;`, and
   enumerate with one of those supported solvers plus `num_solutions`.

4. Validate before solving: call `check_minizinc_model(model=<model text>,
   data=<dzn text, omitted when there is none>, solver=<chosen solver id>)`
   and branch on the returned `status`; never solve before a check has
   returned `"ok"`. The recommended loop is
   `draft -> check_minizinc_model -> repair -> solve_minizinc_model -> explain`.
   Pass the same `data` and `solver` to both the check and the solve so you
   validate the instance and configuration you solve.
   - `"ok"`: the model compiles; proceed to solving.
   - `"error"`: read the `stderr` diagnostics, repair, and re-check until
     `"ok"`.
   - `"timeout"`: validation itself — not the solve — timed out. Do not
     auto-solve. Explain this and let the user choose: simplify the model,
     raise `timeout_ms`, or solve anyway — the one exception to the
     `"ok"`-before-solve rule, taken only on the user's explicit choice.

5. Execute the model:
   - If `solve_minizinc_model` is listed among the tools you can call,
     invoke it as `solve_minizinc_model(model=<model text>, data=<same
     data>, solver=<chosen solver id>, timeout_ms=<milliseconds>)` — that
     exact name, no invented tools or extra arguments — and let the local
     managed runtime solve.
   - If it is not listed, do not fabricate a tool call, and do not tell
     the user to run a bare `minizinc` from their PATH — that bypasses the
     managed runtime and can pick up a different version. Instead walk them
     through the CLI:
       a. `openconstraint-mcp check-runtime` to confirm the managed runtime
          is installed and read its `minizinc` binary path.
       b. If the runtime is missing, either `openconstraint-mcp
          install-runtime` to download the managed bundle, or
          `openconstraint-mcp configure-runtime --runtime-dir <path>`
          (equivalently `OPENCONSTRAINT_MCP_RUNTIME_DIR=<path>`) to point at
          an existing install, then re-run `check-runtime`.
       c. Present the model as a code block and have them solve it by
          invoking that exact managed binary with the chosen solver flag,
          e.g. `--solver cp-sat`.

6. On a syntax, type, or solver error, revise and retry — but only through
   `solve_minizinc_model` when that tool is listed; never fabricate solver
   output. For a HARD problem — the solve returned `status` `unknown` or
   `timeout`, or `satisfied` on an optimization when the user needs a
   proven optimum, or several plausible formulation/solver choices remain —
   explore rather than settle for one run. Once the latest check has
   returned `"ok"`, race alternatives with a background portfolio via
   `submit_portfolio_job(models=[<model texts>], solvers=[<solver ids>],
   data=<same data>)`, polling `get_portfolio_job(job_id=<returned id>)`
   for the winner and the full per-attempt table, varying any of: model
   formulations (`models`, a list — e.g. a different variable encoding,
   redundant constraints, or symmetry-breaking constraints), `solvers`,
   seeds (`seed_count` or an
   explicit `seeds` list), and search controls (`free_search`, `parallel`,
   and each attempt's `per_attempt_timeout_ms` budget).
   For an especially hard instance, also consider the OR-Tools CP-SAT
   Python path (`cpsat_python_solution_workflow` prompt, `run_cpsat_python`) on the
   same problem — neither backend dominates every problem shape, and the
   structured results and checkers from both let you compare outcomes
   before committing to one. When the user instead wants SEVERAL candidate
   formulations raced against each other before committing to any one of
   them, use the `auto_tune_constraint_problem` prompt instead of ad hoc
   solo runs — it structures a three-tier smoke/tuning/full-instance race.

7. Present the result as a short, structured summary; do not dump the raw
   `SolveResult`. Lead with the result itself; do not narrate the prompt,
   workflow, or tool names you used unless the user explicitly asks for
   those implementation details. Read the fields rather than guessing, and
   always cover:
   - the `diagnostic` first when present: `diagnostic.category` is a stable
     enum (`infeasible`, `unbounded`, `timeout_no_incumbent`,
     `timeout_with_incumbent`, `checker_failed`, `syntax_or_compile_error`,
     `missing_data`, `type_error`, …) you branch on before reading raw
     `stdout`/`stderr`. It is `null` on a clean success. Treat `status` and
     `diagnostic.category` as the primary signals and stdout/stderr/transcripts
     as supporting evidence.
   - the `status`, in plain language: distinguish a proven-optimal solution
     (`optimal`) from a feasible-but-unproven one (`satisfied`), and both
     from `unsatisfiable`, `unbounded`, `unknown`, `error`, and `timeout`.
     Never describe a merely `satisfied` result as optimal. Judge "not
     proven optimal" from `status`, not `timed_out`: cleanly hitting
     MiniZinc's own `timeout_ms` returns `timed_out` false with a feasible
     `satisfied`/`unknown`, whereas `timed_out` true means the hard
     subprocess cap killed the run (`return_code` null, `status` `timeout`,
     and `stdout` may be truncated). A non-zero `return_code` with `error`
     means MiniZinc itself failed — read `stderr`.
   - the solution, only when the result carries one (`satisfied` /
     `optimal`; for `timeout` see the diagnostic branch below):
     show it as a block read verbatim from raw `stdout` — the `output`
     block text is authoritative, so do not restate the values yourself.
     Use the structured fields to organize that display, never to replace
     it: `solution` is the best/last solution as a variable-name -> value
     map (model variables only; the objective is reported separately),
     `solutions` is every solution in emission order (for an optimization,
     the improving sequence; its last entry equals `solution`), and
     `objective` is the best objective value (null for a pure-satisfaction
     problem). Build any item table or cross-solution comparison from
     `solution` / `solutions`, report the optimized value from `objective`,
     and keep the verbatim block itself from `stdout`. When the problem
     supplies item-like data (items with weights/values, tasks, shifts,
     etc.) and the solution selects among it, include a compact table
     rather than a prose-only list: for small item sets (roughly 20 rows or
     fewer), one row per item with the item index/name, relevant
     attributes, and the selected/count value; for larger sets, a compact
     table of selected items plus totals.
     An `unsatisfiable` or `error` result has no solution to show: say so
     plainly, and for `error` point at `stderr`. For `timeout`, branch on
     the diagnostic: `timeout_with_incumbent` means `solution` /
     `solutions` / `objective` hold the best found before the cap killed
     the run — present that as an unproven best-so-far, never as optimal;
     `timeout_no_incumbent` means there is no solution to show.
   - the complete model-visible `Statistics:` section is required whenever
     the `statistics` map is non-empty — do not omit it, summarize it, or
     replace it with only selected fields such as `solveTime` and
     `objectiveBound`. Copy the full section from the solve tool's text
     content into the user-facing answer. If the map is empty, say nothing
     of it; its keys vary by solver and are reported best-effort, not
     independently verified.

   Keep it tight: use each heading at most once, and by default add no
   speculative algorithm commentary (value-density ratios, greedy
   reasoning, alternative heuristics) unless the user asks for deeper
   analysis.

8. Persist only if the user asks to save the model: call
   `save_verified_minizinc_model(model=<final model text>, data=<final
   data>, checker=<checker, when one was used>, problem=<original problem
   text>, target_dir=<explicit absolute save directory>)` with the text
   exactly as last checked and solved. You ask the user for that path (or
   use your client's own file picker); the server opens no file dialog,
   and it re-verifies the artifacts through the managed runtime before
   writing anything.
   Replacing a previously saved directory needs `overwrite=true`, and only
   a directory written by a prior save can be replaced. If you explored via
   `submit_portfolio_job`, also pass that job's `PortfolioSolveResult` as
   `portfolio_result` so the winning race's full attempt table (every
   formulation/solver/seed tried, and why) is persisted alongside the saved
   model as `experiment-log.json`, and replay the winner's configuration in
   the save call itself: set `solver` and `random_seed` to the winning
   attempt's `solver` and `seed`, and `free_search` / `parallel` /
   `all_solutions` / `num_solutions` to the race's `solve_controls` values —
   the server rejects a save whose arguments do not match the winning
   attempt's configuration (`timeout_ms` is not compared). The server still
   re-verifies independently and never trusts the attached result as proof.

Boundaries:
- You draft the MiniZinc model; openconstraint-mcp does not.
- openconstraint-mcp owns no LLM credentials and invokes no generative
  model.
- All solving runs locally through the managed MiniZinc runtime — no remote
  backends, no uploads, no hidden network calls.
"""

SOLVE_CPSAT_PYTHON_PROMPT = (
    """\
You are the MCP client's reasoning model, helping the user solve a
constraint-programming or optimization problem using OR-Tools CP-SAT Python
through openconstraint-mcp.

openconstraint-mcp calls no LLM. Its deterministic local tools execute
Python scripts in a child process on the user's machine: you write the
CP-SAT script, `run_cpsat_python` runs it locally and returns a structured
result.

User problem:
{problem}

1. Analyze the problem: decision variables and their domains, hard
   constraints, and the objective (minimize / maximize, or "satisfy" for a
   pure feasibility problem).

2. If anything important is missing (sizes, bounds, the objective,
   tie-breakers), ask concise clarifying questions first. Do not silently
   invent values.

3. Write a complete, runnable OR-Tools CP-SAT Python script. For a SINGLE
   problem instance — the common case — hardcode the actual parameter values
   (e.g. the real player/group/week counts for a social golfer instance)
   directly in the script rather than a named "scenario" that needs a
   `config` to resolve. Reserve the cooperative `config` /
   `OPENCONSTRAINT_MCP_CPSAT_CONFIG` protocol (step 6) for EXPLICIT
   multi-attempt or configured experiments when choosing WHICH INSTANCE OR
   SCENARIO to solve — config-driven instance selection is not the default
   modeling style for a one-off save.
   - Solver-run controls are a separate concern from instance selection, and
     every generated script should always cooperate with them: define one
     `_solver_config() -> dict` helper that reads the JSON file named by
     `OPENCONSTRAINT_MCP_CPSAT_CONFIG` and returns `{{}}` when the variable is
     unset (omitted config and `{{}}` are equivalent), then apply only
     `num_workers` (default 1) and — only when present — `search_time_limit_seconds`
     from it, exactly as the example below does. This keeps an omitted or
     empty config preserving today's defaults: seed 42, one worker, and no
     CP-SAT-owned time limit. When you DO set `search_time_limit_seconds`,
     keep it well under the call's `script_timeout_ms`: it caps CP-SAT's
     search only, so parsing, model building, and serialization still have to
     fit in what is left, and the server does not check one against the other.
   - For a REPRODUCIBLE saved artifact, READ the seed from the environment
     (falling back to 42) and default to a single search worker via the
     config helper, exactly as the example below does.
     `save_verified_cpsat_python`'s optional `seed` argument sets the
     `OPENCONSTRAINT_MCP_CPSAT_SEED` environment variable for the replay
     re-run; a script that hardcodes the seed instead silently ignores
     the replay — the server cannot force a seed into arbitrary Python.
   - Emit a FINAL JSON object as the LAST line of stdout (same-shaped
     intermediate objects during search are allowed — see the improved-
     solution callback below — and only the last one is read as the
     result). Every
     number in it must be finite at any depth: `NaN`/`Infinity` in `objective`
     or anywhere inside `solution` is rejected. Complete runnable example —
     replace the toy model with the real one and keep the emitted JSON
     contract exactly:
     ```
     import json
     import os
     from dataclasses import dataclass

     from ortools.sat.python import cp_model


     @dataclass(frozen=True)
     class Item:
         name: str
         weight: int
         value: int


     @dataclass(frozen=True)
     class ProblemInstance:
         items: list[Item]
         capacity: int
         min_items: int


     @dataclass(frozen=True)
     class Solution:
         status: str
         selected: list[str] | None = None
         objective: float | None = None
         best_objective_bound: float | None = None


     def read_input() -> dict:
         return {{
             "items": [
                 {{"name": "radio", "weight": 3, "value": 5}},
                 {{"name": "lamp", "weight": 4, "value": 7}},
                 {{"name": "kettle", "weight": 5, "value": 9}},
                 {{"name": "clock", "weight": 2, "value": 3}},
             ],
             "capacity": 9,
             "min_items": 3,
         }}


     def parse_input(raw: dict) -> ProblemInstance:
         items = [
             Item(str(row["name"]), int(row["weight"]), int(row["value"]))
             for row in raw["items"]
         ]
         if not items:
             raise ValueError("the instance carries no items")
         min_items = int(raw["min_items"])
         if not 0 <= min_items <= len(items):
             raise ValueError("min_items is outside the available item count")
         return ProblemInstance(items, int(raw["capacity"]), min_items)


     def _solver_config() -> dict:
         config_path = os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")
         if not config_path:
             return {{}}
         with open(config_path) as config_file:
             return json.load(config_file)


     def solve(instance: ProblemInstance) -> Solution:
         model = cp_model.CpModel()
         take = [model.new_bool_var(item.name) for item in instance.items]
         pairs = list(zip(instance.items, take))
         model.add(sum(item.weight * v for item, v in pairs) <= instance.capacity)
         model.add(sum(take) >= instance.min_items)
         total_value = sum(item.value * v for item, v in pairs)
         model.maximize(total_value)

         config = _solver_config()
         solver = cp_model.CpSolver()
         solver.parameters.random_seed = int(
             os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42")
         )
         solver.parameters.num_workers = config.get("num_workers", 1)
         search_time_limit_seconds = config.get("search_time_limit_seconds")
         if search_time_limit_seconds is not None:
             solver.parameters.max_time_in_seconds = search_time_limit_seconds
         status_code = solver.solve(model)

         status_map = {{
             cp_model.OPTIMAL: "optimal",
             cp_model.FEASIBLE: "feasible",
             cp_model.INFEASIBLE: "infeasible",
             cp_model.UNKNOWN: "unknown",
         }}
         has_solution = status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE)
         bound_states = (cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN)
         return Solution(
             status=status_map.get(status_code, "error"),
             selected=(
                 [item.name for item, v in pairs if solver.value(v)]
                 if has_solution
                 else None
             ),
             objective=(
                 float(solver.objective_value)
                 if model.has_objective() and has_solution
                 else None
             ),
             best_objective_bound=(
                 float(solver.best_objective_bound)
                 if model.has_objective() and status_code in bound_states
                 else None
             ),
         )


     def serialize_solution(solution: Solution) -> dict:
         values: dict = {{}}
         if solution.selected is not None:
             values = {{"selected": solution.selected}}
         return {{
             "status": solution.status,
             "objective": solution.objective,
             "solution": values,
             "best_objective_bound": solution.best_objective_bound,
         }}


     def write_output(payload: dict) -> None:
         print(json.dumps(payload))


     def main() -> None:
         write_output(serialize_solution(solve(parse_input(read_input()))))


     if __name__ == "__main__":
         main()
     ```
     `best_objective_bound` (OR-Tools' `solver.best_objective_bound` — a
     PROPERTY, not a method) is a diagnostic bound, not a proven objective.
     Include it for every optimization model so a `status="unknown"` result
     still carries search-progress information even with no incumbent.
     CRITICAL: neither property raises when it has nothing meaningful to
     report — it returns `0.0` or another arbitrary number instead.
     `solver.objective_value` is meaningless without an incumbent: for a
     PURE FEASIBILITY problem (no `model.minimize`/`maximize` call, so
     `model.has_objective()` is `False`), and for an optimization run that
     ends `infeasible` or `unknown` with no solution.
     `solver.best_objective_bound` is meaningless for a pure feasibility
     problem and for `infeasible`/`error`, but on an `unknown` that actually
     searched it is genuine search progress; a run stopped before search
     started reports the uninitialized `0.0` instead. Never drop the
     example's guards: emit `objective` only for a solution-bearing status
     (`optimal`/`feasible`), emit `best_objective_bound` only for
     `optimal`/`feasible`/`unknown`, and emit `null`, not a fabricated
     number, in every other case.
   - For a long or optimization run that may hit `script_timeout_ms`, ALSO emit an
     intermediate JSON object of the SAME shape on each improved solution,
     from a `cp_model.CpSolverSolutionCallback`. Replace the example's WHOLE
     `solve()` function with the one below, which reuses the same
     `Solution` record, `serialize_solution()`, `write_output()`, and
     `_solver_config()`:
     ```
     def solve(instance: ProblemInstance) -> Solution:
         model = cp_model.CpModel()
         take = [model.new_bool_var(item.name) for item in instance.items]
         pairs = list(zip(instance.items, take))
         model.add(sum(item.weight * v for item, v in pairs) <= instance.capacity)
         model.add(sum(take) >= instance.min_items)
         total_value = sum(item.value * v for item, v in pairs)
         model.maximize(total_value)

         class _Best(cp_model.CpSolverSolutionCallback):
             def __init__(self, names, variables, has_objective):
                 super().__init__()
                 self._names = names
                 self._variables = variables
                 self._has_objective = has_objective

             def on_solution_callback(self):
                 write_output(serialize_solution(Solution(
                     status="feasible",
                     selected=[
                         name
                         for name, v in zip(self._names, self._variables)
                         if self.value(v)
                     ],
                     objective=(
                         float(self.objective_value) if self._has_objective else None
                     ),
                     best_objective_bound=(
                         float(self.best_objective_bound)
                         if self._has_objective
                         else None
                     ),
                 )))

         config = _solver_config()
         solver = cp_model.CpSolver()
         solver.parameters.random_seed = int(
             os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42")
         )
         solver.parameters.num_workers = config.get("num_workers", 1)
         search_time_limit_seconds = config.get("search_time_limit_seconds")
         if search_time_limit_seconds is not None:
             solver.parameters.max_time_in_seconds = search_time_limit_seconds
         names = [item.name for item in instance.items]
         status_code = solver.solve(model, _Best(names, take, model.has_objective()))

         status_map = {{
             cp_model.OPTIMAL: "optimal",
             cp_model.FEASIBLE: "feasible",
             cp_model.INFEASIBLE: "infeasible",
             cp_model.UNKNOWN: "unknown",
         }}
         has_solution = status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE)
         bound_states = (cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN)
         return Solution(
             status=status_map.get(status_code, "error"),
             selected=(
                 [item.name for item, v in pairs if solver.value(v)]
                 if has_solution
                 else None
             ),
             objective=(
                 float(solver.objective_value)
                 if model.has_objective() and has_solution
                 else None
             ),
             best_objective_bound=(
                 float(solver.best_objective_bound)
                 if model.has_objective() and status_code in bound_states
                 else None
             ),
         )
     ```
     Pass `model.has_objective()` into the callback so the same
     `0.0`-vs-`null` guard applies there too — a feasibility problem's
     callback fires on every found solution, not just optimization runs.
     The child runs unbuffered, so on a timeout the server recovers the
     last such block as the best-so-far. The final block (printed after
     `solve` returns) remains the authoritative result on a clean run.
     Install the callback UNCONDITIONALLY — never gate it on a configured
     CP-SAT limit. That limit bounds SEARCH only (not parsing, model
     building, or serialization), nothing checks it against
     `script_timeout_ms`, and the script cannot read that deadline, so it
     can never prove the solve returns first. Gate the callback and a run
     killed mid-search prints nothing at all, and every solution CP-SAT
     found is lost.
   - SAFETY: generate only CP-SAT modeling code — no network access, no
     file writes or deletes, no subprocess spawning — unless the user
     explicitly requested it. The server executes this code locally in a
     child process and does not sandbox it.

"""
    + CPSAT_SCRIPT_STRUCTURE_GUIDANCE
    + "\n"
    + CPSAT_OUTPUT_CONTRACT_GUIDANCE
    + """
4. Call `run_cpsat_python(source=<complete script>,
   script_timeout_ms=<milliseconds>)`. The server runs it locally in a child
   process (not remote, not sandboxed) and returns a `CpsatPythonResult`
   with `status`, `solution`, `objective`, `best_objective_bound`
   (diagnostic only — see step 3), `stdout`, `stderr`, `return_code`,
   `timed_out`, `truncated`, `duration_ms`, and a structured `diagnostic`
   (`null` on a clean success).

5. Present the result clearly:
   - Read `diagnostic` first when present: `diagnostic.category` is a stable
     enum (`infeasible`, `timeout_no_incumbent`, `timeout_with_incumbent`,
     `output_truncated`, `child_process_error`, `checker_failed`, …) you
     branch on before reading raw `stdout`/`stderr`. Treat `status` and
     `diagnostic.category` as the primary signals and raw `stdout`/`stderr`
     as supporting evidence, never the primary status signal.
   - Distinguish `optimal` (proven best) from `feasible` (valid but
     unproven optimal). Never describe a `feasible` result as optimal.
   - For `infeasible` or `error`, say so plainly; point at `stderr` on
     `error`. For `timeout`, the child process exceeded `script_timeout_ms`; a
     populated `solution` is the best found so far (unproven, treat as
     feasible-not-optimal), otherwise none was reached in time.
   - For `unknown` (no incumbent found), mention `best_objective_bound`
     when present — it shows the solver made bound progress, but it is a
     diagnostic hint, not a solution.
   - Describe the solution in the user's own terms (task names, variable
     semantics), not as a raw JSON dump.
   - For a HARD instance — `status` is `unknown` or `timeout`, or
     `feasible` when the user needs a proven optimum — also consider the
     MiniZinc portfolio path (`minizinc_solution_workflow` prompt,
     `submit_portfolio_job`) on the same problem — neither backend
     dominates every problem shape, and the structured results and checkers
     from both let you compare outcomes before committing to one. When the
     user instead wants SEVERAL candidate formulations raced against each
     other before committing to any one of them, use the
     `auto_tune_constraint_problem` prompt instead of ad hoc solo runs — it
     structures a three-tier smoke/tuning/full-instance race.

6. For MULTIPLE explicit attempts — comparing model/source variants, or the
   same source under different cooperative configs — use
   `run_cpsat_python_experiment(attempts=[<attempt objects>],
   objective_sense=<"minimize" | "maximize"; omit for pure feasibility>)`
   instead of calling `run_cpsat_python` repeatedly yourself. YOU always
   supply every attempt's complete script; the server never generates,
   diffs, or merges attempts — it only executes what you give it, verifies
   acceptance, and selects a winner.
   - Each attempt is
     `{{name, source | script_path, args, seed, config, script_timeout_ms}}`. Set
     EXACTLY ONE of `source` (a full, independent inline script, same SAFETY
     rule as step 3) or `script_path` (a local path to an existing script);
     setting both, or neither, is rejected before anything runs. `args` is a
     list of strings appended after `script_path` as the child's
     `sys.argv[1:]`, and is rejected alongside `source` rather than ignored.
     `seed` and `config` are optional cooperative protocols: a script must
     opt in to read them, and a non-cooperating script simply ignores them.
   - Prefer `script_path` when the variants ALREADY EXIST on disk — it runs
     each script from its own directory, so a relative `open()` of a shared
     sibling data file resolves and you never paste the same data into
     several attempts. Note the trade-off: a `script_path` attempt is marked
     `used_script_path` and cannot serve as `save_verified_cpsat_python`
     provenance (step 7), because that save re-runs inline source in a fresh
     temp directory and can replay neither `args` nor sibling data. Use an
     inline `source` attempt for anything you intend to save with provenance.
   - To vary the SAME script by a cooperative config instead of pasting it
     multiple times, have the script read
     `os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")`, load that path as
     JSON, and apply whichever fields it defines, e.g.:
       `config_path = os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")`
       `config = json.load(open(config_path)) if config_path else {{}}`
       `solver.parameters.num_workers = config.get("num_workers", 1)`
     The server only writes this JSON to a temp file and points the env var
     at it — it never sets OR-Tools parameters itself. An empty `config`
     (`{{}}`) behaves identically to omitting it.
   - If you set `max_parallel_attempts > 1`, keep each attempt's own
     `solver.parameters.num_workers` conservative: oversubscribing the
     machine's CPUs makes runs slower and less stable, not faster.
   - Attach ONE independent `checker` (with the `problem` value it grades
     against) whenever you are comparing candidates. Sharing one checker —
     and ranking attempts at all — is valid ONLY when every attempt solves
     the SAME problem, the SAME instance, and the SAME objective under the
     SAME objective sense, emitting one shared `solution` schema. Split
     mismatched scripts into separate calls: ranking different objectives or
     different instances against each other is a meaningless comparison.
   - That checker must be a standard-library PREDICATE over the reported
     answer (step 7c's rules apply unchanged): validate that the instance is
     present and well formed, that the solution COVERS every element the
     instance requires, that every hard constraint holds, and that the
     reported `objective` is consistent with the solution it came with.
     Never `import ortools` and never re-solve. An `accepted` verdict proves
     only the properties that checker evaluated — it is NOT an independent
     proof that an `optimal` claim is globally optimal.
   - Read the three non-accepted verdicts differently before repairing
     anything, and never "fix" one by blindly changing the emitted envelope:
     `rejected` means a well-formed solution WAS graded and violates the
     problem, which points at the model's constraints. `error` means NO VALID
     VERDICT was produced — the payload could not be graded (an unusable
     instance, an output block that is not a well-formed claim) OR the checker
     itself failed to run, exited non-zero, was truncated, or printed
     malformed or self-contradictory output. So `error` does not by itself
     accuse the model. `timeout` means the checker ran out of time, which
     points at the checker's cost or its `checker_timeout_ms`, not at the
     answer.
   - An attempt row does NOT carry the checker's own output: it has
     `checker_status`, a short `message`, and a `diagnostic`, but no `errors`,
     `stdout`, or `stderr`. Before repairing anything on an `error` or
     `timeout` verdict, RE-RUN that one attempt to get the full checker
     report, replaying its EXACT inputs — the same `problem`, `seed`, `config`,
     `script_timeout_ms`, and `checker_timeout_ms` — because a CP-SAT run is seed- and
     config-dependent and a rerun under different ones diagnoses a different solve:
     - Attempt used inline `source`: call `save_verified_cpsat_python(source=…,
       problem=…, checker=…, checker_timeout_ms=…, seed=…, config=…,
       script_timeout_ms=…, verify_only=true)`. Despite the name this SAVES NOTHING and
       needs no `target_dir` — it re-runs and re-grades, returning the full report
       in `checker`.
     - Attempt used `script_path`: call `run_cpsat_python_file_checked(
       script_path=…, checker_path=…, problem=…, checker_timeout_ms=…,
       args=…, seed=…, config=…, script_timeout_ms=…)`, writing the checker source to a
       file first since that tool takes a path.
     Do NOT use `submit_cpsat_python_job` / `submit_cpsat_python_file_job` for
     this: they accept no `seed` or `config`, so for a seeded or configured
     attempt they would silently grade a different run.
   - Match the loop to what the user actually asked for:
     - SELECT ONE WINNER (comparing candidates before committing to one):
       the tool already filters out non-accepted attempts, so present the
       winner and the table and leave intentionally discarded candidates
       unrepaired.
     - DELIVER ALL of several requested scripts: inspect EVERY attempt row,
       not only `winner_index`. Repair each non-accepted script and re-run
       until every requested script is accepted, or state plainly which one
       is still blocked and why. Never claim the deliverable is complete
       while any requested script's row is non-accepted. This is your own
       orchestration: the server reports per-attempt acceptance and a
       winner, and has no "all attempts passed" terminal state.
   - Present the winner plus the full attempt table (every attempt's
     status, objective, and whether it was accepted/rejected and why). A
     `timeout` winner is a best-so-far incumbent, not proven optimal, and
     not yet savable — re-run just that attempt with a larger `script_timeout_ms`
     first.

7. Persist only if the user asks: call
   `save_verified_cpsat_python(source=<final script>, problem=<the original
   problem text, or the combined JSON object of 7c when a data-driven checker must
   parse it>, target_dir=<explicit absolute save directory>)`. You
   ask the user for that path; the server opens no file dialog, and it
   re-runs the script to evaluate the save gate before writing anything.
   Replacing a previously saved directory needs `overwrite=true`.
   Optional `seed` is a single-run replay aid: the re-run replays that seed
   and the manifest records it. The save gates are UNCHANGED, so a
   `timeout` result still fails the reported gate regardless of `seed` —
   re-run it to optimal/feasible first. A saved seeded model reproduces by
   hand only when you set `OPENCONSTRAINT_MCP_CPSAT_SEED` to the recorded
   seed; the saved `model.py` carries only its own seed fallback.
   If the script came from `run_cpsat_python_experiment` — the winner, or
   another attempt you chose to save — also pass that attempt's exact
   `config` (`{{}}`/omitted if it ran without one) and the tool's result as
   `experiment_result`, so the full attempt table is persisted alongside
   the saved script as `experiment-log.json` — a provenance SUMMARY (hashes
   and scalar outcomes per attempt), not an archive of every attempt's full
   config. The server still re-verifies independently and never trusts the
   attached result as proof; `experiment_result` must describe an ACCEPTED
   INLINE-`source` attempt matching THIS exact save (same source, seed, and
   config) or the save is rejected before it re-runs anything. A matching
   attempt that ran from `script_path` does not qualify — the save re-runs
   inline source in a fresh temp directory and cannot replay that attempt's
   `args` or `cwd`-relative sibling data.

   Save gate options (in order of strictness):
   a. Reported gate (always applied): `status` in `optimal`/`feasible` and
      non-empty `solution`. This is the minimum required to save and the
      default when no `expectation` or `checker` is supplied.
   b. Expectation gate (optional): supply `expectation` with
      `objective_sense` ('maximize' or 'minimize') and a numeric
      `objective_threshold`. The server checks the script's reported
      objective against this threshold. It is a quality gate or regression
      bound — it does NOT prove the solution is globally optimal.
   c. Checker gate (optional): supply `checker` (a complete Python script
      as a source string) that independently validates the solution against
      problem-specific constraints. The checker script must:
      - Read the payload JSON path from its FIRST positional argument
        (`sys.argv[1]`), e.g. `payload = json.load(open(sys.argv[1]))`.
        The payload has keys `problem` (str|null), `solution` (dict),
        `objective` (float|int|null), and `solver_status` (str).
      - Print exactly ONE JSON object as its FINAL stdout line:
        `{{"status": "accepted"|"rejected"|"error", "errors": [...], "details": {{...}}}}`
        `accepted` with an empty `errors` list is the only passing verdict.
      - Split the two FAILING verdicts your checker emits by what failed,
        because the client fixes a different artifact for each. `error` means
        the payload could not be graded at all — an unusable instance, or a
        `solution`/`solver_status` that is not a well-formed claim — and
        points at the `problem` value or the script's output code. `rejected`
        means a well-formed solution WAS graded against the instance and
        violates it, and points at the model's constraints. Both fail the gate
        either way, so the split costs nothing and is what stops a client from
        "fixing" correct constraints when the real bug is a missing output
        key.
      - Reading the verdict BACK: the server reports `error` for more than
        your checker's own `error` — it also normalizes to `error` when the
        checker could not be started, exited non-zero, was truncated, or
        printed malformed or self-contradictory output (`accepted` with a
        non-empty `errors` list). It adds a third non-accepted verdict,
        `timeout`, when the checker exceeded `checker_timeout_ms`, which
        points at the checker's own cost rather than at the answer. So
        `error` means NO VALID VERDICT, not "the model is wrong": read the
        report's `errors`, `stdout`, and `stderr` — all three are returned on
        these tools — to see whether the model, the emitted envelope, or the
        checker script is the artifact to repair.
      - Be a PREDICATE, not a solver: grade the solution you were handed
        with plain arithmetic over the payload, standard library only. Never
        `import ortools` and never re-solve. The checker runs in the SAME
        interpreter as the model and under its own timeout, so a solving
        checker inherits the failure modes it exists to catch — it can time
        out, exhaust memory, or repeat the model's own modeling bug on
        exactly the hard instances where an independent verdict matters.
      - Never accept VACUOUSLY. Validate the instance BEFORE grading against
        it and return `error` when required keys are missing or domain
        cardinalities disagree with the supplied data. A zero-cardinality
        domain is valid when its cardinality, data, solution, and objective
        (when present) agree; an unexpectedly empty collection may instead
        reveal a serialization slip. Check COVERAGE — every element the
        instance requires is present in the solution — not only that the
        entries present are self-consistent.
      - SAFETY: generate only validation code — no network access, no file
        mutations, no subprocess spawning — unless the user explicitly
        requested it. The server executes this code locally and does not
        sandbox it.
      A data-driven checker reads the problem instance from
      `payload["problem"]` — the only caller-controlled field the sealed
      checker process sees, and the same single value that is persisted as
      `problem.txt`. It therefore has to carry BOTH, so when you write one,
      pass as `problem` a single JSON object holding the machine-readable
      instance AND the user's original request verbatim under its own key:
      `{{"request": "<the user's own words>", "num_machines": 6, "jobs":
      [...]}}`. Send it either as that object or as its serialized text —
      the server serializes an object for you, and the checker receives text
      to `json.loads` either way.
      Keep those keys FLAT and alongside each other, the way this
      repo's `examples/job_shop/data_*.json` sit human-readable
      `name`/`source` next to the instance — a checker reads only the keys
      it needs and ignores the rest, so carrying provenance costs it
      nothing. Never drop the original request to make room for the
      instance (`name`/`source` describe the benchmark, not what the user
      asked for), and never pass free-form prose alone as `problem` when the
      checker must parse it. Prefer this to hardcoding one instance's data in
      the checker script, so the checker validates whatever instance was
      actually solved and stays reusable across instances.
   Write a checker when the user asks for independent validation, when the
   problem has structural constraints the reported `status` alone cannot
   confirm, or when the result will be reused and higher confidence is
   valuable.
   OPTIONAL CHECKER SELF-TEST: `run_cpsat_python_file_checked(...,
   test_checker=true)` reruns the checker only after its baseline accepts, using
   four generic mutations of the solution. Read `checker_test.mutations`, not
   just `rejected_count`/`accepted_count`: `rejected_count: 0, accepted_count:
   0` can mean no mutation applied or that every probe errored, timed out, or
   was skipped. A rejection shows non-vacuity, not completeness; zero rejections
   is inconclusive because generic mutations can remain feasible. For stronger
   evidence, also test a problem-specific known-invalid payload.

8. To replay a saved artifact later, read its
   `.openconstraint-model.json` manifest and call
   `run_cpsat_python_file(script_path=<saved model.py path>,
   seed=<manifest verification.replay_seed>, config=<parsed
   replay-config.json contents, when that sibling file exists>)` — no
   manual environment variables needed. `run_cpsat_python_file` has no
   checker parameter, so this only re-verifies at the `reported` level
   even for a `checked`-level save. To re-run the saved checker too, call
   `run_cpsat_python_file_checked(script_path=<saved model.py path>,
   checker_path=<saved checker.py path>, problem=<contents of the saved
   problem.txt>, checker_timeout_ms=<manifest
   verification.checker_timeout_ms>, seed=..., config=...)` in one step — it
   returns the checker's verdict alongside the result and persists nothing.
   Omitting `checker_timeout_ms` there silently replays the checker under
   `script_timeout_ms` instead of the cap the save recorded. Use
   `save_verified_cpsat_python` again
   with `verify_only=true` when the save also recorded an objective
   `expectation` you need re-checked — which re-runs every gate and needs no
   `target_dir`, and ignores one if you pass it; to persist the replay
   instead, omit `verify_only` (or pass `verify_only=false`) and supply a
   real `target_dir` — plus the saved source/checker/seed/config, AND —
   whenever the manifest or saved directory has them — the original
   `problem` (read from `problem.txt` if a `problem` artifact is listed),
   `expectation` (rebuilt from `verification.expectation.objective_sense` /
   `objective_threshold` if present), and `script_timeout_ms` (from
   `verification.script_timeout_ms`). Omitting any of these changes what gets
   replayed: `problem` feeds the checker's payload directly, so a checker
   that reads it validates against different input; `expectation` is a gate
   that runs and can fail *before* the checker ever runs, so leaving it out
   silently skips the objective-threshold check; and `script_timeout_ms` is the
   solver's re-run budget (and, when `checker_timeout_ms` was not set
   explicitly, the checker's timeout too) — a different value can reach a
   different result under the same gates. Passing all of them reproduces
   every gate the original save ran, including the checker with the
   manifest's `verification.checker_timeout_ms` when present.

Boundaries:
- You write the CP-SAT Python script and any checker; openconstraint-mcp
  does not.
- openconstraint-mcp owns no LLM credentials and invokes no generative
  model.
- All solving runs locally in a child process — no remote backends, no
  uploads, no hidden network calls. The server wrapper makes no network
  calls; an LLM-generated script or checker that reaches the network is
  user-directed.
"""
)

AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT = (
    """\
You are the MCP client's reasoning model. Compare several MiniZinc and/or
OR-Tools CP-SAT formulations, then present one full-instance result.

Use this prompt only when the user asks to compare approaches, not
automatically after a hard single-backend run. This is client-side
orchestration: you draft and select candidates; openconstraint-mcp only
checks, runs, and verifies them locally.

User problem:
{problem}

Use three tiers: a tiny smoke instance rejects broken candidates, a separate
representative tuning instance selects one provisional candidate per backend,
and the full instance re-checks and solves each finalist. Only the
full-instance final run's result is ever presented to the user or used as
save-tool provenance.

1. Identify the decision variables, domains, constraints, and objective. Ask
   concise questions if required values, bounds, tie-breakers, or the objective
   are missing; do not invent them.

2. Look for an existing `.mzn`/`.dzn` pair or CP-SAT `model.py`. If found,
   review it (revise only with the user's agreement) and include it as ONE
   candidate formulation in the drafted set. Do not ignore it, and do not
   treat it as the only candidate.

3. Draft a small candidate set. Vary something that actually changes the
   SEARCH SPACE, not just cosmetic structure:
   - symmetry breaking; for interchangeable objects, draft one candidate WITH
     symmetry breaking and one WITHOUT;
   - implied/redundant constraints;
   - global vs. decomposed constraints such as `alldifferent`, `cumulative`,
     or `circuit` and their CP-SAT equivalents;
   - variable domain tightening.
   Do not draft candidates that differ only in variable naming, constraint
   ordering, or code style. Search STRATEGY is a second, complementary axis,
   distinct from search space size.

   Backend rules:
   - MiniZinc: fix ONE shared `.dzn` parameter interface (names, types, and
     shapes) across all candidates. Only data values scale between tiers; the
     parameter interface itself stays fixed. When an existing `.mzn` is a
     candidate, the shared interface is that model's interface: new candidates
     conform to it, and the existing `.mzn` text must never be rewritten to
     fit it. If the existing `.mzn` hardcodes instance data instead of reading
     it from a `.dzn`, it cannot scale through data values alone: ask the user
     before deriving a parameterized copy for multi-scale racing. Without
     permission, race it only at its existing scale and skip tiers it cannot
     reach. For search strategy, pair `restart_luby` or `restart_geometric`
     with multiple tuning-stage seeds.
     Only Gecode/Chuffed honor restart annotations; CP-SAT ignores them and
     runs its own restarts, so pair a restart-annotated candidate with a
     restart-aware solver in `solvers`, not with `org.cp-sat`.
   - CP-SAT: hardcode the smoke values first. It will be REWRITTEN, not reused
     verbatim, at the representative tuning and full-instance stages; the
     provisional candidate is an approach, not a fixed source string. Use an
     existing `model.py` as-is for smoke, but the original file is never
     overwritten in place by a stage rewrite; the only write to the original
     file's path remains the explicit final save step. For search strategy,
     `solver.parameters.num_workers` above 1 enables OR-Tools' portfolio,
     which already includes automatic LNS and restarts. Do not draft a custom
     fix-and-reoptimize LNS loop. Multiple workers trade reproducibility for
     search power, so rerun every tier. Generate only CP-SAT modeling code: no
     network access, no file writes or deletes, and no subprocesses unless the
     user explicitly requested it.

"""
    # Spliced at TOP LEVEL between steps 3 and 4, verbatim and unindented: this
    # prompt drafts CP-SAT candidates in step 3 and REWRITES them per tier in
    # steps 6 and 11, so every one of those rewrites needs the same layout and
    # the same stdout envelope the other two CP-SAT routes teach. The full
    # output-contract variant is correct here because this prompt registers in
    # the full profile only.
    + CPSAT_SCRIPT_STRUCTURE_GUIDANCE
    + "\n"
    + CPSAT_OUTPUT_CONTRACT_GUIDANCE
    + """
4. Create a tiny smoke instance and use it ONLY to reject structurally broken
   candidates: `inspect_minizinc_model` then `check_minizinc_model` for each
   MiniZinc candidate, and one short `run_cpsat_python` per CP-SAT candidate.
   Call shapes: `inspect_minizinc_model(model=<candidate>, data=<smoke
   dzn>, solver=<a solver it will race with>)`, then
   `check_minizinc_model(model=<candidate>, data=<smoke dzn>, solver=<a
   solver it will race with>)`;
   `run_cpsat_python(source=<smoke script>, script_timeout_ms=<short budget>)`.
   For a candidate that already exists on disk (step 2), use the path-based
   tools instead — `inspect_minizinc_files`/`check_minizinc_files` with
   `model_path`/`data_path`, and `run_cpsat_python_file(script_path=<the
   existing model.py>, script_timeout_ms=<short budget>)` — they run from the
   file's own directory, so relative includes and sibling data resolve.
   When that script reads `sys.argv` for its data file, add
   `args=[<data file>]`; without it the script silently falls back to its
   own hardcoded default instance instead of the one you meant to smoke.
   This step never ranks or selects a winner among the candidates that pass.

5. Call `list_available_solvers` before any MiniZinc portfolio work.

6. Create a SEPARATE, larger representative tuning instance that exercises
   the problem's structure. Never rank or select using the smoke instance's
   results. Reuse MiniZinc's fixed `.dzn` interface with larger values. Each
   CP-SAT candidate is REWRITTEN with the representative tuning instance's
   values hardcoded.

7. Choose winners WITHIN a backend only; never merge candidates from both
   backends into one race. Draft a checker whenever more than one candidate is
   being compared, not only across backends: a checker is what stops an
   incorrect formulation from winning the tuning-stage race. That only holds
   if each checker is a PREDICATE that grades the emitted solution against
   the instance — one that re-solves the problem inherits the very failure it
   exists to catch, and one that accepts missing or cardinality-inconsistent
   instance data instead of erroring passes every candidate alike. For a
   cross-backend comparison, draft TWO backend-specific checkers that enforce
   the same problem constraints. They are NOT interchangeable source:
   MiniZinc uses inline MiniZinc solution-checker source; CP-SAT uses a Python
   script that reads a payload JSON path from `sys.argv[1]` (keys `problem`,
   `solution`, `objective`, `solver_status`). A CP-SAT checker that reads its
   instance from `payload["problem"]` instead of hardcoding it constrains
   every call that attaches it: pass as `problem` ONE flat JSON object
   carrying BOTH the user's original request verbatim AND the
   machine-readable instance THAT run solves — the checker reads the keys it
   needs and ignores the rest, so the request rides along as provenance. Each
   tier solves a DIFFERENT instance, so rebuild that value per tier; handing
   a tuning-stage run the full instance makes the checker reject a
   correct solution. A MiniZinc checker never sees `problem` (it reads the
   `.dzn` interface), so `problem` stays the original text there. Compare each
   backend's final, checker-validated result across backends only when both
   represent the SAME objective and objective sense. When the objectives or
   senses don't match, ask the user which backend/result to keep instead of
   picking one yourself. Checkers remain optional for a single-backend,
   single-candidate run.

8. Select the PROVISIONAL MiniZinc candidate by submitting ONE
   `submit_portfolio_job` call PER smoke-surviving MiniZinc candidate (one
   model, with any solver/seed variants). Call shape:
   `submit_portfolio_job(models=[<one candidate>], solvers=[<solver ids>],
   data=<tuning dzn>, checker=<the MiniZinc checker>, seeds=[<tuning
   seeds>], per_attempt_timeout_ms=<budget>)`. Attach the checker when
   comparing candidates. Compare decisive, checker-accepted results
   across jobs: for an
   OPTIMIZATION problem, rank by best `objective`, then elapsed time; for a
   pure feasibility (`solve satisfy;`) problem there is no `objective`, so
   rank by `status` instead (`satisfied` outranks `unsatisfiable`), then elapsed
   time. NEVER race multiple candidate formulations inside one
   `submit_portfolio_job` call: its `first-decisive-result` winner treats
   `unsatisfiable` and `unbounded` as decisive, while the checker verdict is
   observational. A buggy formulation could otherwise win. Never trust one
   portfolio's `winner` across formulations.

9. Select the PROVISIONAL CP-SAT candidate with ONE
   `run_cpsat_python_experiment` call across the smoke-surviving CP-SAT
   candidates. Call shape:
   `run_cpsat_python_experiment(attempts=[<attempt objects>],
   objective_sense=<"minimize" | "maximize"; omit for pure feasibility>,
   checker=<the CP-SAT checker>, problem=<the original problem text, or step
   7's combined JSON object for the instance THIS run solves when the checker parses
   it>)`, where
   each attempt is
   `{{name, source | script_path, args, seed, config, script_timeout_ms}}` with
   EXACTLY ONE of a complete, independent inline `source` or a
   `script_path` to an existing on-disk script (both set, or neither, is
   rejected). `script_path` runs each script from its own directory — use it
   for candidates already on disk that share a sibling data file, passing
   `args` (rejected alongside `source`) for a per-candidate data argument.
   `checker`/`problem` stay INLINE TEXT for the whole call; there is no
   `checker_path` here. Attach the checker when comparing
   candidates. The tool accepts only attempts with a present solution
   and, when supplied, an `"accepted"` checker result, so it is NOT
   required to split into per-candidate calls. A `script_path` attempt is
   marked `used_script_path` and can never be save provenance in step 13 —
   rewrite the finalist as inline `source` there anyway, so this costs
   nothing.

10. Do not present a provisional candidate as the answer, and do not use its
    result as save-tool provenance.

11. Re-check each provisional formulation on the FULL instance:
    - MiniZinc: use a BOUNDED `solve_minizinc_model`/`solve_minizinc_files` call
      with full `data`, a short `timeout_ms`, and the checker —
      `solve_minizinc_model(model=<finalist>, data=<full data>, checker=<the
      checker>, solver=<winning solver>, timeout_ms=<short budget>)`. Never
      `check_minizinc_model`/`check_minizinc_files`: "`ok` means it compiles,
      not that it is satisfiable."
    - CP-SAT: REWRITE the provisional approach with the full instance's values
      hardcoded, then use `submit_cpsat_python_job` with the checker and poll
      `get_cpsat_python_job` until terminal. Call shape:
      `submit_cpsat_python_job(source=<full-instance script>, checker=<the
      CP-SAT checker>, problem=<step 9's `problem` value, rebuilt for
      the FULL instance>,
      script_timeout_ms=<budget>)`. `run_cpsat_python` has no `checker`
      parameter at all. Keep this exact full-instance `source` for the final
      job and any save.
    - Stop on MiniZinc's `unsatisfiable`/`error` or CP-SAT's
      `infeasible`/`error`; CP-SAT's status vocabulary has no `unsatisfiable`
      value. STOP and report the failure to the user instead of proceeding to
      the final solve.
    - A `timeout`/`unknown` re-check with NO incumbent solution is
      INCONCLUSIVE: proceed to the final solve, but flag that the pre-check did
      not confirm feasibility. MiniZinc returns a `checker.status` of
      `no_solution`; CP-SAT sets `checker_skipped_reason` instead of running
      `checker`. Do not apply the checker gate below to it.
    This is a pass/fail gate, not the result presented to the user.

12. Apply this checker gate to the re-check and final result whenever a
    solution exists and a checker was attached: the checker outcome must be a
    CLEAN pass to count as verified. MiniZinc requires a `checker.status` of
    exactly `"completed"` (a portfolio attempt reports the same verdict as
    `checker_status`); CP-SAT requires a `checker.status` of exactly
    `"accepted"`. Anything short of `checker.status == "completed"` /
    `checker.status == "accepted"` — a `violation`/`rejected` verdict, or a
    checker `error`/`timeout` on a real solution — means correctness was NOT
    confirmed: STOP. Once a solution exists, this gate has no inconclusive
    middle ground.

13. Submit the full-instance final solve:
    - MiniZinc: if `portfolio_result` provenance will be saved, use
      `submit_portfolio_job(models=[<the finalist model>],
      solvers=[<winning solver>], data=<FULL-instance dzn>, checker=<the
      checker>, seeds=[<winning seed>], per_attempt_timeout_ms=<final
      budget>)`, even for one model/solver — not step 8's shape: the save's
      data-hash consistency check rejects a `portfolio_result` produced
      with tuning data; otherwise use `submit_solve_job(model=<finalist>,
      data=<full data>, checker=<the checker>, solver=<winning solver>,
      random_seed=<winning seed>, timeout_ms=<final budget>)` — its
      `SolveResult` carries no `portfolio_result` field. When the winning
      attempt's `seed` is null (a race run without explicit `seeds`),
      omit `seeds` / `random_seed` from these calls entirely — `seeds`
      accepts only integers, and an unseeded winner has no seed to
      replay.
    - CP-SAT: use the SYNCHRONOUS `run_cpsat_python_experiment` (step 9's
      call shape) if `experiment_result` provenance will be saved, and give
      the attempt you intend to save an INLINE `source` — a `script_path`
      attempt is never accepted as save provenance. If that
      cannot fit a synchronous call, use `submit_cpsat_python_job` (step
      11's call shape) and save without `experiment_result`; there is no
      background experiment tool.
    Pass the relevant checker to the finalist call.

14. Poll the matching tool: `submit_portfolio_job` polls with
    `get_portfolio_job`; `submit_solve_job` polls with `get_solve_job`;
    `submit_cpsat_python_job` polls with `get_cpsat_python_job`. Each getter
    takes `job_id=<the id returned by its submit call>`. Read a synchronous
    experiment directly. Only this terminal result is presented.

    Before presenting a solution, apply step 12. A portfolio winner's
    `checker_status` is OBSERVATIONAL; `submit_solve_job`'s
    `SolveResult.checker` and `submit_cpsat_python_job`'s `checker` are also
    observational, so those tools may return an invalid solution. If the gate
    fails, STOP and report the violation to the user instead of presenting the
    result. A synchronous `run_cpsat_python_experiment` already filters
    rejected attempts, so this check is automatically satisfied whenever that
    path was used.

    A terminal `timeout`/`unknown` without an incumbent has nothing for the
    checker to check. MiniZinc reports `checker.status` of `no_solution`;
    CP-SAT sets `checker_skipped_reason` instead of `checker`. Present that
    result (flagged as unproven). Otherwise lead with the actual solve result
    and explain it in the user's terms: read `diagnostic.category` before raw
    `stdout`/`stderr` when a diagnostic exists (the raw streams are supporting
    evidence, never the primary status signal), describe `status` in the
    presenting backend's own vocabulary, never describe a result whose status
    is not `optimal` as optimal, and present a solution only when the status
    actually carries one. Include the complete `Statistics:` section whenever
    a MiniZinc result's `statistics` map is non-empty; CP-SAT results have no
    statistics map, so report none. Do not narrate the workflow or tool names
    you used unless the user asks for those implementation details.

15. Save only when the user asks — `save_verified_minizinc_model` for a
    MiniZinc finalist, `save_verified_cpsat_python` for a CP-SAT finalist —
    using an explicit absolute `target_dir` and the full-instance final
    run's result as provenance — never a smoke or representative-tuning
    result.
    `portfolio_result`/`experiment_result` are PROVENANCE ONLY; the save call
    hash-verifies provenance against the exact artifact and re-verifies it. It
    reaches checked verification only when the SAME `checker` you attached to
    the finalist run is passed directly to the save call itself.
    Dropping `checker` from the save call silently saves at a weaker level.
    - MiniZinc: pass the exact model/data/solver/seed (and, with
      `portfolio_result`, the race's
      `free_search`/`parallel`/`all_solutions`/`num_solutions` — the server
      rejects a save that does not replay the winning configuration), the
      SAME `checker` (when one was drafted for the finalist run), and the
      original problem text as
      `problem`; plus `portfolio_result` ONLY when the final run used
      `submit_portfolio_job`. A final run made through `submit_solve_job` has no
      `portfolio_result` to pass.
    - CP-SAT: pass the exact source/seed/config, the SAME `checker` (when one
      was drafted for the finalist run), and as `problem` the SAME value the
      full-instance finalist run used — the original problem text, or step
      7's combined JSON object for the full instance when the checker parses it, so
      the saved `problem.txt` keeps the request alongside the instance;
      plus `experiment_result` ONLY when the final run used the
      synchronous `run_cpsat_python_experiment`. A final run made through
      `submit_cpsat_python_job` has no `experiment_result` to pass.

Boundaries: openconstraint-mcp calls no LLM, runs no agent loop, and makes no
hidden network calls. Solving stays local; CP-SAT children are unsandboxed, so
generate no network or file-mutating code unless the user explicitly asks.
"""
)
