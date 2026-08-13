# openconstraint-mcp

[![CI](https://github.com/Openconstraint/openconstraint-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Openconstraint/openconstraint-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/openconstraint-mcp)](https://pypi.org/project/openconstraint-mcp/)

A local-first [Model Context Protocol](https://modelcontextprotocol.io) server for
constraint programming and optimization. `openconstraint-mcp` gives an MCP client a
deterministic way to compile-check and solve [MiniZinc](https://www.minizinc.org/)
models on a **managed** solver runtime, exposing open-source solvers (OR-Tools CP-SAT
by default, Chuffed as an optional verifier) over MCP stdio.

Constraint problems — scheduling, rostering, assignment, routing, production
planning, inventory — are exactly where a language model is most likely to produce an
answer that looks right but is subtly infeasible. The division of labor here is
**LLM proposes, server verifies**: the client's LLM drafts a MiniZinc model, and the
local runtime compiles and solves it to produce a checked result. The server runs the
solver; it never drafts a model of its own and never calls an LLM.

Everything runs on your machine. No telemetry, no background network calls, and
nothing leaves your machine unless you opt in — the only network access in the entire
package is the runtime download you trigger explicitly with `install-runtime`.

## Design principles

- **Local-first.** Solving, validation, and result inspection all run on your machine.
  There are no remote solving backends and no upload of your models or data.
- **Managed runtime.** Solver execution always goes through a MiniZinc runtime this
  project resolves and controls, never an arbitrary `$PATH` binary — so a run does not
  depend on whatever MiniZinc happens to be installed on the host.
- **LLM proposes, server verifies.** Natural-language → model translation, critique,
  and repair belong in the MCP *client's* LLM. The server owns the deterministic half:
  compile-check, solve, and report the runtime's verbatim output. It holds no LLM
  credentials and never invokes a generative model.
- **No hidden network calls.** Validation, solving, and result inspection are all
  offline. The only sanctioned network call is the runtime download, and only when you
  run `install-runtime` — never on import, on server boot, or as a "convenience".
- **No telemetry.** Not implemented. Any future telemetry would be opt-in and
  documented.

## Stage 2 readiness

The intended LLM-verification loop —
`inspect/check -> solve or submit job -> check/verify -> repair if needed ->
save -> rerun from saved files` — is complete for both backends:

- **Background jobs.** MiniZinc: `submit_solve_job`/`get_solve_job` and
  `submit_portfolio_job`/`get_portfolio_job`. CP-SAT: `submit_cpsat_python_job`/
  `submit_cpsat_python_file_job` with `get_cpsat_python_job`. See
  [Background solve jobs](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/mcp-tools.md#background-solve-jobs),
  [Background portfolio jobs](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/mcp-tools.md#background-portfolio-jobs), and
  [Background CP-SAT jobs](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/cpsat-python.md#background-cp-sat-jobs).
- **Structured diagnostics.** A stable `diagnostic.category` enum on every
  solve/check/inspect/unsat-core/save/job/portfolio/checker/experiment result —
  see [Structured diagnostics](#structured-diagnostics).
- **Checker-backed workflows.** `solve_minizinc_model`/`solve_minizinc_files`
  accept an inline/path checker; `save_verified_cpsat_python` and the CP-SAT
  job tools accept a Python checker gate. The [Example
  inventory](#example-inventory) below links a real checker-rejects-a-wrong-answer
  demonstration.
- **Infeasibility repair.** `find_unsat_core`/`find_unsat_core_files` diagnose
  an unsatisfiable MiniZinc model; see [Diagnosing and repairing
  infeasibility](#diagnosing-and-repairing-infeasibility) for an end-to-end,
  test-backed walkthrough, including the honest no-core/inconclusive case.
- **Inspection.** `inspect_minizinc_model`/`inspect_minizinc_files` report a
  model's required parameters and output variables before spending a solve.
- **Reproducible artifacts.** `save_verified_minizinc_model` and
  `save_verified_cpsat_python` re-verify before writing, record a durable
  experiment log when portfolio/experiment provenance is attached, and are
  rerunnable via `solve_minizinc_files` / `run_cpsat_python_file` — see
  [Reproducing a saved CP-SAT artifact](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/cpsat-python.md#reproducing-a-saved-cp-sat-artifact)
  for the CP-SAT replay caveat (`run_cpsat_python_file` re-verifies at the
  `reported` level only; `run_cpsat_python_file_checked` re-runs the saved
  checker too, and full gate replay — including the objective `expectation` —
  re-runs `save_verified_cpsat_python`).
- **Examples.** The [Example inventory](#example-inventory) maps every
  retained example to the workflow(s) it demonstrates, its test coverage, and
  any known gap, rather than leaving coverage implicit.

## Installation

Requires Python 3.12+. This project is `uv`-managed end-to-end; install [`uv`](https://docs.astral.sh/uv/)
first if you don't already have it.

All three paths below give you the same `openconstraint-mcp` command and the same
tools. They differ only in where the command lives and whether it is tied to a
checkout.

### Install it (recommended)

```bash
uv tool install openconstraint-mcp
```

`openconstraint-mcp` is now on your `PATH` and works from any directory. Upgrade
later with `uv tool upgrade openconstraint-mcp`.

### Run it without installing

```bash
uvx openconstraint-mcp --help
```

`uvx` fetches the package into a disposable cached environment and runs it,
putting nothing on your `PATH` and re-resolving to the latest published version
on each run.

### Develop on it

```bash
git clone https://github.com/Openconstraint/openconstraint-mcp.git
cd openconstraint-mcp
uv sync --all-groups
```

The command is then `uv run openconstraint-mcp …` (or `just cli …`, which wraps
the same thing), and it works only from inside the checkout.

## Quick start (MCP users)

These commands assume `uv tool install` put `openconstraint-mcp` on your `PATH`;
prefix them with `uvx` or `uv run` if you chose one of the other installation
paths above.

1. **Set up MiniZinc** — optional, and one of:
   - `openconstraint-mcp install-runtime` to fetch and install the managed bundle (Linux x86_64, macOS arm64, Windows x86_64). Roughly 200 MB, once per machine.
   - `openconstraint-mcp configure-runtime --runtime-dir <path>` to point the package at an existing MiniZinc install (a directory containing `bin/minizinc`).

   This step is optional because every installation path already includes the
   OR-Tools CP-SAT Python library, so the whole CP-SAT tool family works
   immediately; only the MiniZinc tools need the runtime. Note that the MiniZinc
   bundle ships its *own* native CP-SAT backend (`fzn-cp-sat`, used when a
   MiniZinc model selects the `cp-sat` solver) — it is a separate copy from the
   Python `ortools` package, and neither install implies the other.

   The runtime is stored per user, outside any virtualenv (see
   [Managed runtime](#managed-runtime)), so you install it once and every
   installation path above finds it — including a `uvx` environment created
   after the fact.
2. **Verify:** `openconstraint-mcp check-runtime` and `openconstraint-mcp list-solvers`.
3. **Wire into your MCP client.** MCP standardizes the wire protocol, not how a
   client is told to launch a server, so each client has its own file name and
   schema. The executable and arguments are identical in all of them.

   **Claude Code** — `.mcp.json` in your project:

   ```json
   {
     "mcpServers": {
       "openconstraint": {
         "type": "stdio",
         "command": "openconstraint-mcp",
         "args": ["stdio", "--toolset", "full"]
       }
     }
   }
   ```

   **opencode** — `opencode.json` in your project, or `~/.config/opencode/opencode.json`:

   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "mcp": {
       "openconstraint": {
         "type": "local",
         "command": ["openconstraint-mcp", "stdio", "--toolset", "full"],
         "enabled": true
       }
     }
   }
   ```

   **Codex** — `.codex/config.toml` in your project, or `~/.codex/config.toml`:

   ```toml
   [mcp_servers.openconstraint]
   command = "openconstraint-mcp"
   args = ["stdio", "--toolset", "full"]
   tool_timeout_sec = 900
   ```

   All three assume `uv tool install` put `openconstraint-mcp` on your `PATH`. To
   use the no-install path instead, make the command `uvx` and prepend
   `openconstraint-mcp` to the arguments — for example
   `"command": "uvx", "args": ["openconstraint-mcp", "stdio", "--toolset", "full"]`.

   Raise your client's per-tool timeout, as the Codex block does. A checked
   CP-SAT call runs two capped child processes, so its worst case is
   `(timeout_ms + 8000) + (checker_timeout_ms + 8000)` milliseconds. If your
   client gives up sooner than that, you get a client-side timeout instead of a
   result while the server is still solving. For solves longer than any
   synchronous timeout allows, use the background job tools instead.

   Restart your MCP client; the `check_runtime` and `list_available_solvers`
   tools should appear.

   > This repository's own `.mcp.json`, `opencode.json`, and `.codex/config.toml`
   > are **development** configs, not templates. They launch `uv run …` against
   > the checkout's virtualenv and, for Codex, pin `cwd` to an absolute path, so
   > they only work inside this clone. Use the blocks above, which need no
   > checkout.

Once connected, the server's MCP `instructions` tell the client to route constraint
and optimization tasks here before running solver code directly — no client-side
setup (prompt files, `AGENTS.md`/`CLAUDE.md` entries) needed beyond the connection
above. Tool selection stays host/model-controlled, so this improves routing but
doesn't guarantee the client calls a tool.

## CLI

The package exposes five commands:

- **`openconstraint-mcp stdio`** — run the MCP server over stdio. This is the entry
  point an MCP client (e.g. Claude Desktop, Claude Code) launches.

  Flags:

  - `--toolset core|full` — which MCP toolset to advertise (default: `core`).
    The **core** profile exposes eight essential tools for a materially
    smaller `tools/list` payload and a less ambiguous default choice
    set: `check_runtime`, `list_available_solvers`, `check_minizinc_model`,
    `solve_minizinc_model`, `check_minizinc_files`, `solve_minizinc_files`,
    `run_cpsat_python`, and `run_cpsat_python_file` — plus one MCP prompt,
    `solve_constraint_problem`, the backend-neutral workflow you invoke by
    hand in a client that exposes MCP prompts. The **full** profile
    (`--toolset full`) additionally exposes the three detailed MCP prompts and
    every advanced tool — background solve/CP-SAT jobs, solver portfolios,
    explicit CP-SAT experiments, checker-verified CP-SAT file runs
    (`run_cpsat_python_file_checked`), verified saving, model-interface inspection,
    unsat-core diagnostics, and tabular (Excel/CSV) I/O. Use `--toolset full`
    when you need any of those; existing users who relied on an advanced tool
    from bare `stdio` must now pass `--toolset full`.

    > This is an advertised-contract change only: the smaller payload is a
    > server-level guarantee. Whether a smaller default set reduces the tokens
    > your model sees is host-dependent — the MCP host decides how it caches,
    > filters, or forwards discovered tool metadata.
- **`openconstraint-mcp install-runtime`** — fetch and install the managed
  MiniZinc bundle (Linux x86_64, macOS arm64, and Windows x86_64 in v0). Streams
  the pinned upstream asset from the MiniZinc GitHub release (a `.tgz` on Linux, a
  `.dmg` on macOS, the NSIS `setup-win64.exe` on Windows — run silently),
  verifies its SHA256, installs it into the chosen target, smoke-checks the
  resulting `bin/minizinc` (`bin\minizinc.exe` on Windows), and remembers the
  install location so `check-runtime` and `list-solvers` find it without further
  configuration. This is the **only** command in the package that touches the
  network.

  Flags:

  - `--runtime-dir <path>` — explicit install location. Overrides
    `OPENCONSTRAINT_MCP_RUNTIME_DIR`, the persisted install config, and the
    platformdirs default, and suppresses the interactive path prompt. Recommended
    when you want to be certain where the install lands.
  - `--yes` / `-y` — non-interactive: skip the path prompt **and** skip the
    overwrite-confirmation prompt **only for a prior managed install**. `--yes`
    is required for non-TTY (CI / scripted) runs.

    `--yes` does **not** force overwrite of an unmanaged non-empty directory.
    Pointing `--runtime-dir` at `$HOME`, `/tmp`, a project checkout, or any
    directory the installer did not previously write to is refused regardless of
    `--yes`. The marker file `.openconstraint-runtime.json` written into the
    runtime root is what makes a directory eligible for overwrite — `--yes`
    only authorises replacing the installer's own prior output.

  When stdin is a TTY and neither `--runtime-dir` nor `--yes` is given, the
  command prompts for the install location (Enter accepts the default).
- **`openconstraint-mcp configure-runtime --runtime-dir <path>`** — point the
  package at an existing MiniZinc install (e.g. a system install, package-manager
  install, or one you built yourself) without setting
  `OPENCONSTRAINT_MCP_RUNTIME_DIR`. Validates that `<path>/bin/minizinc` exists
  and is executable, then persists the path to the install config. Does not
  download anything and does not claim ownership of the directory — use this
  when you already have MiniZinc on disk and just want `openconstraint-mcp` to
  find it.
- **`openconstraint-mcp check-runtime`** — report whether the managed MiniZinc
  runtime is installed. Prints the expected runtime path and exits 0 when present,
  exits 1 otherwise.
- **`openconstraint-mcp list-solvers`** — list solvers exposed by the managed
  MiniZinc runtime. Requires the runtime to be installed; exits 1 with a clear
  error otherwise.

## Structured diagnostics

Every solve, check, inspect, unsat-core, save, job, portfolio, checker, and
experiment result carries an optional `diagnostic` field so a client can branch
on a **stable category** before scraping raw `stdout`/`stderr`/transcripts:

- `diagnostic: null` is the clean-success signal — a diagnostic is present only
  when there is something actionable or noteworthy.
- `diagnostic.category` is a stable enum (below); `diagnostic.message` is a
  concise human summary; `diagnostic.details` is an optional compact dict of
  machine-readable facts (`return_code`, `timed_out`, `truncated`, `solver`,
  `checker_status`, …). Raw streams remain available and unchanged.

Existing `status`/`state` fields are unchanged and remain the primary
success/failure outcome; `diagnostic` is additive. Pre-result MCP errors (raised
before any result model exists) expose the same contract through a documented
first line, `Diagnostic: <category> — <message>`, in the error text.

| category | what happened | typical client action |
| --- | --- | --- |
| `syntax_or_compile_error` | the model did not compile | fix the model syntax and re-check |
| `missing_data` | a required parameter/data value is missing | supply the missing data (`.dzn` or inline) |
| `type_error` | a type/type-inst error | fix the offending declaration/expression |
| `solver_unavailable` | the requested solver id is unknown/unusable | pick an available solver (`list_available_solvers`) |
| `infeasible` | the model is unsatisfiable | relax constraints; try `find_unsat_core` |
| `unbounded` | the objective is unbounded | add a bound to the objective |
| `infeasible_or_unbounded` | unsat or unbounded, solver can't tell | add bounds and re-solve to disambiguate |
| `timeout_no_incumbent` | hit the time limit, no solution found | raise the run's timeout or simplify the model |
| `timeout_with_incumbent` | hit the time limit, best-so-far returned | accept the incumbent or raise the run's timeout for a proof |
| `cancelled` | a job was cancelled | resubmit if still needed |
| `job_failed` | a background job failed with no result | read `message`; fix inputs and resubmit |
| `child_process_error` | the CP-SAT child failed or broke its output contract | fix the script; check `stderr`/`return_code` |
| `output_truncated` | the child's output exceeded the 1 MiB cap (CP-SAT or MiniZinc) and was truncated | reduce printed output, or page a MiniZinc enumeration with `num_solutions` |
| `invalid_save_target` | the save `target_dir` is invalid/occupied | pick an absolute, empty/owned dir; pass `overwrite=true` |
| `not_verified` | a save/verification gate rejected the result | address the gate (objective/checker) and retry |
| `checker_failed` | the solution checker rejected/errored/timed out | inspect `checker`; fix the solution or checker |
| `runtime_missing` | the managed MiniZinc runtime is not installed | run `openconstraint-mcp install-runtime` |
| `unsupported_feature` | a requested control/feature is unsupported | drop it or choose a supporting solver |
| `invalid_request` | malformed/invalid input rejected pre-result | fix the argument/path; retry |
| `no_winner` | a portfolio/experiment accepted no attempt | broaden attempts or relax the gate |
| `unknown` | no safe classification | read the raw `status`/`stderr` |

The server never performs LLM repair and does not sandbox CP-SAT children; a
diagnostic describes only what the local wrapper observed.

## MCP tools

The full-profile tool catalog — every MiniZinc and CP-SAT tool the server can
expose, each in an inline-source and a path-based form, plus background solve
jobs, solver portfolios, registry bounds, and progress notifications. See
[docs/mcp-tools.md](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/mcp-tools.md).

## CP-SAT Python execution path

The second solving path: the client's LLM writes complete OR-Tools CP-SAT
Python and the server runs it in a bounded local child process. Covers the
script contract, the tools, explicit experiments, saved artifacts, background
jobs, and the security posture. See
[docs/cpsat-python.md](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/cpsat-python.md).

## Tabular data I/O (Excel/CSV)

Two backend-agnostic tools move scalars between local `.xlsx`/`.csv` files and
MCP, feeding either solving path. Covers the cell contract, pagination, formula
safety, XLSX round-trip hazards, and the overwrite contract. See
[docs/tabular-data.md](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/tabular-data.md).

## MCP prompts

The four MCP prompts for client-side LLMs — `solve_constraint_problem` in both
profiles, three more under `--toolset full` — and how a user-controlled prompt
differs from a model-controlled tool. See
[docs/mcp-prompts.md](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/mcp-prompts.md).

## Example models

> **Note:** `examples/` is no longer tracked in this repository (see git
> history for the last tracked snapshot). The paths below describe the
> shape of the example models; real industrial examples will replace
> them here.

The `examples/` directory holds small, self-contained MiniZinc models you can
point the path-based file tools at (or run by hand through the managed
runtime). Each is a `model.mzn` — usually with a matching `data.dzn`, and one
also ships a `model.mzc.mzn` solution checker:

- **`examples/knapsack`** — bounded knapsack: choose how many of each item type
  to pack to maximize total value without exceeding the weight `capacity`
  (`solve maximize`).
- **`examples/balanced_assignment`** — assign jobs to workers to minimize the
  most-loaded worker's total duration, i.e. balance the load (`solve minimize`).
- **`examples/social_golfers`** — the Social Golfer Problem: schedule `n_groups`
  groups of `group_size` golfers over `n_weeks` weeks so no pair ever shares a
  group twice (`solve satisfy`). The shipped data is Kirkman's fifteen
  schoolgirls — 15 golfers in 5 groups of 3 over 7 weeks, which uses every one
  of `C(15,2) = 105` pairs exactly once and is the most weeks a 5-3 schedule can
  reach. See [Diagnosing and repairing infeasibility](#diagnosing-and-repairing-infeasibility)
  below for what happens past that maximum. The CP-SAT examples under `cpsat/`
  (a `feasible`, not independently checked, incumbent) and `cpsat_best/` (a
  `checked`-level saved artifact with a checker and a replay config — see
  [Reproducing a saved CP-SAT artifact](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/cpsat-python.md#reproducing-a-saved-cp-sat-artifact))
  are specialized Python constructions for the tougher 7-3-10 boundary instance.
- **`examples/golomb_ruler`** — find an optimal order-5 Golomb ruler: 5 marks
  on a ruler with all pairwise differences distinct, minimizing the ruler's
  length (`solve minimize`). A `save_verified_minizinc_model` artifact (see
  [`save_verified_minizinc_model`](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/mcp-tools.md)) — `problem.md` and
  `solve-result.json` are the saved provenance, and `.openconstraint-model.json`
  is the manifest naming the recorded solve controls, so it also demonstrates
  reproducing a saved MiniZinc result via `solve_minizinc_files`.
  `examples/golomb_ruler/cpsat_python/` is the CP-SAT Python analogue of the
  same problem, scaled up to order 12 and saved at the `checked` verification
  level with both an expectation gate and a checker.
- **`examples/nonogram`** — a 5x5 nonogram puzzle (`solve satisfy`): shade
  cells so each row/column matches its block clues. Another
  `save_verified_minizinc_model` artifact, reinforcing the reproducibility
  workflow with a satisfaction (not optimization) `solve` method.
- **`examples/australia_map_coloring`** — colour Australia's seven
  states/territories with three colours so no two bordering regions share one
  (`solve satisfy`). Its data (`nc = 3`) is inline, so there is no `data.dzn`;
  instead it ships a `model.mzc.mzn` solution checker, so it doubles as a
  demonstration of the checker feature (see below).

For instance, to solve the knapsack example end to end:

```jsonc
// solve_minizinc_files
{
  "model_path": "examples/knapsack/model.mzn",
  "data_path": "examples/knapsack/data.dzn"
}
```

(prefer absolute paths in real MCP calls — see *Path-based file tools* above).

To run the Australia example *with* its solution checker, point `checker_path`
at the shipped `.mzc.mzn`:

```jsonc
// solve_minizinc_files
{
  "model_path": "examples/australia_map_coloring/model.mzn",
  "checker_path": "examples/australia_map_coloring/model.mzc.mzn"
}
```

The resulting `SolveResult.checker` report then carries the checker's
per-solution verdict (here, `CORRECT`).

The social-golfers model is parameterized through its `data.dzn`, which enables
two workflows beyond a single solve:

- **Longest schedule.** "As many weeks as possible" is the same model re-solved
  with `n_weeks` raised until the search stops finding schedules. For the
  shipped instance, `n_weeks = 7` solves and uses every one of `C(15,2) = 105`
  golfer pairs exactly once; `n_weeks = 8` would need 120 distinct pairs, which
  do not exist, so 7 is the true maximum — see the diagnosis-and-repair
  walkthrough right below for what solving past it actually looks like.
- **Multiple schedules.** To enumerate several distinct schedules, lower
  `n_weeks` (e.g. to 5) and request more than one solution with a solver that
  supports it — `num_solutions` works with `org.gecode.gecode` or
  `org.chuffed.chuffed`, not the default `cp-sat`.

### Diagnosing and repairing infeasibility

Pushing `n_weeks` past the shipped instance's maximum of 7 is a convenient way
to walk through the repair loop end to end, reusing the shipped model rather
than a one-off toy — and it demonstrates the loop's least convenient case
honestly, rather than a tidy `"unsatisfiable"` verdict:

1. **Solve past the maximum.** Solve `examples/social_golfers/model.mzn` with
   `n_weeks` overridden to `8` (either inline `data` text or a scratch
   `.dzn`) via `solve_minizinc_files`/`solve_minizinc_model`. Even though this
   instance genuinely has no solution — every pair of the 15 golfers is
   already used once by week 7, so an 8th week cannot avoid a repeat — cp-sat's
   search-based `solve` does not necessarily *prove* that quickly: with a
   short budget, `status` comes back `"unknown"`, not a clean `"unsatisfiable"`.
   This is the realistic trigger for reaching for a dedicated diagnostic
   rather than only a tidy failure case.
2. **Localize the conflict.** Call `find_unsat_core` (or `find_unsat_core_files`)
   with the *same* `n_weeks = 8` data. `find_unsat_core` runs findMUS, a
   different algorithm from cp-sat's search, so it is worth trying even after
   an inconclusive `solve`. This model encodes the pigeonhole argument through
   a single `sum(...) <= 1` constraint per golfer pair rather than many small
   named constraints, so a real run typically comes back `"no_core"` quickly —
   findMUS completing without isolating a MUS is a normal, documented outcome
   (see the `find_unsat_core` **Conservative `no_core`** caveat above), not a
   tool failure and not proof the instance is satisfiable either.
3. **Repair.** Neither `solve` nor `find_unsat_core` resolved this instance
   cleanly, so acting on it means also reasoning about the domain, not
   pattern-matching on a single field: the infeasibility here is a genuine
   counting bound (`8 * n_groups * C(group_size, 2) = 120` required pair
   meetings `>` `C(n_golfers, 2) = 105` total pairs), so the fix is relaxing
   the instance, not hunting for a modeling bug or waiting out a longer search.
   Drop `n_weeks` back to the shipped `7` (or fewer) and re-solve — `status`
   returns to `"satisfied"`.

This is the general MiniZinc infeasibility-repair loop — `solve` ->
inconclusive/`"unsatisfiable"` -> `find_unsat_core` -> read `core`/`stdout` ->
relax or fix the data or model -> `solve` again — applied to a case where
the "fix" is adjusting a parameter rather than editing constraints, and
neither diagnostic tool hands back a clean verdict on its own; see
`find_unsat_core` under [MCP tools](https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/mcp-tools.md) for the tool's full contract,
including the no-core and model-only-`core` caveats this walkthrough relies
on. `tests/test_examples_integration.py::test_social_golfers_diagnose_and_repair_infeasibility`
exercises this exact sequence against the real managed binary.

## Example inventory

A compact map from each retained example to the roadmap domain it covers, the
required workflow(s) it demonstrates (see the coverage list in this project's
closeout plan), the tool surface exercised, its test coverage, and any known
gap. Treat this as a coverage snapshot, not a completion gate: an empty "Gap"
cell means the example is not known to be missing anything for the workflow
listed, not that it is exhaustive.

| Example | Roadmap domain | Workflow(s) | Tools | Tests | Known gap |
| --- | --- | --- | --- | --- | --- |
| `examples/knapsack` | packing/knapsack | basic solve | `solve_minizinc_files` | `test_examples_integration.py::test_knapsack_files_solve_to_a_feasible_selection` | none |
| `examples/balanced_assignment` | assignment/allocation | basic solve | `solve_minizinc_files` | `test_examples_integration.py::test_balanced_assignment_files_solve_to_a_feasible_assignment` | no checker/portfolio demo on this example |
| `examples/social_golfers` | scheduling/rostering | infeasibility repair | `solve_minizinc_files`, `find_unsat_core_files` | `test_examples_integration.py::test_social_golfers_*` | the multiple-schedules (`num_solutions`) workflow described above has no dedicated test |
| `examples/australia_map_coloring` | assignment/allocation | checker-backed solve (acceptance) | `solve_minizinc_files(checker_path=...)` | `test_examples_integration.py::test_australia_map_coloring_with_shipped_checker_completes_correct` | the shipped checker only demonstrates acceptance; see the CP-SAT checkers below for a violation demo |
| `examples/golomb_ruler` | general CSP (no single roadmap domain) | reproducibility (save + file replay) | `save_verified_minizinc_model`, `solve_minizinc_files` | `test_examples_integration.py::test_golomb_ruler_files_reproduce_the_saved_optimum` (integration only, not part of default `just check`) | the `.openconstraint-model.json` manifest was dropped when `examples/` was untracked, so `test_examples_manifest.py` no longer covers this example |
| `examples/nonogram` | general CSP | reproducibility (satisfaction variant) | `save_verified_minizinc_model`, `solve_minizinc_files` | none | no automated coverage: the manifest fixture was dropped when `examples/` was untracked and no integration test exists for this example |
| `examples/nonogram/python` | general CSP | checked CP-SAT save for the same puzzle | `save_verified_cpsat_python` | none | no automated coverage: the manifest fixture was dropped when `examples/` was untracked, and there is no live CP-SAT replay integration test |
| `examples/cpsat_python/assignment.py` | assignment/allocation | CP-SAT direct solve | `run_cpsat_python` | `tests/pyexec/test_core_integration.py::test_run_cpsat_python_solves_assignment_example` | none |
| `examples/cpsat_python/scheduling.py` | scheduling/rostering | CP-SAT direct solve | `run_cpsat_python` | `tests/pyexec/test_core_integration.py::test_run_cpsat_python_solves_scheduling_example` | none |
| `examples/cpsat_python/graph_coloring.py` + `graph_coloring_checker.py` | assignment/allocation | checker-backed solve, incl. a violation | `run_cpsat_python`, checker protocol | `tests/test_cpsat_python_examples.py::test_graph_coloring_checker_*` | none |
| `examples/cpsat_python/clinic_roster_checker.py` | scheduling/rostering | checker rejecting a plausible-looking wrong answer | checker protocol | `tests/test_cpsat_python_examples.py::test_clinic_roster_checker_*` | exercised standalone against synthetic payloads; no paired `model.py` producing a live solve |
| `examples/online_printing_shop` | scheduling/optimization | checker-backed CP-SAT solve with resumable operations, machine calendars, and sequence-dependent setups | `run_cpsat_python_file`, `run_cpsat_python_file_checked` | `tests/examples/test_online_printing_shop.py` | only `data_sops1.json` proves optimality quickly (<1s) on the default single worker; `data_mops1.json` and `data_lops1.json` benefit from `config={"solver_time_limit_seconds": N}` (set well below `script_timeout_ms`, leaving room for parsing, model building, and serialization — nothing validates the two against each other) so CP-SAT stops itself and returns a clean `status="feasible"`/`"optimal"` with `return_code=0` — without it the child is killed at `script_timeout_ms`, but the checker now grades a recovered `status="timeout"` incumbent too (the checker's `solver_status` gate admits `optimal`/`feasible`/`timeout`, mirroring `pyexec/eligibility.py`), so a plain timeout kill still reaches an accepted verdict as long as a well-formed schedule was recovered. `data_lops1.json` additionally needs `num_workers` above the default 1, read from that same `config`: on one worker it spends a 150s limit and still reports `status="unknown"` with no incumbent (which the checker still refuses, since `unknown` carries no claimed solution), while `config={"solver_time_limit_seconds": 150, "num_workers": 8}` reaches an accepted feasible schedule. The callback stays installed regardless of `solver_time_limit_seconds`, because that limit cannot prove the script finishes before `script_timeout_ms`, but caps intermediate envelopes at 512 KiB total. Search continues after that budget is exhausted; a later kill recovers the last envelope that fit, while a clean run still prints its final best result |
| `examples/golomb_ruler/cpsat_python` | general CSP | checked save (expectation + checker gates) | `save_verified_cpsat_python` | none (manually re-verified live during this closeout, see `problem.txt`) | the saved objective is not exactly reproducible run to run (documented in `problem.txt`) — an expected CP-SAT property, not a bug; the manifest fixture was also dropped when `examples/` was untracked, so `test_examples_manifest.py` no longer covers this example |
| `examples/social_golfers/cpsat` + `cpsat_best` | scheduling/rostering | CP-SAT background job, saved artifact, and file-based replay | `submit_cpsat_python_file_job`, `get_cpsat_python_job`, `save_verified_cpsat_python` | `tests/pyexec/test_jobs_integration.py::test_submit_file_with_real_checker_reaches_optimal_and_accepted` | `cpsat/` (reported gate only, no checker) is superseded by `cpsat_best`; kept only for the reported-vs-checked contrast. `cpsat_best/replay-config.json` came from a `config`-only save, not an attached `experiment_result`, so this directory has no `experiment-log.json` — `RESULT.md` is a hand-written substitute for that provenance, not the generated artifact; see the explicit-experiment row below |
| `examples/social_golfers/cpsat_24` | scheduling/rostering | CP-SAT saved artifact for the 8-3-11 boundary instance | `save_verified_cpsat_python` | none | reported gate only; no checker or live replay integration test; the manifest fixture was also dropped when `examples/` was untracked, so `test_examples_manifest.py` no longer covers this example |
| *(no dedicated example file)* | — | CP-SAT explicit experiment (`run_cpsat_python_experiment`) with a durable `experiment-log.json` | `run_cpsat_python_experiment`, `save_verified_cpsat_python(experiment_result=...)` | `tests/pyexec/test_experiment_integration.py`; `tests/pyexec/test_save.py::test_save_with_matching_experiment_result_writes_experiment_log` | no shipped example directory pairs a real `examples/` script with a saved `experiment-log.json` — the integration test is a small self-contained fixture and the save-path test uses synthetic fixtures too; this is the one required workflow this closeout leaves undemonstrated on a real example rather than papering over |

## Managed runtime

The default managed runtime location is `<platformdirs user_data_dir>/minizinc`,
where `user_data_dir` comes from `PlatformDirs("openconstraint-mcp", "openconstraint-mcp")`.
Concretely:

| Platform | Default runtime root                                                                   |
| -------- | -------------------------------------------------------------------------------------- |
| Linux    | `~/.local/share/openconstraint-mcp/minizinc`                                           |
| macOS    | `~/Library/Application Support/openconstraint-mcp/minizinc`                            |
| Windows  | `%LOCALAPPDATA%\openconstraint-mcp\openconstraint-mcp\minizinc`                        |

> The doubled `openconstraint-mcp\openconstraint-mcp\…` segment on Windows is a
> `platformdirs` convention (appauthor *and* appname), not a path-computation bug.

The `minizinc` binary itself is expected at `<runtime>/bin/minizinc` (or
`<runtime>\bin\minizinc.exe` on Windows).

### Overriding the runtime path

Set the environment variable `OPENCONSTRAINT_MCP_RUNTIME_DIR` to override the
**runtime root directory** — *not* the path to the binary itself. The runtime
layer always appends `bin/minizinc` (or `bin\minizinc.exe`) underneath whatever
the env var points at.

For example, if your MiniZinc binary lives at `$HOME/minizinc-bundle/bin/minizinc`,
the correct override is:

```bash
export OPENCONSTRAINT_MCP_RUNTIME_DIR="$HOME/minizinc-bundle"
```

Setting `OPENCONSTRAINT_MCP_RUNTIME_DIR=/path/to/minizinc` directly (pointing at
the binary) will **not** work — the layer will look for `…/minizinc/bin/minizinc`
underneath it.

### Installing the managed runtime

`openconstraint-mcp install-runtime` is the supported way to put a managed
MiniZinc bundle on disk. The first invocation:

1. Resolves the install location. Precedence is `--runtime-dir` > the env var >
   the persisted install config > the platformdirs default
   (`<platformdirs user_data_dir>/minizinc`).
2. Streams the pinned MiniZinc bundle for your platform from the official
   MiniZinc GitHub release — the Linux x86_64 `.tgz`; the macOS `.dmg` on
   Apple Silicon (mounted read-only via `hdiutil` and reshaped into the same
   `bin`/`lib`/`share` layout); or, on Windows x86_64, the NSIS
   `setup-win64.exe` run silently (`setup.exe /S /D=<runtime>`) into the managed
   runtime directory — verifies its SHA256, installs it safely, and
   smoke-checks the resulting `bin/minizinc` (`bin\minizinc.exe` on Windows). On
   macOS the bundled Gecode is the
   Qt-linked build, so the installer vendors its Qt frameworks into
   `<runtime>/Frameworks` and relinks the solver to load them headlessly (no
   GUI is ever launched). That relink step uses the Xcode Command Line Tools, so
   run `xcode-select --install` first if `install-runtime` reports
   `install_name_tool` is missing.
3. Writes a small JSON config (`<platformdirs user_config_dir>/install.json`,
   typically `~/.config/openconstraint-mcp/install.json` on Linux) recording the
   chosen path.

On Windows, the NSIS installer requests administrator rights, so the first
`install-runtime` shows a one-time Windows UAC elevation prompt — confirm it to
let the silent install finish.

Once that config is written, subsequent `check-runtime` and `list-solvers` calls
find the runtime automatically — no env-var fiddling. To reset, delete the
config file, or set `OPENCONSTRAINT_MCP_RUNTIME_DIR` (the env var always wins).
If the config file is present but corrupt (e.g. hand-edited into invalid JSON),
`check-runtime` and `list-solvers` print a warning to stderr and fall back to the
default location rather than failing silently.

If you pass `--runtime-dir <path>` again on a later install, the new path
replaces the old one in the config. The previous runtime directory is not
touched and can be deleted manually.

A successful install also writes a `.openconstraint-runtime.json` marker into
the runtime directory itself. Future `install-runtime` invocations check that
marker before overwriting: an unmanaged non-empty directory is refused
regardless of `--yes`, which makes `--runtime-dir $HOME --yes` (or similar)
safe — your home directory cannot be wiped by a fat-finger.

### Startup diagnostic

On startup the MCP server prints a short three-line diagnostic to **stderr**:
the server version, the resolved runtime directory, and whether the managed
runtime is installed (with an `install-runtime` hint when it is absent). This
banner is **stderr-only by design** — over the stdio transport, `stdout` is the
JSON-RPC protocol channel, so the diagnostic never touches it. The banner only
*reads* the already-resolved runtime status; it never downloads or installs
anything. MCP clients that hide server stderr simply will not show it.

The server also advertises its project `Homepage` to MCP clients via the
`website_url` field, sourced from the package metadata (single source of truth:
`[project.urls]` in `pyproject.toml`).

## v0 limitations

This is an early release; the focus is "easy install, reliable solving, clear
errors" rather than feature breadth. In particular:

- **The automated installer covers Linux x86_64, macOS arm64 (Apple Silicon),
  and Windows x86_64.** Windows ARM, Linux ARM, and macOS x86_64 (Intel) bundles
  are tracked separately. On those platforms, `install-runtime` exits 1 with a
  clear message — use `configure-runtime --runtime-dir <path>` or point
  `OPENCONSTRAINT_MCP_RUNTIME_DIR` at an existing MiniZinc install (a directory
  containing `bin/minizinc`) in the meantime.
- **No telemetry, ever**, unless and until you explicitly opt in to a clearly
  labelled future feature.
- **The only code path that touches the network is the `install-runtime` CLI
  command.** The package does not phone home; `httpx` is only imported when
  `install-runtime` runs (enforced by a regression test).

## Licensing & upstream sources

`openconstraint-mcp` is licensed under the Apache License 2.0; see `LICENSE`.
The MiniZinc runtime it wraps is
**fetched** from the official MiniZincIDE GitHub release at install time — the
Linux x86_64 `.tgz`, the macOS `.dmg`, or the Windows x86_64 NSIS
`setup-win64.exe`, depending on your platform — this repository does **not**
redistribute MiniZinc or its bundled solvers.

The upstream bundle includes:

- MiniZinc itself (the constraint modelling language and its compiler).
- Gecode, Chuffed, OR-Tools CP-SAT, COIN-BC, and other solvers shipped with the
  MiniZincIDE bundle. Their licenses are surfaced upstream — see
  [minizinc.org](https://www.minizinc.org/) for the license index, or the
  per-solver entries on the
  [MiniZincIDE release page](https://github.com/MiniZinc/MiniZincIDE/releases).

After `install-runtime`, each bundled component's license file lives inside the
installed runtime tree (typically under `<runtime_dir>/share/minizinc/...` and
adjacent directories) and is left untouched by the installer. For a single
authoritative document, the MiniZincIDE release page is the recommended source.

## Releasing (maintainers)

`.github/workflows/release.yml` uses PyPI Trusted Publishing, so no PyPI token is
stored in GitHub. A manual workflow run publishes only to TestPyPI; publishing to
PyPI requires a version tag and approval of the protected `pypi` environment.

A Trusted Publisher is bound to an exact owner, repository, workflow filename, and
environment — here `Openconstraint`, `openconstraint-mcp`, `release.yml`, and
`pypi`/`testpypi`. Changing any of them requires re-registering the publisher.

### One-time setup

1. Create the GitHub environments `testpypi` and `pypi`. Require the maintainer as a
   reviewer for `pypi`. A solo maintainer must leave **Prevent self-review**
   disabled, and should uncheck **Allow administrators to bypass** so the approval
   applies to admins too. Restrict `pypi` deployments to tags matching `v*`, and
   `testpypi` deployments to the `master` branch — the TestPyPI job runs from a
   manual dispatch, so a tag rule there would reject every rehearsal.
2. Create and verify separate accounts on [TestPyPI](https://test.pypi.org/) and
   [PyPI](https://pypi.org/), enable 2FA, and store recovery codes safely.
3. On each account's **Publishing** page, add a pending GitHub Trusted Publisher with:
   project `openconstraint-mcp`, owner `Openconstraint`, repository
   `openconstraint-mcp`, workflow `release.yml`, and environment `testpypi` or `pypi`
   respectively. Do not create an API token.

### TestPyPI rehearsal

1. Run `just check` and `just build` locally.
2. After the release workflow is on the default branch, open GitHub **Actions →
   Release → Run workflow** and run it from that branch. This path can publish only
   to TestPyPI.
3. Approve the `testpypi` deployment if that environment has a required reviewer.
4. Smoke-test the uploaded package (replace the version after the first rehearsal):

   ```bash
   uv run --isolated --no-project \
     --with "openconstraint-mcp==0.1.0" \
     --index https://pypi.org/simple/ \
     --default-index https://test.pypi.org/simple/ \
     openconstraint-mcp --help
   ```

   `--index` outranks `--default-index`, so dependencies (pydantic, httpx, ...)
   resolve from PyPI; only the unreleased `openconstraint-mcp` version — absent
   from PyPI — falls through to TestPyPI. A bare `--index test.pypi.org` would
   make TestPyPI's stale/alpha releases of common dependency names (e.g.
   `pydantic` only goes up to `1.5a1` there) win resolution and break the smoke
   test.

TestPyPI never overwrites a release. Increment the version before repeating a
rehearsal whose version is already present there. TestPyPI and PyPI are separate, so
using `0.1.0` on TestPyPI does not prevent publishing `0.1.0` to PyPI.

### PyPI release

After the rehearsal and default-branch CI are green, verify that the version in
`pyproject.toml` is the intended release, then create and push only that tag:

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

Approve the waiting `pypi` deployment in GitHub Actions. Without both the tag push
and that approval, the workflow cannot publish to PyPI.
