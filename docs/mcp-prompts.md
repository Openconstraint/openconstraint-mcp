# MCP prompts

The stdio server exposes four MCP prompts for client-side LLMs. One,
`solve_constraint_problem`, is available in **both** profiles; the other three
are **full**-profile only — start the server with
`openconstraint-mcp stdio --toolset full` to expose them (see [CLI](https://github.com/Openconstraint/openconstraint-mcp/blob/master/README.md#cli)).

> **Two different entry paths.** MCP *tools* are model-controlled: the host and
> its model decide which tool to retrieve and call, which is why the four solve
> tools lead their descriptions with the plain-language problem vocabulary
> (scheduling, rostering, assignment, routing, packing/bin-packing, knapsack,
> resource allocation) and say what input they need. MCP *prompts* are
> user-controlled: a workflow prompt does nothing until you pick it explicitly
> from your client's prompt menu, slash-command list, or command palette, and a
> client that does not surface MCP prompts will never show it. Neither path is a
> routing guarantee — the metadata is the guarantee, and tool selection stays
> host- and model-controlled.

- **`solve_constraint_problem(problem: str)`** — available in **both** profiles,
  including the default `core`. One compact, backend-neutral workflow for
  ordinary solving: analyze the problem's variables, constraints, and objective
  (asking only about material missing information), choose MiniZinc or OR-Tools
  CP-SAT Python by problem shape, draft a complete model or script, verify and
  run it with the core tools — `check_minizinc_model` then
  `solve_minizinc_model`, or `run_cpsat_python`, switching to
  `check_minizinc_files` / `solve_minizinc_files` / `run_cpsat_python_file` when
  the artifact already exists on disk — and present the status and solution in
  the user's own terms. It states the mandatory generation rule for every
  artifact it drafts, MiniZinc model and CP-SAT script alike: generate only
  modeling code — no network access, no file writes or deletes, and no
  subprocess spawning — unless the user explicitly asked for it. The three
  full-only prompts below are the detailed, backend-specific alternatives;
  reach for them when you need the advanced full-profile capabilities they
  cover.

- **`minizinc_solution_workflow(problem: str)`** — **full** profile only. A
  guided template for the MCP client's LLM. Given a natural-language
  constraint or optimization
  problem, the prompt instructs the client's model to:

  1. Identify decision variables, domains, constraints, and any objective.
  2. Ask the user a few concise clarifying questions if the problem is
     underspecified, rather than silently inventing values.
  3. Draft a complete MiniZinc model — including declarations,
     constraints, exactly one `solve` statement, and an `output` block —
     preferring the `cp-sat` solver by default.
  4. Validate the drafted model with `check_minizinc_model` before
     solving, when that tool is available: solve only after the check
     returns `"ok"`; on `"error"`, repair the model from `stderr` and
     re-check; on `"timeout"`, ask the user how to proceed (simplify the
     model, raise `timeout_ms`, or solve anyway) rather than auto-solving.
  5. Call the `solve_minizinc_model` tool if it is available, or
     otherwise walk the user through the openconstraint-mcp CLI —
     `check-runtime` to locate the managed `minizinc` binary (with
     `install-runtime` or `configure-runtime` first if it is missing) —
     and have them invoke that exact managed binary on the drafted
     model. The prompt explicitly forbids recommending a bare
     PATH-based `minizinc` invocation.
  6. Revise the model if MiniZinc reports an error, and present the final
     result to the user as a short, structured summary that leads with the
     result: a plain-language `status`, the solution quoted verbatim from
     `stdout` (only when the status carries one), a compact table rather than
     a prose-only list when the data is item-like (one row per item for small
     item sets, with relevant attributes and the selected/count value), and
     the complete model-visible `Statistics:` section whenever the
     `statistics` map is non-empty. Do not condense that section to selected
     fields such as `solveTime` and `objectiveBound`. Each section heading
     appears at most once, and the explanation stays focused on verifying the
     result rather than adding speculative algorithm commentary by default.
  7. Optionally — only when the user asks to save the result — persist it
     with `save_verified_minizinc_model`, passing the final model/data/checker
     text and the user's explicit absolute target directory. The client asks
     the user for that path (or uses its own file picker); the server opens no
     file dialog and re-verifies the artifacts before writing anything.

  When the user already has the model on disk as `.mzn`/`.dzn` files, the
  prompt skips drafting and routes the same validate → solve → present loop
  through the path-based `check_minizinc_files` and `solve_minizinc_files`
  tools (passing `model_path`/`data_path`), which return the same
  `CheckResult`/`SolveResult` shapes.

  The openconstraint-mcp server itself does **not** call an LLM and does
  not embed any agent framework. The prompt only structures how the
  *client's* LLM should propose a MiniZinc model; the model is then
  verified by the local managed MiniZinc runtime via
  `solve_minizinc_model`. `LLM proposes, local MiniZinc verifies.`

- **`cpsat_python_solution_workflow(problem: str)`** — **full** profile only. A
  guided template for the MCP client's LLM to write OR-Tools CP-SAT Python and
  run it via `run_cpsat_python`. The prompt instructs the client's model to:

  1. Identify decision variables, domains, constraints, and the objective.
  2. Ask concise clarifying questions if the problem is underspecified.
  3. Write a complete, runnable OR-Tools CP-SAT Python script that emits
     the required JSON object (`{"status", "objective", "solution",
     "best_objective_bound"}`) as its last stdout line, using `status_map` to
     translate `cp_model.OPTIMAL` etc. to vocabulary strings. For
     reproducible saved artifacts, set a fixed `solver.parameters.random_seed`
     and prefer a single search worker. **Safety instruction:** generate only
     CP-SAT modeling code — no network access, no file writes or deletes, no
     subprocess spawning — unless the user explicitly asked. The server
     executes this code locally and does not sandbox it.
  4. Call `run_cpsat_python` with the script as `source`.
  5. Present the `CpsatPythonResult`: distinguish `optimal` (proven best)
     from `feasible` (valid but not proven optimal); point at `stderr` on
     `error`; explain `timeout` clearly; for `unknown`, mention
     `best_objective_bound` when present as a diagnostic hint (not a solution).
  6. For MULTIPLE explicit attempts (comparing source variants, or the same
     source under different cooperative configs), call
     `run_cpsat_python_experiment` instead of calling `run_cpsat_python`
     repeatedly — the client always supplies every attempt's complete script,
     as inline `source` or as a `script_path` (+ `args`) to one already on
     disk, exactly one of the two per attempt; the server only executes,
     verifies, and selects a winner. Coordinate
     `max_parallel_attempts` with each script's own
     `solver.parameters.num_workers` to avoid oversubscribing the machine.
  7. Optionally — only when the user asks — call `save_verified_cpsat_python`
     with the script and an explicit absolute `target_dir`. The client asks
     the user for that path; the server opens no file dialog and re-runs the
     script to evaluate the save gate before writing anything. Three gate
     options in order of strictness: (a) **reported gate** (always applied):
     status `optimal`/`feasible` and non-empty solution; (b) **expectation
     gate** (optional): `objective_sense` + `objective_threshold` — a quality
     check, **not a proof of global optimality**; (c) **checker gate**
     (optional): a Python checker script that reads payload JSON from
     `sys.argv[1]` and returns `{"status": "accepted"|"rejected"|"error",
     "errors": [...], "details": {}}` — `accepted` + empty errors is the only
     passing verdict. The checker is not sandboxed. If the saved script came
     from `run_cpsat_python_experiment` — the winner, or any other accepted
     inline-`source` attempt you chose to save instead (never a `script_path`
     one, which the save cannot replay) — also pass its `config` and
     `experiment_result` so
     the full attempt table is persisted as `experiment-log.json` — a
     provenance summary, not an archive.

  The server makes no LLM call. The prompt structures how the *client's*
  LLM should write the script; the script is then executed locally by
  `run_cpsat_python`. `LLM writes, server executes locally.`

- **`auto_tune_constraint_problem(problem: str)`** — **full** profile only.
  Client-side orchestration for comparing *several* candidate formulations
  (MiniZinc and/or CP-SAT Python) before presenting one winner, rather than
  solving a single drafted model. A peer of `minizinc_solution_workflow` and
  `cpsat_python_solution_workflow` — pick it when the user's own framing asks for
  formulations to be compared ("try a few approaches", "which formulation is
  fastest", "compare MiniZinc vs CP-SAT"), not as an automatic escalation
  from a single hard-instance result. The prompt instructs the client's
  model through a fixed THREE-tier workflow:

  1. Identify decision variables, domains, constraints, and the objective;
     ask clarifying questions only when required data is missing. Check for
     an existing on-disk model (a MiniZinc `.mzn`/`.dzn` pair or a CP-SAT
     `model.py`) and, if found, review it and include it as one candidate
     rather than ignoring it or treating it as the only candidate.
  2. Draft a small set of MiniZinc and/or CP-SAT candidates, plus the
     existing on-disk candidate when present. Every MiniZinc candidate fixes
     one shared `.dzn` parameter interface up front — only the data *values*
     scale up across stages, since `submit_portfolio_job` races multiple
     `models` against exactly one shared `data`. Each CP-SAT candidate is
     drafted with the smoke instance's tiny values hardcoded first and gets
     REWRITTEN, not reused verbatim, at each later stage.
  3. **Tiny smoke check** (`inspect_minizinc_model` + `check_minizinc_model`
     per MiniZinc candidate, one short `run_cpsat_python` per CP-SAT
     candidate) rejects only structurally broken candidates — it never ranks
     or selects a winner, since a toy instance does not reliably predict
     full-scale performance.
  4. **Representative tuning race**, on a separate, larger instance sized to
     actually exercise the problem's structure: select a PROVISIONAL
     MiniZinc candidate with one `submit_portfolio_job` call *per*
     smoke-surviving candidate (never multiple formulations raced inside one
     call, since its `first-decisive-result` winner treats
     `unsatisfiable`/`unbounded` as decisive and its checker verdict is only
     observational), ranked by best `objective` then elapsed time for an
     optimization problem, or by `status` then elapsed time for a pure
     feasibility problem (no `objective` to compare); and a PROVISIONAL
     CP-SAT candidate with a single `run_cpsat_python_experiment` call across
     the smoke-surviving CP-SAT candidates (safe to race together, since that
     tool's own acceptance gate already excludes an incorrect formulation). A
     checker is required whenever more than one candidate is compared, and
     two backend-specific checkers are required for cross-backend
     comparison. Neither the smoke nor the tuning-stage result is ever
     presented as the answer or used as save-tool provenance.
  5. **Full-instance re-check**: a bounded `solve_minizinc_model`/
     `solve_minizinc_files` call (never `check_minizinc_model`/
     `check_minizinc_files`, which only compile) or a full-instance CP-SAT
     rewrite run as a CHECKED `submit_cpsat_python_job` (never the plain
     `run_cpsat_python`, which has no `checker` parameter). Stop and report
     the failure on MiniZinc's `unsatisfiable`/`error`, CP-SAT's
     `infeasible`/`error`, or — once a solution exists to check — any
     checker outcome short of a clean pass (MiniZinc `"completed"`, CP-SAT
     `"accepted"`; a genuine checker `error`/`timeout` on a real solution
     also stops). A `timeout`/`unknown` result with NO incumbent is the one
     inconclusive case: the checker naturally has nothing to check then
     (MiniZinc `"no_solution"`, or a skipped CP-SAT checker) — that specific
     combination proceeds to the final solve while flagging that the
     pre-check did not confirm feasibility, rather than being treated as a
     checker failure.
  6. **Final solve** on the full instance: `submit_portfolio_job` (for
     `portfolio_result` provenance) or `submit_solve_job` for MiniZinc; the
     synchronous `run_cpsat_python_experiment` (for `experiment_result`
     provenance, which requires the saved attempt to use inline `source`, not
     `script_path`) or `submit_cpsat_python_job` for CP-SAT. Poll the matching
     `get_*_job` tool for whichever background tool was used. This
     full-instance terminal result — never the smoke, tuning-stage, or
     re-check result — is what gets presented to the user, but only after
     checking its checker verdict: the finalist tools' checker fields are
     all observational (a checker-violated result is never auto-refused), so
     once a solution exists, anything short of a clean
     `"completed"`/`"accepted"` pass there still means stop and report it
     rather than presenting the result as the answer — except a terminal
     `timeout`/`unknown` with no incumbent, where the checker naturally has
     nothing to check and that result is still presented, flagged as
     unproven.
  7. Optionally — only when the user asks, with an explicit absolute
     `target_dir` — save the full-instance winner with
     `save_verified_minizinc_model`/`save_verified_cpsat_python`, passing
     `portfolio_result`/`experiment_result` only when the final solve
     actually used `submit_portfolio_job`/the synchronous
     `run_cpsat_python_experiment`.

  The server calls no LLM and embeds no agent framework anywhere in this
  workflow; every check, race, and solve still runs through the same
  deterministic local tools as the two single-backend prompts.
  `LLM drafts and compares, openconstraint-mcp checks/executes/verifies each
  candidate.`
