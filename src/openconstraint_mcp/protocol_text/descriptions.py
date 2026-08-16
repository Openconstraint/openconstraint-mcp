"""MCP tool and prompt description strings.

These are protocol-contract texts — what MCP clients see as tool/prompt
documentation.  Keeping them here lets server.py focus on wiring.
"""

# Shared description fragments spliced into the constants below, so a wording fix
# lands in one place instead of drifting across copies. This is the same pattern
# as _FILE_TOOL_SHARED_DESCRIPTION, applied to the cross-cutting guarantees the
# solve/job/portfolio tools repeat. Helpers cover near-duplicates that vary only
# by a single token (the job getter name, the terminal-states list).

_LOCAL_ONLY_GUARANTEE = (
    "Runs locally through the managed runtime: no network, no LLM, no telemetry."
)

# CP-SAT tools execute arbitrary Python under the server's own interpreter, not
# the managed runtime, so the wrapper's offline guarantee cannot extend to the
# child — keep that posture honest rather than reusing _LOCAL_ONLY_GUARANTEE.
_CPSAT_CHILD_POSTURE = (
    "Network posture: the server wrapper makes no network, LLM, or telemetry "
    "calls, but the child is arbitrary, unsandboxed Python run under the "
    "server's interpreter — it can open sockets, import `requests`, or shell "
    "out. 'Offline' describes the wrapper here, not the executed script."
)

# The stable plain-language problem vocabulary the four solve tools lead with.
# It also appears in _ROUTING_PARAGRAPH, which reaches the client only through
# the server `instructions`; duplicating it here is deliberate, because tool
# descriptions and instructions are advertised as separate fields a host may
# consume, truncate, or rank independently. Shared so the cue cannot drift
# between the four descriptions or between their core/full variants.
_CP_PROBLEM_DOMAINS = (
    "scheduling, rostering, assignment, routing, packing/bin-packing, knapsack, "
    "or resource allocation"
)

_UNKNOWN_JOB_ID_ERROR = "An unknown `job_id` is an MCP error."

_NO_ARGS_LIST_TOOL = "Takes no arguments; never downloads or runs anything."

_REGISTRY_NOTE = (
    "The registry is in-process and ephemeral: jobs don't survive a server "
    "restart, and finished jobs are retained only up to a cap (oldest evicted)."
)

_SOLVE_CONTROLS_LIST = (
    "`free_search`, `parallel`, `random_seed`, `all_solutions`, and the "
    "solver-gated, satisfaction-only `num_solutions`"
)

# Fans out to 2 core tools (and 4 full ones), so every byte here costs 2 of the
# core metadata budget. Keep it to the TERSE TYPE CONTRACT: what `solution` must
# semantically carry lives in the prompts and the server `instructions`.
_CPSAT_JSON_CONTRACT = (
    "The script MUST emit a final JSON object as its last stdout line "
    "with all three REQUIRED keys `status` (str), `objective` (number|null), "
    "and `solution` (object; `{}` with no incumbent), and MAY include an "
    "optional `best_objective_bound` (number|null) for diagnostics, e.g. "
    '`{"status": "<status>", "objective": <number|null>, "solution": '
    '{<str: val>}, "best_objective_bound": <number|null>}`. Extra keys are '
    'ignored; a missing or invalid required key is rejected (`status="error"` '
    "on a clean exit) with no solution and a `child_process_error` diagnostic "
    "naming the field."
)

_SAVE_TARGET_DIR_RULE = (
    "`target_dir` must be an EXPLICIT ABSOLUTE local directory whose parent "
    "exists; the server never opens a file dialog — the client supplies the "
    "path."
)

_MARKER_GATED_OVERWRITE = (
    "Overwrite is MARKER-GATED: a new or empty path is written directly; a "
    "non-empty directory is replaced wholesale (staged sibling + atomic swap) "
    "only when it holds a prior save's manifest marker, `overwrite=true` is "
    "passed, and it contains no files the prior save did not write; anything "
    "else is refused with an actionable error and nothing is touched."
)

_PACE_POLLING_NOTE = (
    "PACE polling against the job's budget: a `running` job has roughly its "
    "echoed timeout minus `elapsed_ms` left; wait a fraction of the remaining "
    "budget between polls rather than looping tightly."
)


def _returns_immediately_note(get_tool: str) -> str:
    """Background-submit tail: returns at once, watch `state` via `get_tool`."""
    return (
        "Returns at once, so it emits no progress/log status milestones; watch "
        f"`state` via `{get_tool}` instead. "
    )


def _cancellation_idempotent_note(terminal_states: str) -> str:
    """Shared cancel-tool idempotency sentence; `terminal_states` varies per tool."""
    return (
        "Cancellation is best-effort and idempotent: cancelling an "
        f"already-terminal job ({terminal_states}) is a no-op returning the "
        "current status unchanged. "
    )


def _job_result_contract(error_verdict: str) -> str:
    """Shared getter contract: when `result` exists and what `failed` means."""
    return (
        "CONTRACT: `result` is present exactly when `state` is `succeeded` or "
        "`timeout`, absent for `queued`/`running`/`failed`/`cancelled` — so "
        "branch on `state`, not on `result`. `failed` means the job machinery "
        f"itself raised (no result, see `message`); {error_verdict} is a "
        '`succeeded` job whose `result.status == "error"`, NOT `failed`. '
    )


_ROUTING_PARAGRAPH = (
    "For constraint programming or discrete optimization (scheduling, "
    "rostering, assignment, routing, knapsack, allocation, bin-packing, or "
    "model validation), use this MCP server before running solver code "
    "directly."
)

MCP_SERVER_INSTRUCTIONS = (
    _ROUTING_PARAGRAPH + "\n"
    "\n"
    "POSTURE: MiniZinc tools use the managed local MiniZinc runtime; never a "
    "remote solver or a bare PATH minizinc. CP-SAT tools run your Python "
    "locally in an UNSANDBOXED child process — the server wrapper makes no "
    "network calls, but the child is arbitrary code.\n"
    "\n"
    "BACKENDS — equal peers; pick by problem shape:\n"
    "- OR-Tools CP-SAT Python: zero-install, LLM-fluent; suits integer and "
    "scheduling models, imperative preprocessing, custom data structures.\n"
    "- MiniZinc: global constraints, `.dzn` data files, checker verification, "
    "portfolio racing, `num_solutions` enumeration.\n"
    "\n"
    "PROMPTS (when the client supports MCP prompts): for a natural-language "
    "problem use minizinc_solution_workflow or cpsat_python_solution_workflow; "
    "to race several candidate formulations before committing to one, use "
    "auto_tune_constraint_problem.\n"
    "\n"
    "WITHOUT PROMPTS:\n"
    "- MiniZinc: draft the model, check it with check_minizinc_model, then "
    "solve with solve_minizinc_model; inspect_minizinc_model reports the "
    "parameters a model needs as data before you build a `.dzn`. For a model "
    "and data already on disk, pass paths to check_minizinc_files / "
    "solve_minizinc_files instead of pasting contents — they run from the "
    "model's directory, so a relative `include` resolves. Verify solutions "
    "with a checker (`checker` inline, `checker_path` for file solves).\n"
    # The no-stdin rule below is deliberately a second copy of _CPSAT_NO_STDIN_FULL,
    # which carries the same rule on the CP-SAT tool descriptions; both copies are
    # pinned by test, so edit them together.
    "- CP-SAT: write a conforming script and run it with run_cpsat_python "
    "(bounded child: timeout, 1 MB output cap, tree-kill); "
    "run_cpsat_python_file_checked pairs an on-disk script with a required "
    "checker, and run_cpsat_python_experiment races explicit script/seed/"
    "config variants in one call. Give the script one ordered spine — "
    "`read_input`, `parse_input`, `solve`, `serialize_solution`, "
    "`write_output`, called in that order by `main` — "
    "and keep the boundary typed: `parse_input` hands `solve` a typed instance "
    "record and `solve` hands `serialize_solution` a typed solution record, "
    "never loose dicts. The child has NO stdin, so never call `input()` or "
    "read `sys.stdin`: embed the instance in the script, or pass a data file "
    "with run_cpsat_python_file(script_path=…, args=[…]).\n"
    "- DATA: when problem data lives in a local `.csv`/`.xlsx`, page it in "
    "with load_tabular_data instead of retyping it; write_tabular_result "
    "exports a result table.\n"
    "\n"
    "CP-SAT OUTPUT: `json.dumps` only builds a string that `print` sends to "
    "stdout — it writes no file, and the result is not saved anywhere. "
    "`solution` must carry the COMPLETE, problem-specific answer a checker can "
    "grade — every decision value — never prose, statistics alone, or only a "
    "path to a separately written result file. This "
    "text is advisory: nothing is verified until an MCP execution tool "
    "actually runs the script.\n"
    "\n"
    "LONG RUNS: the synchronous tools default to a 30 s wall-clock cap "
    "(`timeout_ms` on the MiniZinc tools, `script_timeout_ms` on the CP-SAT "
    "ones) and hold "
    "the connection open. If a run may outlast the client's own tool-call "
    "limit, submit it instead — submit_solve_job, submit_portfolio_job, "
    "submit_cpsat_python_job, submit_cpsat_python_file_job — and poll the "
    "matching get_* tool, waiting a fraction of the remaining budget between "
    "polls. " + _REGISTRY_NOTE + "\n"
    "\n"
    "WHEN A RUN FAILS: on `unsatisfiable`, call find_unsat_core "
    "(find_unsat_core_files for path solves) for a minimal conflicting subset "
    "before rewriting the model. When a MiniZinc tool fails for a missing "
    "runtime, confirm with check_runtime and tell the user to run "
    "`openconstraint-mcp install-runtime`, or switch to the zero-install "
    "CP-SAT backend.\n"
    "\n"
    "SOLUTION COUNTS: use `num_solutions` only with `org.gecode.gecode` or "
    "`org.chuffed.chuffed`, not the default `cp-sat`; for multiple optimal "
    "solutions, solve the optimization first, then re-solve as satisfaction "
    "with the objective fixed to the proven optimum.\n"
    "\n"
    "SAVING: on an explicit save request, use save_verified_minizinc_model or "
    "save_verified_cpsat_python with an absolute `target_dir`; the server "
    "re-verifies before writing and never opens a file dialog.\n"
    "\n"
    "PRESENTING RESULTS: lead with a plain-language status and the solution "
    "stated in the terms of the user's problem, not the raw JSON result; add "
    "a compact item table for item-like data, and reproduce any model-visible "
    "`Statistics:` section complete — never condense it to selected fields.\n"
    "\n"
    "PROGRESS: the MiniZinc check/inspect/solve/unsat-core tools emit "
    "stage-marker progress and log notifications, not a completion "
    "percentage — never render a percent bar."
)

# Core-profile server instructions: the default `stdio` toolset advertises only
# the eight core tools, so these instructions name ONLY those tools and describe
# the opt-in `--toolset full` surface generically. They must never name a
# full-only tool or prompt — the metadata-budget test cross-checks this text
# against the same forbidden set it scans the core tool payload with.
MCP_SERVER_INSTRUCTIONS_CORE = (
    _ROUTING_PARAGRAPH + "\n"
    "\n"
    "This is the default core toolset. Launch with `--toolset full` for model "
    "inspection, unsat-core diagnostics, jobs, portfolios, experiments, "
    "verified saving, and tabular I/O.\n"
    "\n"
    "BACKENDS — equal peers; pick by problem shape:\n"
    "- OR-Tools CP-SAT Python: zero-install, LLM-fluent; suits integer and "
    "scheduling models, imperative preprocessing, custom data.\n"
    "- MiniZinc: global constraints and `.dzn` data files.\n"
    "\n"
    "WORKFLOW:\n"
    "- MiniZinc: draft the model, check it with check_minizinc_model, then "
    "solve with solve_minizinc_model. For files already on disk use "
    "check_minizinc_files / solve_minizinc_files — they run from the model's "
    "directory, so a relative `include` resolves. check_runtime reports "
    "whether the managed runtime is installed (if not: `openconstraint-mcp "
    "install-runtime`, or use CP-SAT, which needs none); "
    "list_available_solvers lists solvers.\n"
    # Core's ONLY channel for these two rules: the no-stdin rule is
    # deliberately full-only in the tool descriptions (see
    # _CPSAT_NO_STDIN_FULL, kept out of core because that text fans out to two
    # tools), and _CPSAT_JSON_CONTRACT states the TYPE contract while
    # delegating what `solution` must semantically carry to the prompts and
    # these instructions — which a client that ignores prompts never sees. One
    # `instructions` field is one copy, so the fan-out argument does not apply.
    "- CP-SAT: run the script with run_cpsat_python, or run_cpsat_python_file "
    "for one already on disk (bounded child: timeout, 1 MB output cap, "
    "tree-kill). The child has NO stdin: never call `input()` or read "
    "`sys.stdin` — embed the instance in the script, or pass a data file via "
    "run_cpsat_python_file `args`. `solution` in the final stdout JSON must "
    "carry the COMPLETE answer — every decision value — never prose or a file "
    "path.\n"
    "\n"
    "PRESENTING RESULTS: lead with a plain-language status and the solution "
    "stated in the terms of the user's problem, not the raw JSON result; add "
    "a compact item table for item-like data, and reproduce any `Statistics:` "
    "section complete — never condense it to selected fields.\n"
    "\n"
    "POSTURE: MiniZinc tools use the managed local MiniZinc runtime; never a "
    "remote solver or a bare PATH minizinc. CP-SAT tools run your Python "
    "locally in an UNSANDBOXED child process — the server wrapper makes no "
    "network calls, but the child is arbitrary code."
)

CHECK_RUNTIME_DESCRIPTION = (
    "Report whether the managed MiniZinc runtime is installed. Returns a "
    "RuntimeStatus: `installed` (bool), `runtime_dir` (the managed-runtime "
    "directory in use), and `minizinc_binary` (the MiniZinc binary inside it; "
    "null when not installed). When `installed` is false, any MiniZinc tool that "
    "starts a MiniZinc process (check/inspect/solve/unsat-core, and "
    "`list_available_solvers`, which reads the binary's config) will fail — tell "
    "the user to run `openconstraint-mcp install-runtime`. `check_runtime` itself "
    "and the CP-SAT Python tools do not need the runtime and work regardless. " + _NO_ARGS_LIST_TOOL
)

LIST_AVAILABLE_SOLVERS_DESCRIPTION = (
    "List solvers in the managed MiniZinc runtime. Returns a SolverList of "
    "SolverInfo entries — each with `id`, `name`, `version`, `tags`, and a "
    "`capabilities` object of deterministic facts read from the runtime's own "
    "`--solvers-json` config, for client-side solver routing. "
    "`capabilities.supports_all_solutions` (`-a`), `supports_free_search` "
    "(`-f`), `supports_parallel` (`-p`), and `supports_random_seed` (`-r`) "
    "report membership in the solver's declared `stdFlags`, and the solve "
    # Only the ROUTING consequence lives here — select by canonical id or lose
    # upfront rejection. The rejection mechanics belong to the tool that
    # rejects; see the `-a/-f/-p/-r` gate in _SOLVE_MINIZINC_MODEL_BODY.
    "tools enforce them: a control the solver does not declare is rejected "
    "before solving (see `solve_minizinc_model`), matched by exact canonical "
    "`id`, so route by canonical id — a short alias (e.g. `gecode`) passes "
    "through unchecked. `supports_num_solutions` (`-n`) is NOT a raw stdFlags "
    "read but the conservative gate matching the `num_solutions` control — "
    "True only for `org.gecode.gecode` and `org.chuffed.chuffed`, not the "
    "default `cp-sat`. The advisory `std_flags` list reports the flags the "
    "solver declares; it is NOT a passthrough surface — a client cannot send "
    "those flags back into `solve_minizinc_model` / `solve_minizinc_files`. It "
    "can list flags with no named control at all (`-i`, `-s`, `-t`, `-v`), and "
    "it can diverge from the gate: `org.gecode.gist` lists `-n` yet "
    # The copy-verbatim RULE is not repeated here: results.py emits
    # SOLVER_INVENTORY_PRESENTATION_REQUIREMENT in-band, directly above the
    # table it governs, on every call.
    "`supports_num_solutions` is False. The text content is a complete "
    "`id`/`name`/`version` inventory table plus a note (`capability_note`) "
    "that detailed capabilities are available on request; the `supports_*` "
    "booleans and `std_flags` themselves live only in the structured result "
    "and are not printed by default — surface them when the user asks."
)

# Full-only hard-instance guidance. The full profile keeps the portfolio nudge;
# the core profile (which hides `submit_portfolio_job`) swaps in a variant that
# names no hidden tool. Both share the same description body above.
_SOLVE_MZN_HARD_INSTANCE_FULL = (
    "For a HARD "
    "instance — status is unknown/timeout, or the best formulation/solver/seed "
    "choice is unclear — consider `submit_portfolio_job` to race multiple "
    "formulations, solvers, and seeds instead of one run; for an especially "
    "hard instance, also consider the OR-Tools CP-SAT Python path "
    "(`run_cpsat_python`) for the same problem."
)
_SOLVE_MZN_HARD_INSTANCE_CORE = (
    "For an especially hard instance, also consider the OR-Tools CP-SAT Python "
    "path (`run_cpsat_python`) for the same problem."
)

_SOLVE_MINIZINC_MODEL_BODY = (
    "Solve a constraint or discrete-optimization problem ("
    + _CP_PROBLEM_DOMAINS
    + ") from a COMPLETE MiniZinc model you have already drafted, never from the "
    "user's prose. Runs through the managed local runtime. `model` "
    "must be full source: declarations, constraints, exactly one `solve` "
    "statement, and an `output` block. Optional `data` is `.dzn` text run as a "
    "data file beside the model (omit when none is needed). Returns a "
    "SolveResult: `status`, `solver`, `return_code` (null on a timeout or an "
    "output-cap tree-kill; a child that overran the cap but exited on its own "
    "keeps its code), `timed_out`, `truncated` (combined stdout+stderr "
    "exceeded the 1 MiB cap — the child is killed, partial `solutions` kept, "
    "diagnostic `output_truncated`; page with `num_solutions` on "
    "`org.gecode.gecode`/`org.chuffed.chuffed` or shrink the model's `output`), "
    "`elapsed_ms`, `stdout` (human-readable solution "
    "text, rebuilt from the solve stream's output sections), `stderr` "
    "(diagnostics — model/solver errors and warnings, so you can revise and "
    "retry), `solution` (best/last solution as a variable -> value map, model "
    "variables only), `solutions` (every solution in order; its last entry is "
    "`solution`), `objective` (best objective value, null for pure "
    "satisfaction), `statistics` (best-effort, solver-defined keys; empty when "
    "the solver reports none, and a timeout or output-cap truncation can also "
    "leave it empty or partial), and `checker` (null unless a checker was "
    "supplied). Structured "
    "values come from the runtime's machine-readable solve stream, not scraped "
    # The copy-verbatim RULE is not repeated here: results.py emits
    # STATS_PRESENTATION_REQUIREMENT in-band, directly above the section it
    # governs, on every result whose statistics map is non-empty.
    "text. The text content includes a `Statistics:` section whenever that map "
    "is non-empty. Optional solver/search controls (an omitted "
    "control passes no flag, keeping the solver's default behavior): "
    "`free_search` (`-f`: solver's own search "
    "instead of the model's annotations — solver-dependent, not 'no search'); "
    "`parallel` (int >= 1 -> `-p`: search threads); `random_seed` (int -> `-r`); "
    "`all_solutions` (`-a`: enumerate all solutions, or the optimization "
    "improving-sequence, into `solutions`); `num_solutions` (int >= 1 -> `-n`: "
    "cap solutions for a SATISFACTION problem; SOLVER-GATED to `org.gecode.gecode` "
    "or `org.chuffed.chuffed`, NOT the default `cp-sat` — any other solver "
    "returns an actionable error; for optimization use `all_solutions`). The "
    "`-a/-f/-p/-r` controls are capability-gated: a flag missing from the "
    "solver's runtime-local `stdFlags` is rejected before solving with an "
    "error naming solver, control, and flag (canonical-id match — a short "
    "alias or unknown solver passes through); `list_available_solvers` reports "
    "each solver's `supports_*` facts. Optional `checker` is inline MiniZinc "
    "checker source written beside the model and passed via `--solution-checker`; "
    "it can `include` the co-located `model.mzn` but no other "
    "project-relative file — use `solve_minizinc_files` with `checker_path` "
    "for multi-file checkers. When supplied, the result's `checker` field is a "
    "nested CheckerReport "
    "with `status` (`completed`, `violation`, `no_solution`, `error`, "
    "`timeout`), `checks` (one verdict per solution, index-aligned with "
    "`solutions`), and `transcript` (the AUTHORITATIVE raw `--json-stream` "
    # The non-adjudication rule (author CORRECT/INCORRECT is not interpreted;
    # only a nested UNSATISFIABLE is a violation; only `status` proves
    # completeness) is NOT repeated here: results.py emits
    # SOLUTION_CHECK_NON_ADJUDICATION_NOTE in-band on exactly the results that
    # carry a checker report, where this text reaches every caller including
    # the ones that never pass a checker.
    "transcript of solve + checker objects). "
    "`structuredContent` carries the complete SolveResult. "
)

SOLVE_MINIZINC_MODEL_DESCRIPTION = _SOLVE_MINIZINC_MODEL_BODY + _SOLVE_MZN_HARD_INSTANCE_FULL
SOLVE_MINIZINC_MODEL_DESCRIPTION_CORE = _SOLVE_MINIZINC_MODEL_BODY + _SOLVE_MZN_HARD_INSTANCE_CORE

CHECK_MINIZINC_MODEL_DESCRIPTION = (
    "Compile-check a complete MiniZinc model through the managed local runtime "
    "WITHOUT solving it — flattening it for the chosen solver to catch syntax, "
    "type, missing-include, invalid-domain, and unsupported-construct errors so "
    "you can repair it before `solve_minizinc_model`. Optional `data` is `.dzn` "
    "text; a parameterized model needs the same `data` you'll pass to the solve "
    "in order to flatten (omit when none is needed). Returns a CheckResult: "
    "`status` (`ok`/`error`/`timeout`), `solver`, `truncated` (output exceeded "
    "the 1 MiB cap; `stdout`/`stderr` are partial and the diagnostic is "
    "`output_truncated`), raw `stdout`/`stderr`, `elapsed_ms`. `ok` means it "
    "compiles, not that it is satisfiable."
)

INSPECT_MINIZINC_MODEL_DESCRIPTION = (
    "Inspect a MiniZinc model's INTERFACE through the managed local runtime "
    "WITHOUT solving it — report which parameters it needs as data, which "
    "variables it outputs, their types (array `dim`, set-ness), and the solve "
    "`method` (`sat`/`min`/`max`), so you can build correct `.dzn` data before "
    "spending a solve. Optional `data` is `.dzn` text run beside the model (omit "
    "when none is needed). Returns a ModelInspectionResult: `status` "
    "(`ok`/`error`/`timeout`), `solver`, `truncated` (output exceeded the "
    "1 MiB cap; `stdout`/`stderr` are partial and the diagnostic is "
    "`output_truncated`), raw `stdout`/`stderr`, `elapsed_ms`, "
    "and — only when `ok` — a structured `interface` with `method`, "
    "`required_parameters`, `output_variables`, `has_output_item`, `globals`, "
    "`included_files`. `required_parameters` is the set STILL needing a value "
    "given any `data` you passed: with no data it is the full required set; "
    "matching data shrinks it, and an empty `required_parameters` means the data "
    'is complete. IMPORTANT: `status="ok"` means only that the interface was '
    "extracted — it is NOT a data-completeness signal; only "
    "`required_parameters == {}` is. `output_variables` is advisory (output "
    "variables, not necessarily every decision variable). Enum-typed entries "
    "appear as `int`; enum names are not surfaced in v1."
)

FIND_UNSAT_CORE_DESCRIPTION = (
    "Diagnose an unsatisfiable MiniZinc model by computing a minimal "
    "unsatisfiable subset (MUS) of its constraints via the managed runtime's "
    "findMUS tool. Use it when solve_minizinc_model returns 'unsatisfiable'. "
    "Optional `data` is `.dzn` text; pass the SAME `data` you solved with (omit "
    "when none is needed). Returns an UnsatCoreResult: `status` "
    "(`mus_found`/`no_core`/`error`/`timeout`), `core`, `message`, `truncated` "
    "(output exceeded the 1 MiB cap; a verdict parsed from the capped transcript "
    "may be incomplete and the diagnostic is `output_truncated`), raw "
    "`stdout`/`stderr`, `elapsed_ms`. `core` is a best-effort structured list "
    "(source span + text) resolved from the MODEL FILE only — a decision "
    "variable assigned in `data` acts as a constraint, so a MUS member can "
    "originate in the data file and appear in authoritative `stdout` but not in "
    "`core`. The subset is MINIMAL but not necessarily the globally smallest; "
    "a model can have several distinct MUSes and this tool returns one."
)

SAVE_VERIFIED_MINIZINC_MODEL_DESCRIPTION = (
    "Save a successful inline MiniZinc workflow to a LOCAL project directory "
    "AFTER re-verifying it through the managed local runtime. The server trusts "
    "no prior claim of success: it re-runs the compile check and solve on "
    "`model` (with optional `data` and inline `checker`), and writes only when "
    "the check is `ok` and the solve is `satisfied`/`optimal` with a clean exit "
    "and no timeout — and, if a checker is supplied, its nested report is "
    "`completed` (ran without machine-readable violation; NOT a proof of "
    "optimality). "
    + _SAVE_TARGET_DIR_RULE
    + " Optional `portfolio_result` (a PortfolioSolveResult from "
    "a MiniZinc solver-portfolio race) attaches that race's attempt table as "
    "PROVENANCE ONLY — never verification evidence; the save still re-runs "
    "check/solve/checker on `model`/`data`/`solver`/`random_seed` fresh and "
    "gates on that alone. Eagerly rejected (MCP error, before any check/solve) "
    "when any of: (a) `portfolio_result.status != 'winner'`; (b) the winning "
    "attempt's `solver` or `seed` does not match this save's "
    "`solver`/`random_seed` (an unseeded winner matches an unseeded save); "
    "(c) the winning attempt's model or data hash does not match "
    "`model`/`data`; (d) the race's shared `solve_controls` "
    "(`free_search`/`parallel`/`all_solutions`/`num_solutions`) do not match "
    "this save's — the save must replay the winning attempt's search "
    "configuration (`timeout_ms`, a budget, is not gated). A `checker_sha256` mismatch is "
    "NOT rejected — it only affects what the persisted log records. When "
    "supplied and every gate passes, `experiment-log.json` is written (role "
    "`experiment_log` in `files`) recording every portfolio attempt (model "
    "index, solver, seed, timeout, state, result status, checker status, "
    "objective, timing), the race's shared solve controls, plus a compact "
    "summary under the manifest's "
    "`verification`. Fixed filenames: `model.mzn`; `data.dzn`, "
    "`checker.mzc.mzn`, and `problem.md` only when `data`, `checker`, and "
    "`problem` (the user's original natural-language text, saved only when "
    "passed) are supplied; `solve-result.json` (the verifying SolveResult); "
    "`experiment-log.json` only when `portfolio_result` is supplied and the "
    "save succeeds; and a `.openconstraint-model.json` manifest recording tool "
    "version, timestamp, solver, the solve controls used, a verification "
    "summary, and per-file sha256 hashes. "
    + _MARKER_GATED_OVERWRITE
    + " Accepts the same `solver`, `timeout_ms`, and solver/search "
    "controls as `solve_minizinc_model` (`free_search`, `parallel`, "
    "`random_seed`, `all_solutions`, and the solver-gated `num_solutions`); an "
    "`-a/-f/-p/-r` control the selected solver does not declare is rejected "
    "before any check, solve, or write. Returns a SaveVerifiedModelResult: "
    "`status` (`saved`/`not_verified`), `message`, the resolved `target_dir`, "
    "`files` (role, bare filename, sha256 — only on `saved`), `check` (always "
    "present), and `solve` (null when the check gate already failed). A model "
    "that fails any verification gate returns `not_verified` with the gating "
    "results and writes NOTHING; argument/path problems are MCP errors. " + _LOCAL_ONLY_GUARANTEE
)

# Shared guidance injected into each path-based file-tool description.
_FILE_TOOL_SHARED_DESCRIPTION = (
    "Reads the model (and optional data) from local FILE PATHS on the server's "
    "machine and runs the managed runtime from the model's own directory, so a "
    "relative `include` resolves like a hand-run `minizinc`. `model_path` is a "
    "required `.mzn` path (must exist, regular file); `data_path` is an optional "
    "`.dzn` path. Pass absolute paths — a relative path resolves against the "
    "server process's working directory, not the client's. A missing/non-file, "
    "empty, or non-UTF-8 model is an MCP error before any run. It reads the "
    "model, optional data, and any `include`d files; it never writes files, "
    "makes network calls, uploads data, or uses a remote solver."
)

CHECK_MINIZINC_FILES_DESCRIPTION = (
    "Compile-check a MiniZinc model from local file paths WITHOUT solving "
    "it — the path-based sibling of `check_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same CheckResult shape (`status` "
    "`ok`/`error`/`timeout`, `solver`, `truncated`, `stdout`, `stderr`, "
    "`elapsed_ms`); `ok` means it compiles, not that it is satisfiable."
)

SOLVE_MINIZINC_FILES_DESCRIPTION = (
    "Solve a constraint or discrete-optimization problem ("
    + _CP_PROBLEM_DOMAINS
    + ") from a MiniZinc model the user already has on disk — the path-based "
    "sibling of `solve_minizinc_model`, reading local file paths. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same SolveResult shape (`status`, `solver`, "
    "`return_code`, `timed_out`, `truncated` (output-cap overrun — see "
    "`solve_minizinc_model`), `elapsed_ms`, `stdout`, `stderr`, `solution`, "
    "`solutions`, `objective`, `statistics`, `checker`) and the same "
    "model-visible `Statistics:` summary whenever the parsed map is non-empty; "
    "copy the entire section rather than summarizing selected fields. Accepts "
    "the same solver/search controls as `solve_minizinc_model` ("
    + _SOLVE_CONTROLS_LIST
    + "), with the same upfront capability rejection of an `-a/-f/-p/-r` control "
    "the selected solver does not declare. Optional `checker_path` points to a "
    "`.mzc`/`.mzc.mzn` checker file, resolved to absolute and validated before "
    "any run; it adds `--solution-checker <path>` to the same invocation, so "
    "search controls compose with checking. When supplied, the result's "
    "`checker` field is the same "
    "nested CheckerReport as `solve_minizinc_model` — `status`, index-aligned "
    "`checks`, and authoritative raw `transcript`."
)

FIND_UNSAT_CORE_FILES_DESCRIPTION = (
    "Diagnose an unsatisfiable MiniZinc model from local file paths by "
    "computing a minimal unsatisfiable subset (MUS) via the managed "
    "runtime's findMUS tool — the path-based sibling of `find_unsat_core`. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same UnsatCoreResult shape (`status` "
    "`mus_found`/`no_core`/`error`/`timeout`, `core`, `message`, `truncated`, "
    "`stdout`, `stderr`, `elapsed_ms`). `core` resolves from the ENTRY MODEL "
    "FILE "
    "only, so a MUS member in an INCLUDED file appears in authoritative "
    "`stdout` but NOT in `core`. The subset is MINIMAL but not necessarily "
    "the globally smallest."
)

INSPECT_MINIZINC_FILES_DESCRIPTION = (
    "Inspect a MiniZinc model's INTERFACE from local file paths WITHOUT "
    "solving it — the path-based sibling of `inspect_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same ModelInspectionResult shape (`status` "
    "`ok`/`error`/`timeout`, `solver`, `truncated`, `stdout`, `stderr`, "
    "`elapsed_ms`, and the structured `interface` only when `ok`). "
    "`required_parameters` lists "
    "the parameters still needing a value given any `data_path`; an empty "
    '`required_parameters` means the data is complete, but `status="ok"` '
    "alone does NOT — it means only that the interface was extracted. Enum "
    "names are not surfaced in v1."
)

SUBMIT_SOLVE_JOB_DESCRIPTION = (
    "Submit a MiniZinc solve as a BACKGROUND JOB and return immediately, so a "
    "hard solve cannot hit a synchronous MCP client timeout. Takes the same "
    "inline surface as `solve_minizinc_model` — `model` (full source), optional "
    "`data`/`checker`, `solver`, `timeout_ms`, and the solver/search controls "
    + _SOLVE_CONTROLS_LIST
    + ". Argument errors (empty model, non-positive timeout, bad "
    "`parallel`/`num_solutions`) and an `-a/-f/-p/-r` control the selected "
    "solver does not declare are reported synchronously as MCP errors at "
    "admission, before any job exists. Returns a SolveJobStatus with a "
    "server-generated opaque `job_id` and `state` `queued` or `running`; poll "
    "with `get_solve_job(job_id)` and stop with `cancel_solve_job(job_id)`. "
    "Admission is BOUNDED: at most a fixed number of jobs run at once, further "
    "submits sit `queued` up to a cap, and a submit beyond that is REJECTED with "
    "an MCP error (retry once a running job finishes). "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_solve_job")
    + _LOCAL_ONLY_GUARANTEE
)

GET_SOLVE_JOB_DESCRIPTION = (
    "Poll a background solve job by its `job_id` (from `submit_solve_job`). "
    "Returns a SolveJobStatus: `job_id`, `state`, `solver`, `timeout_ms`, "
    "`submitted_at_ms`, `started_at_ms`, `finished_at_ms`, `elapsed_ms`, an "
    "optional `result` (the full SolveResult), and an optional `message`. "
    "`state` is one of `queued`, `running`, `succeeded`, `failed`, `timeout`, "
    "`cancelled`. "
    + _job_result_contract("a SOLVER-level `error` verdict")
    + "A `timeout` job still carries its partial SolveResult. While "
    "`running`, only `state` + `elapsed_ms` advance; live mid-solve statistics "
    "are not provided. " + _PACE_POLLING_NOTE + " On a `succeeded` or `timeout` "
    "job, present `result` as the synchronous solve tools require: lead with the "
    "plain-language status and the solution in the user's terms, and include the "
    "COMPLETE model-visible `Statistics:` section whenever `result.statistics` "
    "is non-empty — do not omit, summarize, or condense it to selected fields. "
    + _UNKNOWN_JOB_ID_ERROR
)

CANCEL_SOLVE_JOB_DESCRIPTION = (
    "Request cancellation of a background solve job by `job_id`. A job still "
    "`queued` is dropped before it starts; a `running` job has its managed "
    "MiniZinc process tree (the solver children too) terminated. "
    + _cancellation_idempotent_note("`succeeded`/`failed`/`timeout`/`cancelled`")
    + "Returns the SolveJobStatus; the job reaches "
    "`cancelled` (with `result is None`) once the worker observes the request — "
    "poll `get_solve_job` to confirm the terminal state. " + _UNKNOWN_JOB_ID_ERROR
)

LIST_SOLVE_JOBS_DESCRIPTION = (
    "List the currently retained background solve jobs as SolveJobStatus "
    "entries (one per job), covering every state from `queued` to terminal. "
    + _REGISTRY_NOTE
    + " "
    + _NO_ARGS_LIST_TOOL
)

SUBMIT_PORTFOLIO_JOB_DESCRIPTION = (
    "Submit a solver portfolio as a BACKGROUND JOB and return immediately, so a "
    "hard race cannot hit a synchronous MCP client timeout. This is the "
    "supported way to run a portfolio: race several MiniZinc formulations, "
    "solvers, and seeds against ONE instance through the managed local runtime "
    "and return the SINGLE winner — a LOCAL race over the background-solve "
    "machinery, no remote/distributed solving, upload, or telemetry. Use it for "
    "a hard instance where you don't know which formulation or solver wins; an "
    "ordinary single-solver `solve_minizinc_model` is still the right first "
    "attempt. Takes the same inline surface as `solve_minizinc_model` — optional "
    "shared `data`/`checker` and the non-seed controls `free_search`, "
    "`parallel`, `all_solutions`, and the solver-gated, satisfaction-only "
    "`num_solutions` — applied identically to every attempt. Instead of one "
    "`model`/`solver`, pass a non-empty `models` list (alternative ENCODINGS of "
    "the same instance, sharing the one `data`/`checker` and controls — NOT a "
    "batch of different problems) and a non-empty `solvers` list. A high-value "
    "variant for a stalled CSP is a model with a restart annotation (e.g. "
    "`restart_luby`/`restart_geometric`) on its solve item: paired with multiple "
    "`seeds`, randomized restart escapes the heavy-tailed search that traps a "
    "single deterministic run. Restart-aware solvers (Gecode/Chuffed) honor "
    "these — include them in `solvers`; CP-SAT ignores them and runs its own "
    "restarts. Do NOT pass `random_seed`: use `seed_count` (shorthand) or "
    "`seeds` (exact values). With `seed_count == 1` and no `seeds`, each (model, "
    "solver) runs once UNSEEDED; with `seed_count > 1` each runs with seeds "
    "`1..seed_count` (every selected solver must support `-r`). With "
    "`seeds=[42, 123]` the portfolio uses exactly those seeds in order, with no "
    "extra unseeded attempt; `seeds` must be non-empty, contain no duplicates, "
    "cannot combine with `seed_count != 1`, and also requires every selected "
    "solver to support `-r`. There is no generic `solver_options`, `extra_args`, "
    "or raw MiniZinc flag passthrough. The plan is the full cross-product — "
    "`len(models) * len(solvers) * seed-count` attempts, model index varying "
    "fastest so the first attempts span distinct formulations. There is NO portfolio-side "
    "cap: every attempt is admitted; up to `max_running_jobs` (default 4) race "
    "simultaneously and the rest QUEUE, starting as running slots free, and a "
    "decisive running winner cancels the still-queued attempts before they "
    "start. Validation, capability enforcement, and admission happen "
    "SYNCHRONOUSLY here: an empty `models`/`solvers`, a bad control, an "
    "`-a/-f/-p/-r` flag the selected solver does not declare, or a plan that "
    "exceeds the job registry's running+queued capacity is reported at once as "
    "an MCP error, before any job exists. The attempts then run as ordinary jobs "
    "on the SAME bounded solve registry as `submit_solve_job` (so they count "
    "against its capacity and also appear in `list_solve_jobs`), and the winner "
    "is selected when you poll. Returns a PortfolioJobStatus with a "
    "server-generated opaque `job_id` and `state` `running`; poll with "
    "`get_portfolio_job(job_id)` — which advances the race and cancels the "
    "losers once a winner emerges — and stop the whole race early with "
    "`cancel_portfolio_job(job_id)`. "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_portfolio_job")
    + _LOCAL_ONLY_GUARANTEE
)

GET_PORTFOLIO_JOB_DESCRIPTION = (
    "Poll a background portfolio job by its `job_id` (from "
    "`submit_portfolio_job`). This also DRIVES the race: each poll selects a "
    "winner once one attempt reaches a decisive verdict and cancels the "
    "still-running losers, so poll until terminal rather than walking away. "
    "Returns a PortfolioJobStatus: `job_id`, `state`, `per_attempt_timeout_ms`, "
    "`submitted_at_ms`, `started_at_ms`, `finished_at_ms`, `elapsed_ms`, an "
    "optional `result` (the full PortfolioSolveResult), and an optional "
    "`message`. `state` is one of `running`, `succeeded`, `cancelled`. CONTRACT: "
    "`result` is present exactly when `state` is `succeeded`, absent for "
    "`running`/`cancelled` — so branch on `state`, not on `result`. A race that "
    "found no decisive winner is still `succeeded` (the orchestration completed) "
    "carrying a PortfolioSolveResult whose `status` is `no_winner`; a "
    "per-attempt failure is recorded in that result's attempts table, not as a "
    "failed job. `cancelled` means the client stopped the race. While "
    "`running`, only `state` + `elapsed_ms` advance; mid-race statistics are not "
    "provided. PACE polling against the race budget: `per_attempt_timeout_ms` "
    "bounds each attempt, not the whole race — a plan with more attempts than the "
    "server's running-job limit runs them in queued waves, so a race with no early "
    "decisive winner can take several multiples of `per_attempt_timeout_ms`. Wait a "
    "fraction of that budget between polls rather than looping tightly. On a "
    "`succeeded` job, present "
    "`result` like a single `solve_minizinc_model`: lead with the winner's "
    "model/solver/seed/status, then the winning solve (solution + the COMPLETE "
    "`Statistics:` section) and the per-attempt table. The winning FORMULATION "
    "is `models[attempts[winner_index].model_index]`. " + _UNKNOWN_JOB_ID_ERROR
)

CANCEL_PORTFOLIO_JOB_DESCRIPTION = (
    "Request cancellation of a background portfolio job by `job_id`, stopping the race "
    "AND every still-running attempt (each attempt's managed MiniZinc process tree is "
    "terminated). "
    + _cancellation_idempotent_note("`succeeded`/`cancelled`")
    + "Returns the PortfolioJobStatus; the job reaches "
    "`cancelled` (with `result is None`) once the race observes the request — poll "
    "`get_portfolio_job` to confirm the terminal state. " + _UNKNOWN_JOB_ID_ERROR
)

LIST_PORTFOLIO_JOBS_DESCRIPTION = (
    "List the currently retained background portfolio jobs as PortfolioJobStatus "
    "entries (one per job), covering `running` and the terminal states. "
    + _REGISTRY_NOTE
    + " "
    + _NO_ARGS_LIST_TOOL
)

_RUN_CPSAT_PYTHON_HEAD = (
    "Solve a constraint or discrete-optimization problem ("
    + _CP_PROBLEM_DOMAINS
    + ") by executing a COMPLETE OR-Tools CP-SAT Python script you have already "
    "written, never the user's prose, in a bounded child "
    "process; returns a structured CpsatPythonResult. The script runs under "
    "the same Python interpreter as the server, with `ortools` and the stdlib "
    "available. " + _CPSAT_JSON_CONTRACT + " "
    "Valid `status` values: `optimal`, `feasible`, `infeasible`, `unknown`, `error`. "
)

# Full-only: names the `cpsat_python_solution_workflow` prompt, which the core profile hides.
_RUN_CPSAT_PYTHON_PROMPT_REF = (
    "Use the `cpsat_python_solution_workflow` prompt to generate conforming scripts. "
)

_RUN_CPSAT_PYTHON_MID = (
    "Returns a CpsatPythonResult: `status` (one of the above, or `timeout` if the "
    "process exceeded `script_timeout_ms`), `solution` (the parsed dict or null), "
    "`objective` (parsed float/int or null), `best_objective_bound` (parsed "
    "float/int or null; OR-Tools' `solver.best_objective_bound` — diagnostic "
    'only, not a proven objective, and useful even on `status="unknown"` when '
    "no incumbent was found), `stdout`, `stderr`, `return_code` "
    "(null on timeout), `timed_out`, `truncated` (combined output exceeded the "
    "1 MB cap — output is cut there and the child killed if still running), "
    "`duration_ms`. A non-zero exit code, missing/unparseable JSON, or an "
    'off-vocabulary status string all yield `status="error"` with details in '
    "`stderr`/`stdout`. "
    "On `timeout`, `solution`/`objective`/`best_objective_bound` carry the last "
    "intermediate result block the script printed (the child runs unbuffered, "
    "so a best-so-far emitted from a CpSolverSolutionCallback survives), else "
    "null. "
)

# Full-only: names the `submit_portfolio_job` MiniZinc portfolio path, hidden in core.
_RUN_CPSAT_PYTHON_PORTFOLIO_REF = (
    "For a HARD "
    "instance — status is unknown/timeout, or incumbent quality is unclear — "
    "consider the MiniZinc portfolio path (`submit_portfolio_job`) to race "
    "multiple formulations, solvers, and seeds for the same problem. "
)

_RUN_CPSAT_PYTHON_TAIL = (
    "This tool "
    "has no `seed`/`config` parameters; it always clears "
    "`OPENCONSTRAINT_MCP_CPSAT_SEED`/`OPENCONSTRAINT_MCP_CPSAT_CONFIG` for the "
    "child so a value inherited from the server's own launch environment cannot "
    "leak in. " + _CPSAT_CHILD_POSTURE
)

# FULL-ONLY, and deliberately a second copy of the no-stdin rule the full server
# `instructions` CP-SAT bullet also states — the same reasoning as
# _CP_PROBLEM_DOMAINS above: tool descriptions and instructions are separate
# advertised fields a host may consume, truncate, or rank independently, and a
# script that reads stdin fails the whole run rather than merely reading oddly.
# Keep it OUT of _CPSAT_CHILD_POSTURE and the shared _HEAD/_MID/_TAIL fragments:
# those reach both profiles, and core metadata has no headroom to spend on it.
_CPSAT_NO_STDIN_FULL = (
    "The child runs with NO stdin: a script that calls `input()` or reads "
    "`sys.stdin` hits an immediate EOF, prints no envelope, and the run comes "
    "back as an error."
)

RUN_CPSAT_PYTHON_DESCRIPTION = (
    _RUN_CPSAT_PYTHON_HEAD
    + _RUN_CPSAT_PYTHON_PROMPT_REF
    + _RUN_CPSAT_PYTHON_MID
    + _RUN_CPSAT_PYTHON_PORTFOLIO_REF
    + _RUN_CPSAT_PYTHON_TAIL
    + " "
    + _CPSAT_NO_STDIN_FULL
)
RUN_CPSAT_PYTHON_DESCRIPTION_CORE = (
    _RUN_CPSAT_PYTHON_HEAD + _RUN_CPSAT_PYTHON_MID + _RUN_CPSAT_PYTHON_TAIL
)

_RUN_CPSAT_PYTHON_FILE_HEAD = (
    "Solve a constraint or discrete-optimization problem ("
    + _CP_PROBLEM_DOMAINS
    + ") by executing an OR-Tools CP-SAT Python script the user already has on "
    "disk, from a LOCAL file path — the "
    "path-based sibling of `run_cpsat_python`. Pass `script_path` instead of "
    "pasting the whole source, so iterating on a local file does not mean "
    "re-copying it on every call. The script runs with its working directory set "
    "to the file's own directory, so a relative `open()` of a sibling data file "
    "or `import` of a helper module resolves (mirroring the MiniZinc file tools). "
    "`script_path` is resolved to absolute and validated before any run: a "
    "missing path, a non-file, an empty/whitespace-only script, or non-UTF-8 "
    "content is rejected with an actionable MCP error and nothing runs. Same "
    "execution contract, output cap, timeout, and tree-kill as "
    "`run_cpsat_python`: " + _CPSAT_JSON_CONTRACT + " "
)

# The returned-shape sentence, split out of the head so the full profile can
# name the checked sibling's wider return type without changing a single byte
# of the core profile's advertised description.
_RUN_CPSAT_PYTHON_FILE_SHAPE_CORE = (
    "The returned CpsatPythonResult has the identical shape (`status`, "
    "`solution`, `objective`, `best_objective_bound`, `stdout`, `stderr`, "
    "`return_code`, `timed_out`, "
    "`truncated`, `duration_ms`), including `timeout` partial recovery. "
)

_RUN_CPSAT_PYTHON_FILE_SHAPE_FULL = (
    "The returned CpsatPythonResult has the identical shape as "
    "`run_cpsat_python`'s, including `timeout` partial recovery, and carries NO "
    "checker fields — this tool runs the script and reports what it printed, "
    "nothing more. To also VERIFY the result against a checker script that is "
    "already on disk, call `run_cpsat_python_file_checked` instead: same "
    "`script_path`/`args`/`script_timeout_ms`, plus a required `checker_path`, and it "
    "returns a CpsatPythonCheckedResult (every CpsatPythonResult field plus `checker`, "
    "`checker_skipped_reason`, `checker_timeout_ms`, and `checker_test`). "
)

_RUN_CPSAT_PYTHON_FILE_SEED_CONFIG = (
    "Optional `seed` (a non-bool integer in the CP-SAT `random_seed` "
    "signed-int32 range) and `config` (a JSON object) are REPLAY aids for "
    "re-running a saved seeded/configured artifact through this file tool "
    "instead of exporting environment variables by hand: `seed` sets "
    "`OPENCONSTRAINT_MCP_CPSAT_SEED`, and a non-empty `config` is written to a "
    "temp file whose path is set as `OPENCONSTRAINT_MCP_CPSAT_CONFIG` — the same "
    "two cooperative, opt-in "
)

# Full-only: both fragments name `save_verified_cpsat_python`, hidden in core.
# The core protocols phrase drops the cross-tool reference; the checked-replay
# sentence is dropped entirely (its whole subject is the hidden save tool).
_RUN_CPSAT_PYTHON_FILE_PROTOCOLS_FULL = "protocols as `save_verified_cpsat_python`'s "
_RUN_CPSAT_PYTHON_FILE_PROTOCOLS_CORE = "`seed`/`config` protocols. "

_RUN_CPSAT_PYTHON_FILE_MID = (
    "An empty `config` (`{}`) is identical to omitting it. "
    "When `seed`/`config` are omitted, both protocol environment variables are "
    "explicitly cleared for the child rather than left to inherit a stale value "
    "from the server's own launch environment. "
)

# Shared by every tool that forwards `args` to a child — `run_cpsat_python_file`
# (both profiles), `run_cpsat_python_file_checked`, `run_cpsat_python_experiment`,
# and `submit_cpsat_python_file_job` — so they cannot drift on what they reject.
_CPSAT_ARGS_LIMITS = (
    "`args` is a flag/path list, not a data channel: an entry containing a NUL, "
    "or a list whose combined UTF-8 encoding exceeds 32 KiB, is rejected with an "
    "actionable MCP error UP FRONT rather than surfacing as a spawn-time "
    # Why each limit exists (subprocess raises on a NUL, the OS refuses an
    # oversized argv) is omitted: the client cannot act on either, and this
    # fragment is spliced into four tool descriptions.
    "failure. Pass bulk input in a file the script opens. "
)

_RUN_CPSAT_PYTHON_FILE_ARGS = (
    "Optional `args` (a list of strings) is appended after the script path, so "
    "the script reads it as `sys.argv[1:]` — pass it for a script that takes its "
    'data file or a flag on the command line (e.g. `args=["data_ft10.json"]` '
    "runs an `examples/job_shop/model.py`-style script against that instance "
    "instead of its hardcoded default, with no edit to the source). Omitting it "
    "runs the script with no arguments. " + _CPSAT_ARGS_LIMITS
)

_RUN_CPSAT_PYTHON_FILE_CHECKED_REPLAY_FULL = (
    "To replay a `checked`-level save at its own verification level, point "
    "`run_cpsat_python_file_checked` at the saved `model.py` and `checker.py` "
    "(passing the saved `problem` text, plus `seed`/`config` when the manifest "
    "records them). Use `save_verified_cpsat_python` with `verify_only=true` "
    "when you also need the saved objective `expectation` gate re-run — that "
    "mode needs no `target_dir` and ignores one if passed, so to persist the "
    "replay itself omit `verify_only` (or pass `verify_only=false`) with a real "
    "`target_dir`. "
)

RUN_CPSAT_PYTHON_FILE_DESCRIPTION = (
    _RUN_CPSAT_PYTHON_FILE_HEAD
    + _RUN_CPSAT_PYTHON_FILE_SHAPE_FULL
    + _RUN_CPSAT_PYTHON_FILE_SEED_CONFIG
    + _RUN_CPSAT_PYTHON_FILE_PROTOCOLS_FULL
    + "`seed`/`config`. "
    + _RUN_CPSAT_PYTHON_FILE_MID
    + _RUN_CPSAT_PYTHON_FILE_ARGS
    + _RUN_CPSAT_PYTHON_FILE_CHECKED_REPLAY_FULL
    + _CPSAT_CHILD_POSTURE
    + " "
    + _CPSAT_NO_STDIN_FULL
)
RUN_CPSAT_PYTHON_FILE_DESCRIPTION_CORE = (
    _RUN_CPSAT_PYTHON_FILE_HEAD
    + _RUN_CPSAT_PYTHON_FILE_SHAPE_CORE
    + _RUN_CPSAT_PYTHON_FILE_SEED_CONFIG
    + _RUN_CPSAT_PYTHON_FILE_PROTOCOLS_CORE
    + _RUN_CPSAT_PYTHON_FILE_MID
    + _RUN_CPSAT_PYTHON_FILE_ARGS
    + _CPSAT_CHILD_POSTURE
)

RUN_CPSAT_PYTHON_FILE_CHECKED_DESCRIPTION = (
    "Solve a constraint or discrete-optimization problem ("
    + _CP_PROBLEM_DOMAINS
    + ") AND verify the result, in one synchronous call, from two LOCAL file "
    "paths the user already has on disk: `script_path` (an OR-Tools CP-SAT "
    "Python script) and the REQUIRED `checker_path` (a checker script). This is "
    "`run_cpsat_python_file` plus a mandatory verification pass — pick it when "
    "an independent check of the solution matters; pick `run_cpsat_python_file` "
    "when it does not. "
    "Each script runs in its OWN directory (`cwd` = that file's parent), so a "
    "relative `open()` of a sibling data or reference file resolves on both "
    "sides. Both paths are resolved and validated (exists / regular file / "
    "non-empty / UTF-8) BEFORE anything runs; a bad path is an actionable MCP "
    "error naming the offending parameter and no child is spawned. "
    "The model script follows the usual contract: " + _CPSAT_JSON_CONTRACT + " "
    "The checker protocol: the server writes a temporary JSON payload "
    '(`{"problem": <str|null>, "solution": {...}, "objective": <float|int|null>, '
    '"solver_status": "<status>"}`) and passes its absolute path as the '
    "checker's `sys.argv[1]`; the checker must print, as its FINAL stdout line, "
    'one JSON object `{"status": "accepted"|"rejected"|"error", "errors": '
    '[...], "details": {...}}`. '
    "Pass `problem` — the instance text or JSON the checker validates against — "
    "whenever the checker is data-driven; it is optional in the signature but a "
    "checker that reads `payload['problem']` will reject without it, and it "
    "CANNOT be inferred from `args` (those name a data file relative to the "
    "script's directory, not the instance itself). "
    "`checker_timeout_ms` defaults to `script_timeout_ms`; when `test_checker` is on, "
    "an omitted value is capped at the largest checker timeout that fits the "
    "synchronous wall-clock budget, never derived below 2000 ms (a `script_timeout_ms` "
    "that would force it lower is rejected). `args` is appended after `script_path` as "
    "the model script's `sys.argv[1:]`; `seed`/`config` are the same replay aids "
    "`run_cpsat_python_file` documents. "
    + _CPSAT_ARGS_LIMITS
    + "Returns a CpsatPythonCheckedResult: every CpsatPythonResult field, plus "
    "`checker` (the CpsatCheckerReport, whose `status` is the verdict), "
    "`checker_skipped_reason` (set INSTEAD of `checker` when the run produced no "
    "checkable incumbent — the two are mutually exclusive), `checker_timeout_ms`, "
    "and `checker_test` (the self-test report; null unless `test_checker` opted "
    "in). A checker that rejects, times out, crashes, or emits "
    "garbage does NOT fail the call: the model result always survives and the "
    "verdict is reported. The top-level `diagnostic` composes the run and "
    "baseline checker — a "
    "run timeout wins, else a failed checker overrides, else the run's own "
    "diagnostic — so `diagnostic: null` remains the clean-success signal. "
    "A timed-out run WITH a recovered incumbent is still checked; one without "
    "is skipped. "
    "`test_checker` (OPT-IN, default false) re-runs an `accepted` checker against "
    "up to four deterministic generic mutations: objective, list drop/duplicate, "
    "and numeric-or-boolean field perturbation. `checker_test` carries one "
    "COMPACT `mutations` row per probe — `name` plus either a verdict "
    "(`status`, an 8 KiB-capped `errors` prefix, `duration_ms`) or a "
    "`skipped_reason`, never the "
    "mutant's raw output — and `rejected_count`/`accepted_count`; the counts "
    "exclude errors, timeouts, and skipped probes. The baseline is not repeated "
    "there: `checker` above is the one full report returned. "
    "The generic probes are not known-invalid: a rejection is non-vacuity "
    "evidence, while zero rejections is inconclusive. Self-testing is "
    "synchronous-only and has a projected 120 s cap. Without `test_checker`, "
    "`script_timeout_ms` has no upper bound. For a longer run, use "
    "`submit_cpsat_python_file_job`, which takes the same path-based "
    "`checker_path` (so a sibling-file checker still resolves) but offers no "
    "self-test. "
    "The checker is a correctness gate against an INCORRECT script, not a "
    "security boundary against a hostile one: it is a second unsandboxed local "
    "child with exactly the same posture as the model script. " + _CPSAT_CHILD_POSTURE
)

SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION = (
    "Start here for a plain-language constraint or discrete-optimization "
    "problem (" + _CP_PROBLEM_DOMAINS + "). Guides the MCP client's LLM "
    "through one compact backend-neutral loop: analyze the problem, choose "
    "MiniZinc or OR-Tools CP-SAT Python by problem shape, draft a complete "
    "model or script, verify and run it with the local tools (using the "
    "path-based tools when the artifact already exists on disk), and present "
    "the result in the user's own terms."
)

MINIZINC_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION = (
    "Guide the MCP client's LLM through translating a natural-language "
    "constraint or optimization problem into MiniZinc and running it "
    "through the local managed runtime (via solve_minizinc_model when "
    "available, otherwise by walking the user through the "
    "openconstraint-mcp CLI to set up and invoke the managed runtime "
    "manually — never via a bare PATH-based minizinc). The MiniZinc peer "
    "of the cpsat_python_solution_workflow prompt: pick it for expressive global "
    "constraints, `.dzn` data files, checker verification, portfolio "
    "racing, or `num_solutions` enumeration."
)

RUN_CPSAT_PYTHON_EXPERIMENT_DESCRIPTION = (
    "Run a list of EXPLICIT attempts — each a complete, independent OR-Tools "
    "CP-SAT Python script variant, optionally paired with a `seed` and/or a "
    "cooperative `config` — and return the best ACCEPTED result plus a compact "
    "attempt table. This generalizes a seed sweep into explicit attempts: the "
    "CLIENT proposes every attempt (source variants, config variants, or both); "
    "the server never generates, diffs, or merges attempts, and never sets "
    "OR-Tools parameters directly. "
    "Required: `attempts` (non-empty list of {`name` (optional; defaults to "
    "`attempt-{index}`, and every resolved name — explicit or defaulted — must "
    "be unique), `source` (a non-empty inline script) OR `script_path` (a local "
    "path to an existing UTF-8 Python script) [EXACTLY ONE of the two, never "
    "both and never neither], `args` (optional list of strings appended after "
    "`script_path` as the child's `sys.argv[1:]`; rejected when supplied "
    "alongside `source` rather than silently ignored), `seed` (optional "
    "non-bool integer "
    "in the CP-SAT random_seed signed-int32 range), `config` (optional JSON "
    "object, default `{}`), `script_timeout_ms` (optional per-attempt override)}). "
    + _CPSAT_ARGS_LIMITS
    + "A `script_path` attempt runs with `cwd` set to the script's own parent "
    "directory — exactly like `run_cpsat_python_file` — so a relative `open()` "
    "of a sibling data file resolves, and several attempts can race existing "
    "on-disk scripts against shared data with no duplicated source in the "
    "request. Every attempt is validated (including each `script_path`) BEFORE "
    "any child runs, so one bad path rejects the whole call. `checker` and "
    "`problem` remain INLINE TEXT for the whole experiment — this tool has no "
    "`checker_path`; only `attempts[i]` gained a path option. NOTE: an attempt "
    "that ran from `script_path` is marked `used_script_path: true` and CANNOT "
    "be used as `save_verified_cpsat_python` provenance (see that tool). "
    "Optional: `objective_sense` ('maximize'|'minimize' for optimization; omit "
    "or pass null for feasibility), `default_script_timeout_ms` "
    "(fallback for attempts with no `script_timeout_ms`), `max_parallel_attempts` "
    "(default 1 = serial; capped at min(server CPU count, 4) and rejected above "
    "that), `problem` (forwarded to the checker payload), `checker` (a Python "
    "checker source string), `checker_timeout_ms` (defaults to the effective "
    "per-attempt timeout), `include_winner_stdout` (default `true`; pass "
    "`false` to omit the winner's raw `stdout` from the returned result — "
    "`solution`/`objective`, the parsed structured answer, are unaffected; "
    "for a well-behaved script `stdout` is a redundant raw-text copy of the "
    "same JSON). "
    "Two cooperative protocols, both OPT-IN for the attempt's script: `seed` "
    "sets `OPENCONSTRAINT_MCP_CPSAT_SEED`; a non-empty `config` is written to a "
    "temp JSON file and its path set as `OPENCONSTRAINT_MCP_CPSAT_CONFIG`. A "
    "script that ignores either env var simply runs unaffected — the server "
    "cannot force either into arbitrary Python. An empty `config` (`{}`) is "
    "identical to omitting it: no temp file, no env var, `config_sha256` null. "
    "PARALLELISM: attempts run through a bounded worker pool sized by "
    "`max_parallel_attempts`; coordinate it with each script's own "
    "`solver.parameters.num_workers` — `max_parallel_attempts * num_workers` "
    "oversubscribing the machine can make runs slower and less stable. When an "
    "attempt's `config` sets a `num_workers` key, the server checks "
    "`max_parallel_attempts * num_workers` against this machine's CPU count and "
    "adds an advisory entry to the result's `warnings` list if it's exceeded — "
    "a heuristic limited to that one convention, blind to `num_workers` set "
    "any other way (e.g. hardcoded). Results "
    "are always returned in ORIGINAL attempt order, and winner tie-breaks use "
    "that same order, never completion order. "
    "Acceptance is two ordered gates: base acceptance (status "
    "`optimal`/`feasible`/`timeout`, a non-empty solution, and — in "
    "optimization mode only — a finite numeric objective), then — only for base-eligible "
    "attempts — the optional checker gate (accepted iff the checker returns "
    "`accepted`). The winner is the accepted attempt with the best objective "
    "for `objective_sense` (skipped in feasibility mode, where objective is "
    "not required), ties broken by stronger status (optimal > feasible > "
    "timeout), then fastest `duration_ms`, then earliest attempt order. "
    "BUDGET GATE: synchronous and rejected UP FRONT (before any child runs) "
    "when its projected wall-clock budget — batched by `max_parallel_attempts`, "
    "using each attempt's effective timeout, checker timeout when present, and "
    "a conservative per-child timeout/kill overhead — exceeds a fixed cap; "
    "reduce attempt count/timeouts or raise `max_parallel_attempts` to fit. "
    "The rejection breaks the projected total down by the slowest attempt's "
    "components, so the bottleneck is visible. THIS TOOL IS FOR "
    "COMPARING MULTIPLE short/medium attempts in one call, not for running one "
    "long attempt — for a SINGLE attempt expected to approach or exceed this "
    "cap, use `run_cpsat_python` instead, which has no multi-attempt budget "
    "ceiling. "
    "Returns a CpsatPythonExperimentResult: `status` ('winner'|'no_winner'), "
    "`winner_index`/`winner_name`/`winner` (a full CpsatPythonResult, all "
    "present iff 'winner'), `attempts` (every attempt, accepted or not, with "
    "its resolved `name`, `source_sha256` (the inline text's hash, or the "
    "on-disk file's raw-byte hash), `config_sha256`, `used_script_path`, and a "
    "diagnostic "
    "`best_objective_bound` — useful even on an `unknown`/rejected attempt "
    "with no incumbent, and never used for acceptance or winner selection), "
    "`elapsed_ms`, "
    "`objective_sense` (or null for feasibility), `selection_policy`, "
    "`source_sha256` (index-aligned with `attempts`), `checker_sha256`, "
    # The reproducibility guidance itself is NOT restated here: experiment.py's
    # _REPRODUCIBILITY_WARNING carries the same text — why a winner may not
    # replay, and what to set for a stronger one — into `warnings` on EVERY
    # winner, which is the only outcome it applies to.
    "`problem_sha256`, `warnings` (advisory strings, always including a "
    "reproducibility disclaimer on a winner — an experiment winner is ONE "
    "OBSERVED RUN, not a guarantee — plus the num_workers-oversubscription "
    "warning when triggered; empty otherwise). "
    "A `timeout` winner is "
    "REPORTABLE, not SAVABLE — `save_verified_cpsat_python`'s reported gate "
    "still requires `optimal`/`feasible`. Pass this result's `experiment_result` "
    "to `save_verified_cpsat_python` to persist the winner with provenance; "
    "that save re-verifies the winner fresh and NEVER trusts this result as "
    "evidence. " + _CPSAT_CHILD_POSTURE
)

SAVE_VERIFIED_CPSAT_PYTHON_DESCRIPTION = (
    "Freshly re-run the supplied CP-SAT Python `source` and persist it to a LOCAL "
    "directory only when all applicable save gates pass. " + _SAVE_TARGET_DIR_RULE + " "
    "`target_dir` is required for a save but NOT for `verify_only=true`. It runs the "
    "SAME solver child and the SAME gates in the SAME order, skipping only save-target "
    "validation and persistent writes; supplied `target_dir` and `overwrite` are "
    "ignored. A passing verify-only run returns `reason=null` with "
    "`saved=false`, `target_dir=null`, and no `files`; a failing one is identical to "
    "a failed save. "
    "Gate order (reported → expectation → checker): "
    "(1) Reported gate — always applied: `status` must be `optimal`/`feasible` "
    "AND `solution` must be non-empty. "
    "(2) Expectation gate — optional: `expectation` requires the reported objective "
    "to meet a numeric threshold. This is a QUALITY GATE, not an optimality proof. "
    "(3) Checker gate — optional: supply `checker` (a Python script source string) "
    "to validate the solution against problem-specific constraints. The checker "
    "receives a payload JSON path as its first positional argument and must emit one "
    "JSON object as its final stdout line with required `status` "
    "(`accepted`|`rejected`|`error`) and `errors` (a string list), plus optional "
    "object-valued `details`. "
    "Only `accepted` with an empty `errors` list passes. Supply `checker_timeout_ms` "
    "to override the checker's timeout (defaults to `script_timeout_ms`). "
    "Optional `seed` (a non-bool integer in the CP-SAT random_seed signed-int32 "
    "range) and optional `config` (a JSON object; `{}` is treated as omitted) are "
    "replay aids for this one re-run, never gate changes: the re-run sets "
    "`OPENCONSTRAINT_MCP_CPSAT_SEED` and writes `config` to a temp file named by "
    "`OPENCONSTRAINT_MCP_CPSAT_CONFIG`, so a cooperating script picks both up. On "
    "a successful save, the manifest records a supplied `seed`, non-empty `config` is "
    "persisted as `replay-config.json`, and `model.py` is byte-for-byte the submitted "
    "`source` encoded as UTF-8; it carries only its own seed fallback. "
    "Optional `experiment_result` from `run_cpsat_python_experiment` is PROVENANCE "
    "ONLY, never verification evidence. It must be self-consistent with this save "
    "request or the save is REJECTED before any child runs: `status=='winner'`, and "
    "at least one ACCEPTED attempt in "
    "`experiment_result.attempts` whose `source_sha256` matches `source`, `seed` "
    "matches the supplied `seed`, and `config_sha256` matches the canonical hash of "
    "the supplied `config` (not necessarily the experiment's own `winner_index`). "
    "A matching attempt that ran from `script_path` (`used_script_path: true`) does "
    "NOT count — this save's re-run is always inline `source` in a fresh temp-dir "
    "`cwd`. At least one matching attempt must be an inline-`source` one. On a "
    "successful save the attempt table is written as `experiment-log.json`, a "
    "provenance SUMMARY (per-attempt hashes and scalar outcomes) that archives no "
    "attempt's `config`. "
    "Seed/config provenance cannot guarantee bit-for-bit replay: CP-SAT randomness, "
    "parallel search, or script nondeterminism may produce a different solution; no "
    "built-in gate compares it with a prior run. "
    "Fixed filenames: `model.py` (the script); `problem.txt` when `problem` is "
    "supplied; `checker.py` and `solution.json` when a checker is supplied; "
    "`replay-config.json` when `config` is non-empty; `experiment-log.json` when "
    "`experiment_result` is supplied; and a `.openconstraint-model.json` manifest "
    "recording tool version, timestamp, verification level, expectation settings, "
    "checker summary (status/error_count/duration/timed_out/truncated only — no "
    "free text), a compact experiment-log summary, and per-file sha256 hashes. "
    + _MARKER_GATED_OVERWRITE
    + " "
    "Read the returned SaveVerifiedPythonResult as two independent things: `saved` "
    "is PERSISTENCE only, never the verdict; the verdict is `reason` (null iff every "
    "supplied gate passed) plus `reported_passed`, `expectation_passed` (null when "
    "that gate was not evaluated), `checker`, and `verification_level` — the highest "
    "gate that passed. " + _CPSAT_CHILD_POSTURE
)

CPSAT_PYTHON_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION = (
    "Guide the MCP client's LLM through writing an OR-Tools CP-SAT Python "
    "script that conforms to the run_cpsat_python output contract and "
    "running it via run_cpsat_python. The CP-SAT peer of the "
    "minizinc_solution_workflow prompt: pick it for zero-install solving (no "
    "managed runtime needed), pure integer/scheduling problems, imperative "
    "pre-processing, or custom data structures."
)

AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION = (
    "Guide the MCP client's LLM through a three-tier auto-tuning workflow "
    "that compares several MiniZinc and/or CP-SAT candidate formulations "
    "before presenting one winner: a tiny smoke check that only rejects "
    "structurally broken candidates (never ranks), a separate representative "
    "tuning instance that races the survivors via submit_portfolio_job / "
    "run_cpsat_python_experiment to a provisional per-backend candidate, and "
    "a full-instance re-check plus final solve that alone supplies the "
    "presented result and any save-tool provenance. A peer of "
    "minizinc_solution_workflow and cpsat_python_solution_workflow: pick it when the "
    "user's own framing asks for formulations to be compared before "
    "committing to one, not as an automatic escalation from a single hard "
    "run inside either single-backend prompt."
)

# --- CP-SAT background job descriptions -------------------------------------

# Shared optional-checker paragraph for both CP-SAT job submit tools. The
# checker is DIAGNOSTIC only: it never gates saving (saving always replays
# through save_verified_cpsat_python) and never upgrades a solver status.
_CPSAT_JOB_CHECKER_NOTE = (
    "OPTIONAL DIAGNOSTIC CHECKER: pass `checker` (a Python checker source "
    "string, same protocol as save_verified_cpsat_python's checker gate), plus "
    "optional `problem` (forwarded to the checker payload) and "
    "`checker_timeout_ms` (defaults to `script_timeout_ms`; echoed on the job status "
    "as the effective value). After the solver child finishes with a non-empty "
    "solution and status `optimal`/`feasible`/`timeout`, the checker runs as a "
    "second bounded child while the job stays `running`; its "
    "CpsatCheckerReport (`accepted`/`rejected`/`error`/`timeout`) lands in the "
    "job status `checker` field. A supplied checker that did not run sets "
    "`checker_skipped_reason` instead. The report is DIAGNOSTIC ONLY: a "
    "checked `timeout` incumbent stays unsavable, and saving still re-runs "
    "verification through `save_verified_cpsat_python`. Bad checker arguments "
    "are rejected before a job is admitted. "
)

SUBMIT_CPSAT_PYTHON_JOB_DESCRIPTION = (
    "Submit an OR-Tools CP-SAT Python INLINE SOURCE as a BACKGROUND JOB and return "
    "immediately, so a long solve cannot hit a synchronous MCP client timeout. "
    "Takes the same `source` and `script_timeout_ms` as `run_cpsat_python`. "
    + _CPSAT_JSON_CONTRACT
    + " "
    + _CPSAT_JOB_CHECKER_NOTE
    + "Returns a CpsatPythonJobStatus with a server-generated opaque `job_id` and "
    "an initial `state` of `queued` or `running` (a script that finishes before "
    "the submit response is built can already report a terminal state); poll "
    "with `get_cpsat_python_job(job_id)` and "
    "stop with `cancel_cpsat_python_job(job_id)`. "
    "Admission is BOUNDED: at most a fixed number of CP-SAT jobs run at once, "
    "further submits sit `queued` up to a cap, and a submit beyond that is REJECTED "
    "with an MCP error (retry once a running job finishes). "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_cpsat_python_job")
    + _CPSAT_CHILD_POSTURE
)

SUBMIT_CPSAT_PYTHON_FILE_JOB_DESCRIPTION = (
    "Submit a LOCAL OR-Tools CP-SAT Python SCRIPT FILE as a BACKGROUND JOB and "
    "return immediately — the path-based counterpart to `submit_cpsat_python_job`. "
    "Pass `script_path` (an absolute local path) instead of pasting source; the "
    "script runs with its working directory set to the file's own directory so "
    "relative imports and data-file opens resolve. "
    "`script_path` is validated before admission: a missing path, a non-file, an "
    "empty/whitespace-only script, or non-UTF-8 content is rejected with an "
    "actionable MCP error and no job is created. "
    "Same output contract as `run_cpsat_python_file`; same admission bounds, "
    "polling (`get_cpsat_python_job`), and cancel (`cancel_cpsat_python_job`) as "
    "`submit_cpsat_python_job` — the `job_id` is kind-agnostic. "
    "Optional `args` (a list of strings) is appended after the script path, so "
    "the script reads it as `sys.argv[1:]` — pass it for a script that takes its "
    "data file or a flag on the command line, exactly as with "
    "`run_cpsat_python_file`. It is recorded at admission, so the job runs the "
    "values supplied on submit even while it waits in the queue. "
    + _CPSAT_ARGS_LIMITS
    + "That rejection happens at admission too, so no job record is created. "
    + _CPSAT_JOB_CHECKER_NOTE
    + "This tool ALSO accepts `checker_path` (a local path to an on-disk "
    "checker script) as an alternative to the inline `checker` string; the two "
    "are MUTUALLY EXCLUSIVE — pass at most one, supplying both is rejected at "
    "admission with no job created. A `checker_path` checker runs IN PLACE, "
    "with its working directory set to its own parent directory, so a checker "
    "that opens a relative sibling file finds it — which means `problem` can be "
    "a bare data filename next to the checker instead of a large instance "
    "inlined into every submit. That bare-filename `problem` form is specific "
    "to this path-based checker run: `save_verified_cpsat_python` has no "
    "`checker_path` and runs its inline checker from a temp copy, so saving "
    "this result later means inlining the instance again. It is validated at "
    "admission exactly like "
    "`script_path` and the resolved path is recorded, so the job runs the file "
    "named on submit; a checker file deleted before the checker phase runs "
    'surfaces as a `status="error"` checker report on the finished job, not a '
    "failed job. "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_cpsat_python_job")
    + _CPSAT_CHILD_POSTURE
)

GET_CPSAT_PYTHON_JOB_DESCRIPTION = (
    "Poll a background CP-SAT Python job by its `job_id` (from "
    "`submit_cpsat_python_job` or `submit_cpsat_python_file_job`). "
    "Returns a CpsatPythonJobStatus: `job_id`, `state`, `script_timeout_ms`, "
    "`submitted_at_ms`, `started_at_ms`, `finished_at_ms`, `elapsed_ms`, an "
    "optional `result` (the full CpsatPythonResult), an optional `message`, and "
    "— when the job was submitted with a checker — the diagnostic checker "
    "outcome: `checker` (a CpsatCheckerReport) or `checker_skipped_reason`, "
    "plus the effective `checker_timeout_ms` echo. `script_timeout_ms` caps the SOLVER "
    "child only; when the solver result is checker-eligible (non-empty "
    "solution, status `optimal`/`feasible`/`timeout`) the job stays `running` "
    "through the checker phase for up to an additional `checker_timeout_ms`; "
    "an ineligible result skips the checker and sets `checker_skipped_reason`. "
    "A checker report never changes `state` "
    "or savability (a checked `timeout` incumbent stays unsavable). "
    "`state` is one of `queued`, `running`, `succeeded`, `failed`, `timeout`, "
    "`cancelled`. "
    + _job_result_contract("a script-level `error` verdict (a crash or bad JSON)")
    + "A `timeout` job carries its partial CpsatPythonResult "
    "(`result.timed_out == True`, best-so-far `solution`/`objective`/"
    "`best_objective_bound`). While "
    "`running`, only `state` + `elapsed_ms` advance. "
    + _PACE_POLLING_NOTE
    + " On `succeeded` or `timeout`, present the result as "
    "`run_cpsat_python` requires: lead with the plain-language status and the "
    "solution in the user's terms. " + _UNKNOWN_JOB_ID_ERROR
)

CANCEL_CPSAT_PYTHON_JOB_DESCRIPTION = (
    "Request cancellation of a background CP-SAT Python job by `job_id`. A job "
    "still `queued` is dropped before it starts; a `running` job has its Python "
    "child process tree terminated. "
    + _cancellation_idempotent_note("`succeeded`/`failed`/`timeout`/`cancelled`")
    + "Returns the CpsatPythonJobStatus; the job reaches `cancelled` (with "
    "`result is None`) once the worker observes the request — poll "
    "`get_cpsat_python_job` to confirm the terminal state. " + _UNKNOWN_JOB_ID_ERROR
)

LIST_CPSAT_PYTHON_JOBS_DESCRIPTION = (
    "List the currently retained background CP-SAT Python jobs as "
    "CpsatPythonJobStatus entries (one per job), covering every state from "
    "`queued` to terminal. Works for both inline-source and file-based jobs. "
    + _REGISTRY_NOTE
    + " "
    + _NO_ARGS_LIST_TOOL
)


# --- Tabular (.xlsx/.csv) I/O -------------------------------------------------

# The cell contract is identical in both directions, so state it once.
_TABULAR_SCALAR_CONTRACT = (
    "Cells are JSON SCALARS ONLY — string, number, boolean, or null. Nested "
    "arrays/objects and non-finite numbers (NaN, Infinity) are rejected before "
    "any file is touched. The server does mechanical I/O only: it never infers "
    "what a column MEANS. Interpreting columns and building MiniZinc data or "
    "CP-SAT structures is YOUR job."
)

_TABULAR_LOCAL_ONLY = (
    "Runs locally: reads/writes only the named local file — no network, no LLM, "
    "no telemetry, no subprocess, and no managed-runtime dependency."
)

LOAD_TABULAR_DATA_DESCRIPTION = (
    "Read a page of rows from a LOCAL `.xlsx` or `.csv` file. Use this to pull "
    "problem data (capacities, costs, demands, shift requirements) out of a "
    "spreadsheet before modelling it. Returns a TabularData page: `headers`, "
    "`rows`, sheet metadata (`sheet_name`, `available_sheets`), and pagination "
    "fields (`row_offset`, `next_row_offset`, `total_rows`, `truncated`, "
    "`truncation_reason`). " + _TABULAR_SCALAR_CONTRACT + " "
    "`path` must be an EXPLICIT ABSOLUTE local `.xlsx`/`.csv` file: a relative "
    "path resolves against the server process's working directory, not the "
    "client's, so it is REFUSED rather than read from an unpredictable place. "
    "`sheet` selects an XLSX worksheet "
    "(default: the active one); the result reports `sheet_name` and every "
    "`available_sheets` name, and a CSV rejects `sheet` since it has none. "
    "`has_header=true` (default) treats row 1 as headers. "
    "HEADERS ARE ALWAYS STRINGS: a date/time header becomes ISO-8601, another "
    "non-string becomes its text form, and a BLANK header (empty or missing) "
    "becomes the positional name `col_1`, `col_2`, … — as do all columns when "
    "`has_header=false`. Duplicate header names are preserved as-is. "
    "TYPES: XLSX date/time cells are converted to ISO-8601 strings, while "
    "numeric and boolean cells keep their scalar types. A CSV is TEXTUAL — "
    'every cell reads back as a string, so `"3"` must be converted client-side '
    "before use as a number. CSV parsing uses one fixed dialect (comma-separated, "
    '`"`-quoted, UTF-8); semicolon and other locale dialects are not detected. '
    "PAGINATION: `row_offset` (default 0) is a zero-based offset among DATA rows "
    "— the header is not a data row — and `max_rows` (default 1000) caps the page. "
    "The structured page body (`headers`, `rows`, and pagination metadata) is "
    "additionally capped at 1 MiB; the page ends at `max_rows` or the byte cap, "
    "whichever binds first. The ceiling does "
    "not cover the tool call's separate human-readable text summary. Only WHOLE "
    "rows are ever returned, never a truncated row or cell. When "
    "`truncated` is true, `truncation_reason` is `max_rows` or `max_bytes` and "
    "`next_row_offset` is the offset to request next — pass it back to page "
    "forward; at EOF both are null. `total_rows` always counts every data row in "
    "the file. A single row (or the headers alone) too large for the 1 MiB ceiling "
    "is an error naming the offending offset, not a silent truncation. "
    "A formula cell reads as its CACHED result — the server never evaluates a "
    "formula, so an uncalculated one reads as null — and a merged cell exposes its "
    "value only in the top-left position. " + _TABULAR_LOCAL_ONLY
)

WRITE_TABULAR_RESULT_DESCRIPTION = (
    "Write `headers` + `rows` to a LOCAL `.xlsx` or `.csv` file. Use this to hand "
    "a solved schedule, assignment, or plan back to the user as a spreadsheet. "
    + _TABULAR_SCALAR_CONTRACT
    + " Every row must have exactly one cell per header. "
    "`target_path` must be an EXPLICIT ABSOLUTE local path whose parent directory "
    "exists and whose suffix is `.xlsx` or `.csv`; the server never opens a file "
    "dialog — you supply the path. The write is ATOMIC and, by default, CANNOT "
    "CLOBBER: with `overwrite=false` an existing target (even one created while "
    "the write was in flight) wins and is left byte-for-byte untouched, and the "
    "call is an error; pass `overwrite=true` to atomically replace exactly that "
    "one file. A rejected write leaves the filesystem untouched. "
    "FORMULA SAFETY: the server never emits executable spreadsheet code. XLSX "
    "stores every string it writes, on every sheet, as an explicit string cell, "
    'so `"=1+1"` is written and read back as the literal TEXT `=1+1`. '
    "A CSV field cannot say 'this is literal "
    "text', so a CSV write REJECTS any string whose first non-whitespace character "
    "is `=`, `+`, `-`, or `@`. Note this also rejects a NUMBER SENT AS A STRING: "
    'send `-5` as the numeric cell `-5`, not the string `"-5"`, or write `.xlsx` '
    "instead. CSV is textual on write too — a string is emitted unchanged, null "
    "becomes an empty field, and other scalars take their normal text form, so a "
    "type-preserving CSV round trip is NOT promised (use `.xlsx` when types "
    "matter). An XLSX cell string is capped at 32,767 characters; a longer one is "
    "rejected rather than silently truncated. The XLSX data sheet is always "
    "`Sheet1` and stays active; only `gantt`/`charts` add sheets beside it. "
    "XLSX ROUND-TRIP SAFETY: further writes are rejected rather than silently "
    'corrupted or changed on the next read — an empty-string row cell (`""`, '
    "send `null` instead); a number needing more than 16 significant digits, or "
    "whose int/float type would flip on read-back (send it as a string instead); "
    "a string with a character XML cannot represent, e.g. a lone surrogate or "
    "`U+FFFE`/`U+FFFF`; a zero-column table (`headers=[]`); and a string "
    "containing a carriage return (`\\r`, alone or as `\\r\\n`) — XML normalizes "
    "it to `\\n` on read, so use `\\n`, or write `.csv`, which preserves it. "
    "PRESENTATION (XLSX only, all OPTIONAL — omit them for exactly the plain "
    "table above; a `.csv` target plus any of them is REJECTED, not ignored): "
    "`style` formats the data sheet (bold frozen header row, auto filter, banded "
    "rows, fitted widths); `gantt` adds a cell-grid timeline sheet from "
    "`task_column`, `start_column`, and EXACTLY ONE of `end_column`/"
    "`duration_column`, coloured by `color_column` and grouped onto ONE ROW per "
    "`row_column` value — the resource view: pass the machine/vehicle/crew column "
    "to see contention and idle time, omit it for one row per task. Tasks whose "
    "spans overlap on a row spill onto extra rows rather than overwrite one "
    "another, and a grouped bar carries its task label inside it. Each time "
    "column is ONE unit labelled by its LEFT EDGE, followed by a final tick at "
    "the horizon, so a task ending at 11 stops at the tick marked 11. `charts` "
    "plots the data "
    "sheet's own columns. Columns are named by HEADER STRING, so a duplicated "
    "header is rejected as ambiguous; Gantt times must be "
    "discrete integers >= 0, never coerced from a float or string, with the grid "
    "capped at 512 time units; charted `y_columns` (and a `scatter` `x_column`) must "
    "be numeric; no `sheet_name` may collide case-insensitively with `Sheet1`, "
    "the Gantt's, or another chart's, but two charts matching EXACTLY share one "
    "sheet. Styling never changes a stored value — "
    "and a DATE `number_format`, which would make its column read back as "
    "ISO-8601 TEXT, is rejected rather than applied. "
    "Returns a TabularWriteResult, whose fields the output schema names: "
    "`status` is always 'written' (every refusal is an MCP error instead), "
    "`sha256` is of the committed bytes, `sheets_written` lists the sheets "
    "written, data sheet first (empty for a CSV, which has none), and "
    "`diagrams_written` (one token per diagram, in render order: `gantt`, "
    "`chart:bar`, `chart:line`, `chart:scatter`; styling adds none). "
    + _TABULAR_LOCAL_ONLY
)
