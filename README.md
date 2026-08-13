# openconstraint-mcp

[![CI](https://github.com/Openconstraint/openconstraint-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Openconstraint/openconstraint-mcp/actions/workflows/ci.yml)

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
  [Background solve jobs](#background-solve-jobs),
  [Background portfolio jobs](#background-portfolio-jobs), and
  [Background CP-SAT jobs](#background-cp-sat-jobs).
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
  [Reproducing a saved CP-SAT artifact](#reproducing-a-saved-cp-sat-artifact)
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

After installing the package above. These commands assume `uv tool install` put
`openconstraint-mcp` on your `PATH`; prefix them with `uvx` or `uv run` if you
chose one of the other installation paths.

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
   `(timeout_ms + 8000) + (checker_timeout_ms + 8000)` milliseconds; a default
   60-second tool timeout abandons solves the server is still running.

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

> **This section is the full-profile catalog.** It documents every tool the
> server can expose. The default `stdio` profile is **core** and advertises only
> eight of them (`check_runtime`, `list_available_solvers`,
> `check_minizinc_model`, `solve_minizinc_model`, `check_minizinc_files`,
> `solve_minizinc_files`, `run_cpsat_python`, `run_cpsat_python_file`) plus the
> single `solve_constraint_problem` [MCP prompt](#mcp-prompts). The advanced
> tools and the three detailed prompts below require
> `openconstraint-mcp stdio --toolset full` (see [CLI](#cli)).

The stdio server exposes two runtime-introspection tools, a model-check tool, a
model-inspection tool, an execution tool, an unsat-core diagnostic tool, and
background/portfolio job tools — each of the MiniZinc tools in an **inline-source**
form (below) and a **path-based file** sibling ([Path-based file tools](#path-based-file-tools)) — plus a
verified-save tool that persists a successful inline workflow to a local
project directory. The two solve
tools also accept optional solution checkers, so a normal solve can validate each
produced solution against a checker model without changing result shape:

- **`check_runtime`** — returns a `RuntimeStatus` with fields
  `installed: bool`, `runtime_dir: str`, and `minizinc_binary: str | None`.
- **`list_available_solvers`** — returns a `SolverList` of `SolverInfo` entries
  (`id`, `name`, `version`, `tags`, and a `capabilities` object), plus a
  top-level `capability_note`. `capabilities`
  carries `supports_all_solutions` (`-a`), `supports_free_search` (`-f`),
  `supports_parallel` (`-p`), `supports_random_seed` (`-r`),
  `supports_num_solutions` (`-n`), and an advisory `std_flags` list — deterministic
  facts read from the managed runtime's `--solvers-json` config for client-side
  solver routing. `supports_num_solutions` is the conservative gate
  (`org.gecode.gecode` / `org.chuffed.chuffed` only, matching the `num_solutions`
  solve control). The four `-a/-f/-p/-r` facts are **enforced** for the named
  controls they correspond to: a requested `all_solutions` / `free_search` /
  `parallel` / `random_seed` is rejected before solving when the selected solver's
  `stdFlags` omit the matching flag. Enforcement is by exact canonical solver `id`
  (the same stance as the `num_solutions` gate), so select non-default solvers by
  canonical id to get the upfront rejection — a short alias (e.g. `gecode`) or an
  unknown solver does not resolve and passes through to MiniZinc unchanged.
  `std_flags` stays advisory — it reports the standard flags the
  solver configuration declares and is **not** a passthrough, so clients cannot
  send those flags back into `solve_minizinc_model` / `solve_minizinc_files`.
  Alongside the structured `SolverList`, the tool returns model-visible text
  content presenting a complete `id`/`name`/`version` inventory table of
  **every** solver (with a final-answer requirement to copy the table without
  omitting rows, converting it to bullets/prose, summarizing, or grouping
  entries), followed by a user-visible note that detailed solver capabilities
  can be requested, a `num_solutions` routing note, and a caution that a declared
  MIP solver may still need separate binaries/licenses to run. The full
  `capabilities` metadata stays in the structured result and is not printed by
  default — request it explicitly to surface the `supports_*` booleans and
  `std_flags`. Raises a runtime-missing error if the managed MiniZinc binary is
  not present.
- **`check_minizinc_model`** — compile-check a complete MiniZinc model
  through the managed local runtime **without solving it**. This is the
  cheap pre-flight before `solve_minizinc_model`: it runs MiniZinc's
  dry-run compile (`-c`) for the chosen solver, flattening the model to
  FlatZinc but stopping before the search, so it catches syntax, type,
  missing-include, invalid-domain, and unsupported-construct errors in a
  fraction of a solve. Arguments:

  - `model: str` — the complete MiniZinc source. Must not be empty.
  - `data: str | None = None` — optional inline MiniZinc data (`.dzn`
    contents — any data assignments, not parameter-only) provided directly
    as text; omit (or pass `null`) for models that need no external data.
    It is written to a private temp file alongside the model and passed to
    the managed runtime as a positional `.dzn` data file (MiniZinc's
    `model.mzn data.dzn` order) — never a client-supplied path. A
    parameterized model needs its data to flatten, so check it with the same
    `data` you intend to pass to `solve_minizinc_model`.
  - `solver: str = "cp-sat"` — passed through verbatim to MiniZinc's
    `--solver` flag. The compile is solver-aware, so a model that
    compiles for one solver may not for another — check against the
    solver you intend to solve with. An unknown or unavailable solver is
    a compile failure: it surfaces as `status="error"` with MiniZinc's
    diagnostic in `stderr`, not as an MCP error.
  - `timeout_ms: int = 30000` — compile budget in milliseconds, enforced
    as a wall-clock cap on the runtime subprocess (plus a few seconds'
    grace). It is also passed through to MiniZinc's `--time-limit`, but
    that flag primarily bounds *solving*, so for a compile the subprocess
    cap is the real stop. Must be strictly positive (`0` is a validation
    error, not "no timeout").

  Returns a `CheckResult` with fields:

  - `status: str` — one of `"ok"`, `"error"`, `"timeout"`. `"ok"` means
    **the model compiles, not that it is satisfiable** — compilation does
    not run the search, so a clean check does not guarantee a solution
    exists (that is only known after solving).
  - `solver: str` — the solver the model was flattened for, echoed from
    the request.
  - `truncated: bool` — `true` when the child's combined stdout+stderr
    exceeded the **1 MiB** output cap (same contract as
    `solve_minizinc_model`'s `truncated`): `stdout`/`stderr` are partial and
    the `diagnostic` is `output_truncated`. The `status` stays return-code
    driven, so a clean exit that overran the cap is still `"ok"` — the
    compile verdict holds even when the captured output does not.
  - `stdout: str` — the runtime's raw stdout (normally empty on a clean
    compile).
  - `stderr: str` — the runtime's raw stderr (compile diagnostics and
    warnings land here).
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.

  **Failure-mode contract.** As with `solve_minizinc_model`, environment
  and argument problems — runtime not installed, empty `model`,
  non-positive `timeout_ms`, OS-level failure to exec the managed binary —
  surface as **MCP errors**. Compile diagnostics come back as a normal
  `CheckResult` with `status="error"` and the diagnostic in `stderr`, so a
  client LLM can repair the model and re-check without exception handling.

  **Recommended loop.** `check_minizinc_model` is the validate step in
  **draft → check → repair → solve → explain**: draft a model, check it,
  repair on `status="error"` and re-check until `"ok"`, then hand the clean
  model to `solve_minizinc_model`. Validating first turns a class of
  failures into cheap compile errors instead of spent solve attempts. When
  the model uses inline data, pass the **same** `data` to both the check and
  the solve call so you validate and solve the same instance.

- **`inspect_minizinc_model`** — inspect a model's **interface without solving
  it**. It wraps the managed runtime's `--model-interface-only` flag, which runs
  MiniZinc's type analysis and stops *before* flattening or search, so it is even
  cheaper than `check_minizinc_model`. Use it to discover what data a model needs
  (so a client LLM can build a correct `.dzn`) and what it outputs, before
  spending a solve. Arguments:

  - `model: str` — the complete MiniZinc source. Must not be empty.
  - `data: str | None = None` — optional inline `.dzn` data, written to a
    private temp file beside the model and passed as a positional data file
    (same contract as `check_minizinc_model`). Supplying data narrows the
    reported `required_parameters` (see below); omit it to see the model's full
    required set.
  - `solver: str = "cp-sat"` — passed through to `--solver`. Interface
    extraction is solver-independent in practice, but the flag is accepted for
    consistency with the other tools.
  - `timeout_ms: int = 30000` — wall-clock budget (must be strictly positive);
    shares the `check` default, since inspection is a comparable pre-flight.

  Returns a `ModelInspectionResult` with fields:

  - `status: str` — one of `"ok"`, `"error"`, `"timeout"`. **`"ok"` means only
    that the interface was *extracted* — it is NOT a data-completeness signal.**
    A no-data inspection is `"ok"` with a *non-empty* `required_parameters`
    (that is the whole point of the tool). Completeness is signalled solely by
    `required_parameters == {}`.
  - `solver: str` — echoed from the request.
  - `interface: ModelInterface | None` — populated **only when `status="ok"`**,
    with fields:
    - `method: str` — the solve kind, one of `"sat"`, `"min"`, `"max"`.
    - `required_parameters: dict[str, InterfaceType]` — the parameters **still
      needing a value** given any `data` you passed. With no data this is the
      model's full required set; supplying the matching data shrinks it to `{}`.
    - `output_variables: dict[str, InterfaceType]` — the model's output variables.
      **Advisory:** with an `output` item this tracks the output-referenced
      variables and excludes functionally-defined ones, so treat it as "the
      model's output variables", not "every decision variable".
    - `has_output_item: bool` — whether the model declares an `output` item.
    - `globals: list[str]`, `included_files: list[str]` — as reported by the
      runtime.

    Each `InterfaceType` carries `base_type` (one of `"int"`, `"bool"`,
    `"float"`, `"string"`, `"tuple"`, `"record"`, `"ann"`), `dim` (array
    dimensionality; `0` for a scalar), `is_set` (`true` for a set type), and
    `is_optional` (`true` for an `opt` type). `"ann"` is MiniZinc's annotation
    type — e.g. an `array[1..2] of ann` search-strategy list passed to
    `seq_search`. **This mode does not surface:** enum-typed entries appear as
    `base_type="int"` (enum names are not exposed — infer them from the model
    text); variable domains and parameter ranges (e.g. `1..n`) are not reported;
    array index sets are not reported, only the `dim` count; and `tuple`/`record`
    entries carry only the tag, not their component types.
  - `truncated: bool` — output-cap overrun flag, same contract as
    `check_minizinc_model`'s `truncated` (`stdout`/`stderr` partial,
    `diagnostic` is `output_truncated`).
  - `stdout: str` / `stderr: str` — the runtime's raw output. A *successful*
    inspection may still emit warnings to `stderr`, so `status="ok"` does not
    depend on empty `stderr`.
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.

  **Failure-mode contract.** Identical to `check_minizinc_model`: environment and
  argument problems (runtime missing, empty `model`, non-positive `timeout_ms`,
  OS-level exec failure) surface as **MCP errors**; a model type/syntax error
  comes back as a normal `ModelInspectionResult` with `status="error"`,
  `interface=None`, and the diagnostic in `stderr`.

- **`solve_minizinc_model`** — run a complete MiniZinc model through the
  managed local runtime. Arguments:

  - `model: str` — the complete MiniZinc source (declarations, constraints,
    exactly one `solve` statement, and an `output` block). Must not be empty.
  - `data: str | None = None` — optional inline MiniZinc data (`.dzn`
    contents — any data assignments, not parameter-only) provided directly
    as text; omit (or pass `null`) for models that need no external data.
    It is written to a private temp file alongside the model and passed to
    the managed runtime as a positional `.dzn` data file (MiniZinc's
    `model.mzn data.dzn` order) — never a client-supplied path.
  - `checker: str | None = None` — optional inline MiniZinc checker source,
    written beside the model as `checker.mzc.mzn` and passed through
    MiniZinc's `--solution-checker` flag. Omit it for an ordinary solve.
  - `solver: str = "cp-sat"` — passed through verbatim to MiniZinc's
    `--solver` flag.
  - `timeout_ms: int = 30000` — solving budget in milliseconds. Must be
    strictly positive. `0` is **not** "no timeout" — it is a validation
    error. Pass a real budget, or omit the argument to get the default.
  - `free_search: bool = False` — when true, passes `-f`: the solver may
    ignore the model's search annotations and use its own search strategy.
    This means "search freely", **not** "no search"; its effect is
    solver-dependent (large for Chuffed's LCG, often minor for CP-SAT).
  - `parallel: int | None = None` — when set, passes `-p <n>` to request `n`
    parallel search threads. Must be `>= 1`.
  - `random_seed: int | None = None` — when set, passes `-r <n>` to seed the
    solver's randomization. Any int is accepted.
  - `all_solutions: bool = False` — when true, passes `-a`: enumerate every
    solution (satisfaction) or the optimization improving-sequence, all
    captured in order in `solutions`.
  - These four `-a/-f/-p/-r` controls are **capability-gated**: if the selected
    solver's runtime-local `stdFlags` (see `list_available_solvers`) do not
    declare the matching flag, the request is rejected **before solving** with an
    actionable error naming the solver, the control, and the flag. The check
    matches the solver by exact canonical `id`; a short alias (e.g. `gecode`) or
    an unknown solver does not resolve and passes through to MiniZinc unchanged.
  - `num_solutions: int | None = None` — when set, passes `-n <n>` to cap the
    number of solutions for a **satisfaction** problem. Must be `>= 1`. It is
    **solver-gated**: only `org.gecode.gecode` and `org.chuffed.chuffed`
    support `-n`; the default `cp-sat` (and any other solver) returns a clear,
    actionable error instead of a broken run. It is **not** meaningful for
    optimization (`minimize`/`maximize`) — use `all_solutions` there for the
    improving sequence. For multiple optimal solutions, first solve the
    optimization to a proven optimum, then re-solve as a satisfaction model
    with the objective fixed to that value and use a supported
    `num_solutions` solver.

  All five search controls are optional and **solve-only** (not on the check
  or findMUS tools); with none set, the invocation is byte-identical to the
  default solve.

  Returns a `SolveResult` with fields:

  - `status: str` — one of `"timeout"`, `"error"`, `"unsatisfiable"`,
    `"unbounded"`, `"unsat_or_unbounded"`, `"unknown"`, `"optimal"`,
    `"satisfied"` (precedence in that order — see the source for details).
  - `solver: str` — the solver name that ran, echoed from the request.
  - `return_code: int | None` — the managed binary's subprocess return code,
    or `null` when the outer subprocess timeout fired before a real return
    code existed (so `null` on `status="timeout"`), **or** when the output cap
    tree-killed the child — that exit code is the server's artifact rather
    than the model's. A fast writer that overran the cap but exited on its own
    keeps its genuine exit code (`truncated=true` with a non-null
    `return_code`).
  - `timed_out: bool` — `true` when the subprocess wall-clock cap fired. This
    is explicit process-timeout metadata; today it is redundant with
    `status="timeout"`, not a new independent solver signal.
  - `truncated: bool` — `true` when the child's combined stdout+stderr exceeded
    the **1 MiB** output cap (the same cap and file-backed capture the CP-SAT
    runner uses). The process tree is killed if still running, but a fast
    writer can overrun the cap and exit cleanly before the executor's poll
    loop sees it. Partial parsed solutions are
    **kept** (each `--json-stream` line is a complete record) and the
    `diagnostic` is `output_truncated` either way. On a cap tree-kill,
    `return_code` is `null` and `status` is the stream's verdict if one
    arrived else `satisfied` when solutions survived else `unknown` (never the
    rc-derived `error`); a clean-exit overrun instead keeps its genuine
    `return_code` and can still classify as the rc-derived `error`. A trivially reachable trigger is
    `all_solutions=true` enumeration on a high-cardinality satisfaction model;
    page with `num_solutions` on `org.gecode.gecode`/`org.chuffed.chuffed`, or
    reduce the model's `output`.
  - `stdout: str` — the human-readable solution text, **reconstructed** from
    the solve stream's `default` output sections (one solution's `output`
    block per block). When a model declares no explicit `output` item the
    stream carries only the `json` section, so each solution's block is instead
    synthesized as `name = <value>;` lines from its variable map (objective
    excluded) — the solution is shown either way. Solve runs use MiniZinc's
    `--json-stream` transport, so this is the rendered solution text, not the
    literal process bytes (which are line-delimited JSON); when no checker is
    supplied, the raw stream is not surfaced.
  - `stderr: str` — the run's **diagnostic channel**: the managed process's
    real stderr plus any solve-stream `error`/`warning` messages folded in
    (deduplicated). `--json-stream` may route model/solver diagnostics into
    the stdout stream as error objects, so they are collected here regardless
    of channel — read `stderr` for what went wrong.
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.
  - `solution: dict[str, Any] | None` — the best/last solution as a
    variable-name → value map (the stream's `json` section, model variables
    only; the objective is reported separately, not folded in). `null` when
    no solution was produced.
  - `solutions: list[dict[str, Any]]` — every emitted solution in order (the
    optimization improving-sequence, or an `all_solutions` enumeration). Its
    last entry is `solution`; `[]` when none.
  - `objective: int | float | None` — the best objective, taken from the last
    solution. `null` for pure-satisfaction problems and when no solution was
    produced.
  - `statistics: dict[str, str]` — best-effort solver statistics, merged from
    the stream's `statistics` objects (typed values stringified, last-wins on
    duplicate keys). May be `{}` when none were emitted; the key set is
    solver- and version-defined, **not** a stable contract. Unlike the prior
    stdout scrape, these are **driver-emitted** sibling stream objects, so a
    model's `output` block can no longer forge them.
  - `checker: CheckerReport | None` — `null` unless a checker was supplied.
    When present, it carries:
    - `status: str` — one of `"completed"`, `"violation"`, `"no_solution"`,
      `"error"`, `"timeout"`.
    - `checks: list[SolutionCheck]` — one checker verdict per produced solution,
      index-aligned with `solutions` when checking completed or found a
      violation. Each entry has `violation: bool` and `output: str`.
    - `transcript: str` — the authoritative raw `--json-stream` transcript,
      including both solve and checker objects. `stdout` remains the
      reconstructed solution text only.

  **Solution checking.** Checking augments a normal solve: it adds exactly
  `--solution-checker` to the same managed MiniZinc invocation, so `free_search`,
  `parallel`, `random_seed`, `all_solutions`, and supported `num_solutions` all
  compose with it. A checker's `CORRECT`/`INCORRECT` text is surfaced verbatim in
  `checker.checks[].output` and is **not** interpreted by the server; only a
  nested `UNSATISFIABLE` makes `checker.status="violation"`. Rejected solutions
  still appear in `solutions`, so consult the aligned checks before treating each
  produced solution as valid. A checker validates solution correctness and can
  recompute an objective, but it never proves optimality — `status` remains the
  completeness/optimality signal.

  Inline checkers run in the same private temp directory as the inline model, so
  they may include the co-located `model.mzn` but cannot resolve arbitrary
  project-relative local includes. For multi-file checker projects, use
  `solve_minizinc_files` with `checker_path`.

  The MCP response also includes model-visible text content with status,
  solver metadata, stdout/stderr, and a `Statistics:` section whenever
  the parsed `statistics` map is non-empty. That text includes an explicit
  final-answer requirement telling the client's LLM not to omit the section.
  `structuredContent` still carries the complete validated `SolveResult` for
  clients that consume structured output directly.

  **Division of labor.** The `minizinc_solution_workflow` MCP prompt (below)
  guides the client LLM to draft a MiniZinc model; `solve_minizinc_model`
  executes that drafted model locally and returns the runtime's verbatim
  output. `LLM proposes, server verifies.`

  **Failure-mode contract.** Environment and argument problems —
  runtime not installed, empty `model`, non-positive `timeout_ms`, OS-level
  failure to exec the managed binary — surface as **MCP errors** the
  client must surface to the user. Solving outcomes — unsat, unbounded,
  timeout, MiniZinc model/syntax/type/solver errors — come back as a
  normal `SolveResult` whose `status` field encodes the outcome, so a
  client LLM can branch on it (and feed `stderr` back into a revise-and-
  retry loop) without exception handling.

- **`find_unsat_core`** — diagnose why a MiniZinc model is unsatisfiable by
  wrapping findMUS (`org.minizinc.findmus`) through the managed runtime.
  This complements the solve loop: when `solve_minizinc_model` returns
  `status="unsatisfiable"`, call `find_unsat_core` to localize the conflict.
  Pass the **same** `data` you passed to that solve: a parameterized model
  needs it to flatten at all, and diagnosing a different instance than the
  one that proved unsat is meaningless. Arguments:

  - `model: str` — the complete MiniZinc source. Must not be empty.
  - `data: str | None = None` — optional inline MiniZinc data (`.dzn`
    contents — any data assignments, not parameter-only) provided directly
    as text; omit (or pass `null`) for models that need no external data.
    It is written to a private temp file alongside the model and passed to
    the managed runtime as a positional `.dzn` data file (MiniZinc's
    `model.mzn data.dzn` order) — never a client-supplied path.
  - `timeout_ms: int = 30000` — findMUS budget in milliseconds. Must be
    strictly positive. `0` is a validation error, not "no timeout".

  Returns an `UnsatCoreResult` with fields:

  - `status: str` — one of `"mus_found"`, `"no_core"`, `"error"`,
    `"timeout"`. Clients branch on this field; there is no derived
    `core_found` flag.
  - `core: list[UnsatCoreConstraint]` — best-effort structured constraints
    from the submitted model, each with `line`, `column`, `end_line`,
    `end_column`, and `source`. This may be empty even when a MUS was found.
  - `message: str` — short run-specific summary.
  - `truncated: bool` — output-cap overrun flag, same contract as
    `check_minizinc_model`'s `truncated`. A truncated findMUS transcript may
    have lost MUS lines beyond the cap, so a `no_core` (or even `mus_found`)
    verdict parsed from it may be incomplete — the `diagnostic` is
    `output_truncated` rather than the verdict's usual category.
  - `stdout: str` — raw findMUS output, preserved verbatim and authoritative.
  - `stderr: str` — raw runtime diagnostics.
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.

  **MUS caveat.** The tool reports **a** minimal unsatisfiable subset:
  constraints that are jointly unsatisfiable and from which none can be
  removed while staying unsatisfiable. Minimal does **not** mean globally
  smallest, and a model may have several MUSes.

  **Model-only `core`.** The structured `core` is **best-effort** and
  resolves **model-file** spans only; raw `stdout` is authoritative. A
  `.dzn` cannot contain `constraint` items, but assigning a *decision
  variable* in data is equivalent to a constraint, so if the client does
  that, a MUS member can originate in the data file — it appears in raw
  `stdout` but is **not** added to `core`. Do not treat `core` as a
  complete enumeration of the conflict.

  **Conservative `no_core`.** `status="no_core"` means findMUS completed
  without reporting a MUS, **not** that the model is satisfiable. A tight
  `timeout_ms` can also surface as `no_core` rather than `timeout` if
  findMUS stops at its own `--time-limit` with return code 0.

  **Failure-mode contract.** Environment and argument problems — runtime not
  installed, empty `model`, non-positive `timeout_ms`, OS-level failure to
  exec the managed binary — surface as **MCP errors**. findMUS outcomes —
  MUS found, no MUS reported, findMUS/runtime diagnostics, and timeout — come
  back as a normal `UnsatCoreResult` whose `status` encodes the outcome.

- **`save_verified_minizinc_model`** — persist a *successful* inline MiniZinc
  workflow to a local project directory, **after the server re-verifies it**
  through the managed runtime. The inline tools above are ephemeral by design:
  a model that checked and solved exists only in the conversation. This tool
  turns that result into a durable local project — without trusting the
  client's claim that the model worked. Arguments:

  - `model: str` — the complete MiniZinc source to verify and save.
  - `target_dir: str` — **explicit absolute path** of the directory to create
    or update; its parent must already exist. The server opens **no OS file
    dialog or picker** — choosing the path is the client's job (ask the user,
    or use the client's own UI), and the chosen path is passed here. MCP
    elicitation is deliberately **not** used or required in v1; the explicit
    `target_dir` argument is the durable contract that works in every client.
  - `data: str | None = None`, `checker: str | None = None` — optional inline
    `.dzn` data and solution-checker source, with the same semantics as
    `solve_minizinc_model`; the re-check and re-solve both use them.
  - `problem: str | None = None` — the user's original natural-language
    problem text. Saved only when passed explicitly; the server never infers
    or retains conversation history.
  - `solver`, `timeout_ms`, `free_search`, `parallel`, `random_seed`,
    `all_solutions`, `num_solutions` — the same solve controls as
    `solve_minizinc_model`, applied to the verifying solve and recorded in
    the manifest so the recorded verification is reproducible.
  - `overwrite: bool = False` — required to replace a previous save (see the
    overwrite gate below).
  - `portfolio_result: PortfolioSolveResult | None = None` — optional.
    Attaches a MiniZinc solver-portfolio race's full attempt table (from
    `submit_portfolio_job`/`get_portfolio_job`, see
    [Solver portfolios](#solver-portfolios)) as **provenance only** — it is
    never used as verification evidence; the save still re-runs
    check/solve/checker fresh and gates on that alone. Rejected eagerly
    (before any check/solve) unless `portfolio_result.status == "winner"`,
    the winning attempt's `solver`/`seed` match this call's
    `solver`/`random_seed` (an unseeded winner matches an unseeded save),
    the winning formulation's/`data`'s hash matches `model`/`data`, and the
    race's shared `solve_controls`
    (`free_search`/`parallel`/`all_solutions`/`num_solutions`) match this
    call's — the save must replay the winning attempt's search configuration
    (`timeout_ms`, a budget rather than search configuration, is not gated).
    A `checker_sha256` mismatch is **not** rejected — the fresh checker gate
    still decides.

  **Verification gate.** Before anything is written, the server re-runs the
  compile check and then the solve on the artifacts exactly as supplied. The
  save proceeds only when the check is `"ok"` **and** the solve finished
  `"satisfied"` or `"optimal"` with a clean exit and no timeout **and** —
  when a `checker` is supplied — the nested checker report is `"completed"`
  (the checker ran without machine-readable violation; **not** a proof of
  optimality). Any other outcome returns `status="not_verified"` carrying the
  gating `check`/`solve` results and writes **nothing**.

  **Artifact layout.** The saved directory uses fixed filenames — the only
  user-chosen path is the directory itself:

  | File | Written | Contents |
  | --- | --- | --- |
  | `model.mzn` | always | the verified model source, verbatim |
  | `data.dzn` | only when `data` was passed | the `.dzn` text (may be empty) |
  | `checker.mzc.mzn` | only when `checker` was passed | the checker source |
  | `problem.md` | only when `problem` was passed | the original problem text |
  | `solve-result.json` | always | the verifying `SolveResult` as JSON |
  | `experiment-log.json` | only when `portfolio_result` was passed and the save succeeded | the portfolio's full attempt table (every model/solver/seed tried, statuses, checker verdicts), the race's shared solve controls, plus the winner's index/seed/solver |
  | `.openconstraint-model.json` | always | manifest: tool version, timestamp, `backend` (`"minizinc"`), solver, the solve controls used, a verification summary (including a compact experiment-log summary when `portfolio_result` was supplied; `statuses_seen` lists MiniZinc result statuses, while `attempt_states_seen` lists portfolio lifecycle states), and per-file sha256 hashes |

  **Overwrite safety (marker-gated).** A brand-new path or an existing empty
  directory is written directly. A non-empty directory is replaced only when
  *all three* hold: it contains a prior save's `.openconstraint-model.json`
  manifest, `overwrite=true` was passed, and it holds no files the prior save
  did not write. Anything else — user files present, an unrecognizable
  manifest, a missing `overwrite` — is refused with an actionable MCP error
  before any solver runs. Replacement is wholesale, via a staged hidden
  sibling directory and atomic rename swap (restoring the prior directory
  from its backup if the swap itself fails), so a save can never leave a
  half-written directory or a stale file from an earlier save behind.

  Returns a `SaveVerifiedModelResult`: `status` (`"saved"` /
  `"not_verified"`), `message`, the resolved `target_dir` (echoed on both
  outcomes; on `not_verified` it names the directory that was *not* written),
  `files` (role, bare filename, and sha256 per saved file — empty unless
  `saved`), `check` (always present), and `solve` (`null` when the check gate
  already failed). The save runs entirely locally: no network, no LLM, no
  telemetry — and it writes only inside (and, transiently while staging,
  beside) the explicit `target_dir`.

  **Reproducing a saved artifact:** there is no dedicated inspect/rerun tool —
  read `.openconstraint-model.json` directly (it names the `backend` and the
  `solve_controls` used) and call `solve_minizinc_files` with the saved
  `model.mzn`/`data.dzn`/`checker.mzc.mzn` paths, `solver`, `timeout_ms`, and
  the recorded solve controls, then compare the returned `SolveResult` to the
  saved `solve-result.json`. See [Reproducing a saved CP-SAT
  artifact](#reproducing-a-saved-cp-sat-artifact) for the CP-SAT Python
  equivalent.

### Background solve jobs

`solve_minizinc_model` blocks until the solve finishes, which a hard problem
can outrun a client's synchronous request timeout. The job tools run the same
inline solve as a **background job**: submit returns immediately with a
`job_id`, and the client polls for the result on its own schedule. The job
registry is **in-process and ephemeral** — jobs do not survive a server
restart — and runs entirely locally through the managed runtime (no network,
no LLM, no telemetry).

- **`submit_solve_job`** — admit a solve as a background job. Takes the same
  inline surface as `solve_minizinc_model` (`model`, optional `data`/`checker`,
  `solver`, `timeout_ms`, and the `free_search` / `parallel` / `random_seed` /
  `all_solutions` / `num_solutions` controls). Argument errors (empty model,
  non-positive timeout, a bad `parallel`/`num_solutions`) are reported
  synchronously **before any job exists**. Returns a `SolveJobStatus` with a
  server-generated opaque `job_id` and an initial `state` of `"queued"` or
  `"running"`. Admission is **bounded**: at most a fixed number of jobs run at
  once, further submits sit `"queued"` up to a fixed cap, and a submit beyond
  that is **rejected with an MCP error** (retry once a running job finishes)
  rather than growing the queue unboundedly.
- **`get_solve_job`** — poll a job by `job_id`. This is the OS-independent way
  to watch a background solve — no `ps`/`Get-Process` needed. Returns the
  `SolveJobStatus`: `state` (`"queued"`, `"running"`, `"succeeded"`,
  `"failed"`, `"timeout"`, `"cancelled"`), `timeout_ms` (the requested solve
  time-limit, echoed in every state), timing fields, an optional `result` (the
  full `SolveResult`), and an optional `message`. **State contract:** `result`
  is present exactly when `state` is `"succeeded"` or `"timeout"`, so
  `state == "failed"` **iff** `result is None`. `"failed"` means the job
  machinery itself raised (see `message`); a *solver*-level `error` verdict is a
  `"succeeded"` job whose `result.status == "error"`, **not** `"failed"`. A
  `"timeout"` job still carries its partial `SolveResult`. While a job is
  `"running"`, only `state` and `elapsed_ms` advance — live mid-solve
  statistics are not provided, so **pace your polling against the job's own
  budget** (`remaining ≈ timeout_ms - elapsed_ms`, usually terminal shortly
  after that) rather than a fixed `sleep`: tight loops just burn calls since a
  `running` job exposes no new data between polls. A completed `"succeeded"` or
  `"timeout"` job is the only place a background solve's statistics surface —
  its `result.statistics` carries the same model-visible `Statistics:` section
  the synchronous solve tools produce.
- **`cancel_solve_job`** — request cancellation by `job_id`. A still-`queued`
  job is dropped before it starts; a `running` job has its managed MiniZinc
  **process tree** (solver children included) terminated. Cancellation is
  best-effort and idempotent: cancelling an already-terminal job is a no-op.
  The job reaches `"cancelled"` (with `result is None`); poll `get_solve_job`
  to confirm.
- **`list_solve_jobs`** — list the currently retained jobs, one
  `SolveJobStatus` per job. Finished jobs are retained only up to a cap, so the
  oldest terminal jobs may have been evicted.

These four tools return at once, so — unlike the blocking solve/check/inspect
tools — they emit no progress/log status notifications; watch a job's `state`
via `get_solve_job` instead. An unknown `job_id` is an MCP error.

### Solver portfolios

Race several **model formulations**, solvers, and seeds against **one** instance
and return the single winner. This is a **local race** over the same
managed-runtime background-solve machinery — there is no remote/distributed
solving, upload, or telemetry; every attempt runs on this machine. Reach for it on
a hard instance where the best formulation or solver is unknown; an ordinary
single-solver `solve_minizinc_model` is still the right first attempt.

Because a hard race can run past a client's synchronous request timeout, a
portfolio runs as a **background job**: submit it with
[`submit_portfolio_job`](#background-portfolio-jobs) and poll
[`get_portfolio_job`](#background-portfolio-jobs) for the winner. It takes the same
inline surface as `solve_minizinc_model` — optional shared `data`/`checker`, and
the non-seed controls `free_search` / `parallel` / `all_solutions` /
`num_solutions`, applied identically to every attempt — but takes a non-empty
**`models`** list (alternative encodings of the same instance, sharing the one
`data`/`checker`; **not** a batch solve of different problems) and a non-empty
`solvers` list instead of one `model`/`solver`, and **does not** take
`random_seed`. The portfolio API still exposes named controls only: there is no
generic `solver_options`, `extra_args`, or raw MiniZinc flag passthrough.

- **Seeds.** `seed_count` (default `1`) generates seeds deterministically: with
  `seed_count == 1` each `(model, solver)` runs once **unseeded**; with
  `seed_count > 1` each runs with seeds `1..seed_count`, so every selected solver
  must support `-r`. Use `seeds` for exact user-controlled values instead:
  `seeds=[42, 123, 999]` runs exactly those seeds, in that order, with no extra
  unseeded attempt. An explicit `seeds` list must be non-empty, must not contain
  duplicates, requires `seed_count` to stay at its default `1`, and still requires
  every selected solver to support `-r`.
- **Cross-product, no cap.** The plan is the full cross-product
  `len(models) * len(solvers) * seed_count` when using the shorthand, or
  `len(models) * len(solvers) * len(seeds)` when `seeds` is supplied, with the
  **model index varying fastest** so the first attempts span distinct formulations.
  There is **no portfolio-side cap**: every attempt is admitted; up to `max_running_jobs`
  (default `4`) race simultaneously and the rest **queue**, starting as running
  slots free, and a decisive running winner cancels the still-queued attempts
  before they start. The only breadth bound is the registry's running+queued
  capacity — a plan past it is rejected by the job registry (raise capacity via
  the [registry-bound env vars](#configuring-registry-bounds)). Unsupported
  `-a/-f/-p/-r` controls are rejected up front too (canonical-id match, like the
  single-solve gate). Mind plan size: the cross-product grows fast.
- **Winner policy.** The first attempt to reach a decisive verdict
  (`optimal`/`satisfied`/`unsatisfiable`/`unbounded`/`unsat_or_unbounded`) wins
  and the remaining attempts are **cancelled**; if none is decisive, the best
  available terminal attempt is returned (a timeout/error *with* a solution,
  then `unknown`, then a timeout without a solution, then an error).
- **Result.** A `PortfolioSolveResult`: `status` (`"winner"`/`"no_winner"`),
  `winner_index`, the winning `SolveResult` in `winner` (its own `status` tells
  you whether the win was decisive), `attempts` (every attempt's `model_index`,
  solver, seed, final state, result status, objective, `checker_status`, and
  message — including the cancelled losers, so you need not poll child jobs),
  `elapsed_ms`, and `selection_policy`. The winning formulation is
  `models[attempts[winner_index].model_index]`. Present it like a single
  `solve_minizinc_model`: lead with the winner's model/solver/seed/status and then
  the winning solve.
  - **Provenance hashes.** `models_sha256` (one sha256 digest per formulation,
    index-aligned with `models`), `data_sha256` (sha256 of `data`, or `null`
    iff `data` was `None` — an empty-string `data` hashes distinctly from
    `null`), and `checker_sha256` (sha256 of `checker`, or `null` if none was
    supplied) content-bind the race to the exact formulations/data/checker it
    ran. `solve_controls` records the shared search configuration
    (`free_search`/`parallel`/`all_solutions`/`num_solutions`) every attempt
    ran with, captured at admission time like the hashes. Pass this whole
    `PortfolioSolveResult` as `portfolio_result` to
    `save_verified_minizinc_model` (below) to persist the race's full attempt
    table alongside a saved model.

### Background portfolio jobs

Portfolios run as background jobs — the portfolio analogue of
`submit_solve_job`/`get_solve_job`: submit the race and return immediately, then
poll for the winner, so a hard race never blocks past a client's synchronous
request timeout.

The design is **collect-on-poll**: there is no extra worker pool. The attempts
are admitted as ordinary jobs on the **same** solve registry as
`submit_solve_job` (so they count against its capacity and also show up in
`list_solve_jobs`), and winner-selection — the pure function of the attempts'
statuses — runs **when you call `get_portfolio_job`**. That keeps submit
non-blocking without cloning the job machinery.

- **`submit_portfolio_job`** — admit a portfolio race as a background job. Takes
  `models`, `solvers`, optional shared `data`/`checker`, `seed_count`, `seeds`,
  `per_attempt_timeout_ms`, and the non-seed controls (see
  [Solver portfolios](#solver-portfolios) above). Validation, capability
  enforcement, and admission run
  **synchronously**: an empty `models`/`solvers`, a bad control, an unsupported
  `-a/-f/-p/-r` flag, or a plan past the registry's running+queued capacity is
  reported at once as an MCP error, **before any job exists**. Returns a
  `PortfolioJobStatus` with an opaque `job_id` and `state` `"running"`.
- **`get_portfolio_job`** — poll a portfolio job by `job_id`. **Each poll drives
  the race**: once an attempt reaches a decisive verdict it selects the winner
  and cancels the still-running losers, so poll until terminal rather than
  submitting and walking away. Returns a `PortfolioJobStatus`: `state`
  (`"running"`, `"succeeded"`, `"cancelled"`), `per_attempt_timeout_ms`, timing
  fields, an optional `result` (the full `PortfolioSolveResult`), and an optional
  `message`. **State contract:** `result` is present exactly when `state` is
  `"succeeded"`. A race with no decisive winner is still `"succeeded"` (carrying a
  `"no_winner"` `PortfolioSolveResult`); a per-attempt failure is recorded in that
  result's attempts table, not as a failed job. Pace polling against
  `per_attempt_timeout_ms` rather than a fixed `sleep`.
- **`cancel_portfolio_job`** — stop a running race and **every** still-running
  attempt (each attempt's managed process tree is terminated). Best-effort and
  idempotent; the job reaches `"cancelled"` (with `result is None`).
- **`list_portfolio_jobs`** — list the retained portfolio jobs, one
  `PortfolioJobStatus` each. Finished jobs are retained only up to a cap.

Loser attempts are cancelled at the next poll (not the instant a winner appears),
bounded by each attempt's own `per_attempt_timeout_ms` — negligible for a polling
client, and the trade for not running a second worker pool. Like the other job
tools these return at once and emit no progress notifications; watch `state` via
`get_portfolio_job`. An unknown `job_id` is an MCP error.

### Configuring registry bounds

Background solve jobs (`submit_solve_job`) and a background portfolio job's
attempts share one in-process **job registry** with three bounds. They default to the values below and are overridable via environment
variables read **once at server start**:

| Env var | Meaning | Default | Minimum |
| --- | --- | --- | --- |
| `OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS` | Solves running concurrently | `4` | `1` |
| `OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS` | Submissions queued past the running cap | `16` | `0` |
| `OPENCONSTRAINT_MCP_MAX_RETAINED_TERMINAL` | Finished jobs kept for status polling | `64` | `1` |

A submission (or portfolio batch) beyond the `running + queued` capacity is
rejected with a clear error. An **invalid** value — non-integer or below the
variable's minimum — **fails fast at server start, naming the offending variable**
(no silent fallback to the default). Raise `OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS` /
`OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS` to admit wider portfolios.

### Path-based file tools

The four tools above take the model (and optional data) as **inline source
text**, which the server writes to a private temp file. That is ideal for the
small/medium models a client LLM drafts, but it forces the agent to read an
entire `.mzn`/`.dzn` from disk and thread the whole contents through MCP
arguments. For large local models the server also exposes **path-based**
siblings that read the model/data from local file paths instead:

- **`check_minizinc_files`** — path-based sibling of `check_minizinc_model`.
- **`inspect_minizinc_files`** — path-based sibling of `inspect_minizinc_model`.
- **`solve_minizinc_files`** — path-based sibling of `solve_minizinc_model`.
- **`find_unsat_core_files`** — path-based sibling of `find_unsat_core`.

Each returns the **same** result shape as its inline counterpart
(`CheckResult` / `ModelInspectionResult` / `SolveResult` / `UnsatCoreResult`).
A path-based inspection is the one that genuinely benefits from running in the
model's own directory: the interface parses without data, but a relative
`include` must still resolve from the model's own dir. The inline tools are
unchanged and remain the right choice for ephemeral, isolated text workflows.

**Arguments** (all four):

- `model_path: str` — path to a local `.mzn` file on the machine running the
  server. Required; must exist and be a regular file.
- `data_path: str | None = None` — path to a local `.dzn` file, or `null`. An
  empty data file is allowed (a valid "no parameters" input).
- `checker_path: str | None = None` — `solve_minizinc_files` only. Optional
  path to a MiniZinc checker whose filename must end in `.mzc` or `.mzc.mzn`;
  it is resolved to absolute and validated before any run.
- `solver: str = "cp-sat"` — `solve`/`check`/`inspect` only (not
  `find_unsat_core_files`, which always uses findMUS).
- `timeout_ms: int = 30000` — same semantics as the inline tools; must be
  strictly positive.

`solve_minizinc_files` additionally accepts the same optional, solve-only
search controls as `solve_minizinc_model` — `free_search`, `parallel`,
`random_seed`, `all_solutions`, and the solver-gated, satisfaction-only
`num_solutions` (see above for semantics and defaults) — plus `checker_path`
for solution checking.

**Includes (MiniZinc CLI style).** The file tools run the managed binary on the
real `model_path` with the working directory set to the model's own directory,
exactly like running `minizinc` by hand. A **relative** include such as
`include "helpers.mzn";` therefore resolves against the model's directory, and
**standard-library** includes (`globals.mzn`, `alldifferent.mzn`, etc.) resolve
from the solver's library path. (The inline tools, by contrast, run the inline
source in a private temp dir, so relative local includes do not resolve
there — which is why the file tools exist.)

**Path validation.** Before any subprocess, each tool resolves `model_path` and
`data_path` to **absolute** paths (`Path.resolve()`, following symlinks the
caller named) and rejects, as a clear MCP error naming the offending path: a
missing or non-file `model_path`/`data_path`, an empty/whitespace-only model
file, and a non-UTF-8 model file. Relative inputs resolve against the server
process's working directory, which in MCP stdio is wherever the client launched
the server — **prefer absolute paths** to avoid surprises.

**Read scope.** A file tool reads the model file, the optional data file, and
any local files they reference through MiniZinc `include`. It does **not** write
files, make network calls, upload data, or use a remote solver, and solving
still goes through the managed runtime. The threat model is "a local user
pointing the tool at their own files": the tool reads nothing the user could not
read by hand.

**`find_unsat_core_files` core caveat.** As with the inline `find_unsat_core`,
the structured `core` is **best-effort** and `stdout` is **authoritative**. The
`core` resolves spans from the **entry model file only**: a MUS member that
lives in an *included* file appears in `stdout` but not in `core`. The
entry-file filter matches on **basename**, so an included file that shares the
entry model's basename in a different directory could have its spans
mis-attributed to the entry model — a documented limitation of the best-effort
core (raw `stdout` stays authoritative).

[License](#license) · 

### Progress and status notifications

The nine long-running tools (`check_minizinc_model` / `check_minizinc_files`,
`inspect_minizinc_model` / `inspect_minizinc_files`, `solve_minizinc_model` /
`solve_minizinc_files`, `find_unsat_core` / `find_unsat_core_files`, and
`save_verified_minizinc_model`) emit
status feedback while MiniZinc is running, on two MCP channels:

- **Progress notifications** (`notifications/progress`) are sent only when the
  client requests them by including `_meta.progressToken` in the tool-call
  request. Values are small increasing stage counters (`1` validating, `2`
  solver running, `3` parsing, `4` complete) with a short message; `total` is
  deliberately omitted. They are **status updates, not a solver completion
  percentage** — MiniZinc/CP-SAT expose no reliable cross-solver progress
  signal, so render them as a spinner, stepper, or status text, never as a
  determinate percent bar.
- **Log notifications** (`notifications/message`, level `info`) carry the same
  milestone messages, but delivery depends on the protocol version the client
  negotiates. On a handshake-era session (`2025-11-25` and earlier) they are
  sent for every request, no token required — so clients that surface MCP
  server logs always show activity state. On `2026-07-28` and later the
  logging capability became a per-request opt-in (SEP-2577): the server still
  emits every milestone, but the SDK drops it unless that request's `_meta`
  asked for `info`-level logs. **Treat this channel as best-effort** — a client
  that wants guaranteed activity feedback should send a `progressToken` and
  read the progress channel, which is unaffected on both.

The MiniZinc subprocess runs in a worker thread, so both channels are
delivered while the solve is still in flight and the server stays responsive
to other requests during long runs. Both channels are local protocol messages
to the connected client; nothing changes in any tool's input schema, output
schema, or result semantics, and a client that supports neither channel simply
sees the final result as before.

## CP-SAT Python execution path

In addition to the MiniZinc declarative path, `openconstraint-mcp` exposes a
second solving path: the client's LLM writes OR-Tools CP-SAT Python, and the
server runs it in a **local child process**.

### Four separate steps

These are commonly conflated; keeping them apart is what makes a generated
script verifiable:

1. **Source-file creation.** The MCP client writes (and repairs) the `.py`
   script. The server never generates, rewrites, or patches source.
2. **Result transport.** The running script prints a final JSON object as its
   last stdout line. `json.dumps` only serializes a Python object into a string
   that `print` sends to stdout — it creates no file and saves nothing.
3. **Checker verification.** A separate checker script grades that reported
   answer against the original instance.
4. **Optional managed saving.** Only when the user asks: `save_verified_cpsat_python`
   re-verifies and writes a manifest-tracked artifact directory.

**The `solution` must be complete and in-band.** It carries every decision value
the checker needs, keyed so the checker can grade it — never prose, never
statistics alone, and never only a path to a result file the script wrote. A
supplementary `result_file` key is allowed as an extra (extra keys are ignored by
the executor), but it can never replace the in-band answer: the checker receives
the parsed `solution`, not your filesystem.

**Who guarantees what.** The client proposes; the server verifies. Prompt and
tool-description wording is advisory — the deterministic guarantee begins only
once the script is executed through an MCP tool. A client that writes a script
and never invokes an execution tool has no server guarantee at all. And an
`accepted` checker verdict proves only the properties that checker encodes: it
validates feasibility and consistency, and does **not** by itself prove a
`status="optimal"` claim is globally optimal.

### Recommended generated-script layout

The stdout JSON envelope (specified under **Tools** below) is **enforced** — the
executor parses it and rejects a malformed one. A script's internal layout is
**advisory**: it is what the CP-SAT prompts (`cpsat_python_solution_workflow`,
`solve_constraint_problem`, `auto_tune_constraint_problem`) and the full
profile's server instructions recommend to the client's LLM, not something the
server checks or every client receives.

For a newly generated one-off script, that recommendation is a single ordered
spine — `read_input`, `parse_input`, `solve`, `serialize_solution`,
`write_output`, called in that order by `main` — with a typed boundary across
`solve()`: a typed instance record in, a typed solution record out, rather than
loose dicts threaded from step to step. Only `serialize_solution()` maps that
record onto the stdout envelope.

The child process runs with **no stdin**, so a generated script must never call
`input()` or read `sys.stdin`: it gets an immediate EOF, prints no envelope, and
the whole run comes back as an error. Embed the instance in the script for
inline `run_cpsat_python`, or pass a data file through
`run_cpsat_python_file(script_path=…, args=[…])`, which runs the script from its
own directory.

This is the recommendation for a script generated fresh for one problem. Three
of the shipped `examples/` directories follow it internally: `examples/job_shop/`,
`examples/flexible_job_shop/` (with the six self-contained `model*.py`
formulations described below), and `examples/online_printing_shop/` (whose
`models.py` also owns its typed OPS data contract). They follow the same
`read_input`/`parse_input`/`solve`/`serialize_solution`/`write_output` spine,
but `read_input()` resolves its instance from `sys.argv` (the ARGV or
RELATIVE-FILE input mode `CPSAT_SCRIPT_INPUT_GUIDANCE` documents) rather than
hardcoding one — which is what keeps `args=["data_ft10.json"]`-style instance
switching working. `examples/nonogram/` and `examples/social_golfers/` predate
this spine and keep their original flat, single-function shape.

### Delivering several script variants

When the user asks for several working scripts (rather than "give me the best
one"), run them as one experiment and treat **every** attempt row as a
deliverable:

1. Generate each variant as its own file.
2. Execute them in a single `run_cpsat_python_experiment` call — inline `source`
   attempts, or `script_path` attempts for files already on disk — with one
   independent `checker`. Sharing one checker (and ranking attempts at all) is
   valid only when every attempt solves the same problem, the same instance, and
   the same objective under the same objective sense, emitting one shared
   `solution` schema.
3. Inspect **all** of `result["attempts"]`, not only `winner_index`. Repair each
   non-accepted script and re-run until every requested variant is accepted, or
   report plainly which one is still blocked.
4. Optionally save the finalist with `save_verified_cpsat_python`. A
   `script_path` attempt is marked `used_script_path` and can never be save
   provenance (that save re-runs inline source in a fresh temp directory), so
   re-run the finalist as an inline `source` attempt if you want to attach its
   `experiment_result`.

Selecting a single winner is the other, unchanged mode: rejected attempts may be
discarded without repair.

Checker-backed multi-script verification needs the full toolset —
`run_cpsat_python_experiment` and `save_verified_cpsat_python` are not in the
core profile, so start the server with `openconstraint-mcp stdio --toolset full`.

### Tools

- **`run_cpsat_python(source: str, script_timeout_ms: int = 30000)`** — execute
  LLM-generated OR-Tools CP-SAT Python source in a bounded child process and
  return a `CpsatPythonResult`. The script must emit a final JSON object as
  its last stdout line with all three **required** keys `status`, `objective`,
  and `solution`; it may also include an optional `best_objective_bound` for
  diagnostics:

  ```json
  {"status": "optimal", "objective": 42.0, "solution": {"x": 3, "y": 7}, "best_objective_bound": 42.0}
  ```

  Valid `status` values: `optimal`, `feasible`, `infeasible`, `unknown`,
  `error`. `objective` must be a finite number or `null` — a pure feasibility
  model still emits the key with `null`. `solution` must be a JSON object; `{}`
  is a well-typed envelope for "no incumbent" (it fails the *acceptance* gates,
  not the envelope check). The same finiteness rule holds one level down: every
  number **at any depth inside `solution`** must be finite, because `json.dumps`
  writes `NaN`/`Infinity` as bare tokens that are not valid JSON for a strict
  client. Extra keys are ignored.

  Same-shaped **intermediate** JSON objects are allowed and are what makes a
  timed-out run recoverable. Bound their cumulative bytes below the executor's
  combined 1 MiB stdout/stderr cap; the workflow prompt uses a 512 KiB budget,
  leaving room for the final object and stderr. Each intermediate object must
  fit that budget. Only the last complete object is read as the result.

  On a **clean exit**, a missing or invalid required key is rejected as
  `status="error"` with no solution and a `child_process_error` diagnostic whose
  `details.field` names the offending key — a key path such as
  `solution["tasks"][3]["start"]` when the offender is nested — so the client
  knows exactly what to repair. On a **timeout** the status stays `"timeout"`
  instead: the malformed partial is discarded rather than recovered, and the
  drop is reported through `rejected_partial_field` / `rejected_partial_reason`
  in the timeout diagnostic's `details`.
  Use the `cpsat_python_solution_workflow` prompt to generate conforming scripts.

  The child process runs under the server's own Python interpreter (the
  project venv, which already ships `ortools`), launched unbuffered (`-u`).
  Output beyond 1 MB is truncated and the child killed. Returns
  `CpsatPythonResult`: `status`, `solution`, `objective`, `best_objective_bound`,
  `stdout`, `stderr`, `return_code` (null on timeout), `timed_out`, `truncated`,
  `duration_ms`.

  `best_objective_bound` (OR-Tools' `solver.best_objective_bound` property) is
  optional and diagnostic only — never used for acceptance, winner selection,
  or save verification. It is `null` for a script that doesn't emit it
  (backward compatible) or reports a non-finite/non-numeric value, and it is
  most useful on `status="unknown"`, where `objective` is `null` but the
  solver may still have made bound progress.

  **Partial result on timeout.** A long or optimization run can print
  same-shaped intermediate JSON from a `cp_model.CpSolverSolutionCallback`, up
  to a fixed cumulative byte budget. Because the child is unbuffered, the last
  emitted block survives the timeout kill: on `status="timeout"` the server
  recovers it into `solution`/`objective`/`best_objective_bound` as an unproven
  incumbent (which may not be the latest one found), or leaves them null if none
  was printed in time. The same required-key check applies to that
  block: a malformed partial is not recovered as an incumbent, and the run keeps
  its `timeout` status and timeout diagnostic rather than becoming a contract
  error. The rejection is still reported — the timeout diagnostic's `details`
  carry `rejected_partial_field` and `rejected_partial_reason` naming the
  offending key, so a client can repair its progress block; both keys are absent
  when no JSON block was printed at all. On a clean run the final block (printed
  after `Solve` returns) is the authoritative result.

- **`run_cpsat_python_file(script_path: str, script_timeout_ms: int = 30000, args:
  list[str] | None = None, seed: int | None = None, config: dict | None =
  None)`** — path-based sibling of
  `run_cpsat_python`. Pass a local `.py` path instead of pasting the source, so
  iterating on a file does not mean re-copying it on every call. The script
  runs with its working directory set to the file's own directory, so a
  relative `open()` of a sibling data file or `import` of a helper module
  resolves (mirroring `solve_minizinc_files`). `script_path` is resolved to
  absolute and validated before any run — a missing path, a non-file, an
  empty/whitespace-only script, or non-UTF-8 content is rejected with a clear
  error and nothing runs. Same JSON output contract, output cap, timeout,
  tree-kill, and `CpsatPythonResult` shape (including timeout partial
  recovery) as `run_cpsat_python`.

  `args` is appended after the script path, so the script reads it as
  `sys.argv[1:]`. This is what lets a script that takes its data file on the
  command line be pointed at a different instance without editing its source:
  `examples/job_shop/model.py` reads `sys.argv[1]` and otherwise falls back to
  `data_ft06.json`, so `run_cpsat_python_file(script_path=".../job_shop/model.py",
  args=["data_ft10.json"])` is the tool-level equivalent of `python model.py
  data_ft10.json`. Omitting `args` runs the script with no arguments, exactly
  as before. `args` is a flag/path list, not a data channel: it is rejected
  before any child is spawned if an entry contains a NUL or the list encodes to
  more than 32 KiB total, both of which otherwise fail at spawn time — the NUL
  as a `ValueError` from `subprocess`, the oversized argv as an OS refusal. Pass
  bulk input in a file the script opens.

  `seed` and `config` are REPLAY inputs for re-running a saved seeded/
  configured artifact through this file tool instead of exporting environment
  variables by hand — the same two cooperative, opt-in protocols as
  `save_verified_cpsat_python`'s `seed`/`config`: `seed` sets
  `OPENCONSTRAINT_MCP_CPSAT_SEED`, and a non-empty `config` is written to a temp
  file whose path is set as `OPENCONSTRAINT_MCP_CPSAT_CONFIG` (an empty `config`
  (`{}`) is identical to omitting it). When both are omitted, both protocol
  env vars are explicitly cleared for the child rather than left to inherit a
  stale value from the server's own launch environment — the same clearing
  rule `run_cpsat_python` applies unconditionally, since it has no
  `seed`/`config` parameters of its own. This tool runs the script and reports
  what it printed — to also verify the result, use
  `run_cpsat_python_file_checked` below.

- **`run_cpsat_python_file_checked(script_path: str, checker_path: str,
  script_timeout_ms: int = 30000, args: list[str] | None = None, problem: str | None =
  None, checker_timeout_ms: int | None = None, test_checker: bool = False,
  seed: int | None = None, config: dict | None = None)`** *(full profile only —
  start the server with
  `--toolset full`)* — `run_cpsat_python_file` plus a mandatory verification
  pass, in one synchronous call. Both `script_path` and the **required**
  `checker_path` are existing local files; each runs in **its own directory**
  (`cwd` = that file's parent), so a relative read of a sibling data or
  reference file resolves on both sides. Both paths are resolved and validated
  (exists / regular file / non-empty / UTF-8) **before anything runs**; a bad
  path is an error naming the offending parameter and no child process is
  spawned.

  **Checker protocol.** The server writes a temporary JSON payload and passes
  its absolute path as the checker's `sys.argv[1]`:

  ```json
  {"problem": "<str|null>", "solution": {"…": "…"},
   "objective": 1165, "solver_status": "optimal"}
  ```

  The checker must print, as its **final stdout line**, one JSON object:

  ```json
  {"status": "accepted", "errors": [], "details": {"num_jobs": 20}}
  ```

  `status` is `accepted`, `rejected`, or `error`. Anything else — a nonzero
  exit, truncated output, no final JSON line, or `accepted` with a non-empty
  `errors` list — is normalized to `error`.

  **`problem`** is the instance text (or JSON) the checker validates against.
  It is optional in the signature but **required in practice by a data-driven
  checker**, and it cannot be inferred from `args`: those name a data file
  relative to the script's directory, not the instance itself.
  `examples/job_shop/checker.py`, for instance, returns `rejected` without it.

  **`checker_timeout_ms`** defaults to `script_timeout_ms`. When `test_checker` is on,
  an omitted value is capped at the largest checker timeout that fits the
  synchronous wall-clock budget, but never derived below **2000 ms** — see the
  wall-clock section. `args`, `seed`, and `config` behave exactly as
  on `run_cpsat_python_file` and apply to the model child only — the checker is
  a verification step, never a replayed solve.

  Returns a **`CpsatPythonCheckedResult`**: every `CpsatPythonResult` field,
  plus `checker` (the checker report, whose `status` is the verdict),
  `checker_skipped_reason` (set *instead of* `checker` when the run produced no
  checkable incumbent — the two are mutually exclusive), `checker_timeout_ms`,
  and `checker_test` (the self-test report described below; `null` unless
  `test_checker` opted in). A checker that rejects, times out, crashes, or emits
  garbage **does not fail the call**: the model result always survives and the
  verdict is reported. The top-level `diagnostic` composes the run and baseline
  checker: a run timeout wins, else a failed checker overrides, else the run's
  own diagnostic. An `optimal` run the checker rejects surfaces a
  `checker_failed` diagnostic. A
  timed-out run **with** a recovered incumbent is still checked; one without it
  is skipped.

  **`test_checker` — mutation probe.** Opt-in, default `false`.
  Nothing in an `accepted` verdict distinguishes a real checker from
  `print('{"status": "accepted", "errors": []}')`. With `test_checker: true`,
  after — and *only* after — an `accepted` baseline verdict, the server re-runs
  **your checker** against four deterministic, domain-agnostic mutations of the
  solution: `objective_perturbed`, `element_dropped`, `element_duplicated`, and
  `numeric_field_perturbed`. The element mutations operate on the longest
  non-empty list among the solution's top-level values (ties broken by sorted
  key order); element type does not matter, since dropping or duplicating an
  entry never looks inside it — a list of objects, of bare integers, of rendered
  strings, or of nested lists is equally mutable. The numeric mutation bumps a
  number reachable from that list's first element (its first integer field if
  the element is an object, the element itself if it is an integer), falling
  back to a top-level integer when there is no list *or* its leading element
  yields no integer. If no integer exists anywhere, it flips the first boolean
  instead, so a flat boolean assignment such as `{"x1": true, "x2": false}`
  still produces an applied mutation. The result is reported in a new
  `checker_test` field:

  - `mutations` — one **compact** row per mutation with its `name`, plus
    exactly one of a `skipped_reason` when the mutation was never graded (it
    could not be produced, or its probe faulted mid-flight — one faulted row
    never discards the others' verdicts) and the verdict when it was: `status`,
    an `errors` prefix capped at 8 KiB of compact JSON (including an explicit
    truncation marker), and `duration_ms`. A mutation ran iff its row
    carries a `status`. Rows deliberately omit the mutant's raw
    `stdout`/`stderr`/`details`: four of those, each able to hold a MiB of
    checker output, would flood your client's context to say something
    `status` and the bounded errors prefix already say. The accepted baseline
    is not repeated here either — the top-level `checker` is the one full
    report returned, including its complete errors.
  - `rejected_count` — the checker graded the mutant and refused it. Evidence
    the checker is not vacuous.
  - `accepted_count` — the checker graded the mutant and swallowed it. This is
    the tolerated-corruption count.

  An `error`/`timeout` mutant reached no verdict and counts in neither field,
  same as a skipped mutation — so `rejected_count: 0, accepted_count: 0` alone
  cannot distinguish "the solution's shape offered nothing to corrupt" from
  "every mutant that ran errored out or timed out": a checker that *choked* on
  a corrupted payload is not a checker that *tolerated* it, and the two
  deserve opposite reactions, but both leave these counts at zero. Read
  `mutations` directly to tell them apart.

  **A rejection proves non-vacuity, not completeness.** It shows the checker can
  reject a payload — never that it grades every constraint. Like every probe
  here it is not an independent correctness proof, and a checker does not prove
  optimization optimality. Because these generic mutations are not
  known-invalid, a zero `rejected_count` over mutants that actually ran is
  still inconclusive and produces no top-level diagnostic: every mutated
  solution may still be feasible. Treat it as a prompt
  to test the checker separately with a problem-specific, known-invalid payload,
  not as a verdict about the checker. A non-`accepted`
  baseline leaves `checker_test` `null` — there is nothing to test the checker
  against. A fault while probing one mutation becomes that row's
  `skipped_reason` rather than escaping, so the run, its `accepted` verdict, and
  the other rows all survive; if building the mutations fails outright — a
  solution nested too deeply to copy, say — every row is reported skipped for
  that reason and the run still returns normally. The self-test is
  synchronous-only:
  `submit_cpsat_python_file_job` has no `test_checker`.

  **Wall clock.** This call is nominally
  `(script_timeout_ms + ~8 s) + (checker_timeout_ms + ~8 s)` — two sequential children,
  each plus the process-tree termination grace — plus one further checker child
  per applied mutation, `(applied mutations) × (checker_timeout_ms + ~8 s)`,
  whenever `test_checker` is on. At the 30 s defaults that is about **76.5 s**
  with it off. An explicitly requested 30 s checker timeout with all four
  mutations would project to about **229.5 s** and is rejected.

  **Only `test_checker` is gated.** With it on, the call limits the projected
  worst case to **120 s**, charging each child its timeout plus a conservative
  process-tree termination/poll overhead and assuming all four mutations apply.
  When `checker_timeout_ms` is omitted, the server uses the smaller of
  `script_timeout_ms` and the largest checker budget that fits. The 30 s model default
  therefore derives an **8100 ms** checker timeout and projects to exactly 120 s,
  so `test_checker: true` works without changing another argument. An explicit
  over-budget checker timeout is rejected before any child runs.

  The derived value shrinks as `script_timeout_ms` grows, and it becomes the **baseline**
  checker's budget as well as the mutants' — so it is floored at **2000 ms**
  rather than allowed to dwindle, and a `script_timeout_ms` that would force it lower
  (above about **60.5 s**) is rejected instead. Without that floor, opting into
  an informational probe could time out a baseline checker that would have been
  given the full `script_timeout_ms`, turning a clean run into a `checker_failed` one.
  The floor bounds only the *derived* cap: a deliberately short `script_timeout_ms`
  still yields a checker timeout that matches it, and an explicit
  `checker_timeout_ms` is honoured as given. The rejection message reports the
  model/checker budgets, child count, overhead, and total. The ceiling exists because the
  self-test is the only thing that turns one checker child into five *and* has
  no background-job equivalent, so an over-budget probe would have nowhere to
  go.

  Without `test_checker`, `script_timeout_ms` has **no upper bound** by design, because
  a caller must be able to ask for the solve time the problem needs; every child
  still runs under the executor's own cap with process-tree kill. Set your MCP
  client's tool timeout accordingly in your own client config — a 900 s cap
  (Codex's `tool_timeout_sec = 900`) leaves room for a long solve without
  wedging the client indefinitely. For a solve longer than a synchronous MCP
  call can hold, use `submit_cpsat_python_file_job`, which is path-native for
  both the *script* and (via `checker_path`) the *checker*, and is not bound by
  a synchronous timeout — a checker that reads a relative sibling file resolves
  it there too. What that tool does not offer is `test_checker`.

  **Posture.** The checker is a **second unsandboxed local child** with exactly
  the same posture as the model script: it is a correctness gate against an
  *incorrect* script, not a security boundary against a hostile one. The server
  wrapper makes no network calls; both children are arbitrary local Python.

- **`save_verified_cpsat_python(source, …)`** — re-run `source`
  and persist it only when all supplied save gates pass. Gates run in order
  and short-circuit on the first failure:

  1. **Reported gate** (always): `status` in `optimal`/`feasible` AND a
     non-empty `solution`. This is the minimum required to save.
  2. **Expectation gate** (optional): pass `expectation` with
     `objective_sense` (`"maximize"` or `"minimize"`) and a numeric
     `objective_threshold`. The server checks whether the re-run objective
     meets the threshold. **This is a quality gate, not a proof of global
     optimality** — a script may pass the threshold and still not be the
     theoretically best solution.
  3. **Checker gate** (optional): pass `checker` (a complete Python script
     as inline source) that independently validates the solution. The checker
     receives the payload JSON path as `sys.argv[1]`; the payload has keys
     `problem`, `solution`, `objective`, `solver_status`. It must print
     exactly one JSON object as its final stdout line:
     `{"status": "accepted"|"rejected"|"error", "errors": [...], "details": {...}}`.
     `accepted` with an empty `errors` list is the only passing verdict.
     `checker_timeout_ms` controls the checker's process timeout (defaults
     to `script_timeout_ms`). **The checker is not sandboxed** — generate only
     validation code (no network, no file mutations).

  `problem` is one text value — handed to the checker as `payload["problem"]`
  and persisted verbatim as `problem.txt`. A **data-driven checker** parses a
  machine-readable instance out of it, so callers often have a JSON object to
  send rather than prose. Every tool that takes `problem` accepts either: a
  JSON object or array is serialized to its canonical text form before it goes
  any further, and a string is passed through untouched (a string that already
  contains JSON is never unwrapped). Non-finite numbers (`NaN`, `±inf`) are
  rejected, because the checker payload is written as strict JSON. The
  published schema stays `string | null` — text is the canonical form; the
  object spelling is accepted so a correct instance is not rejected over how
  the call happened to be written.

  `target_dir` must be an explicit absolute local path; the server never
  opens a file dialog. It is required for a save but **not** for
  `verify_only=true`, which re-evaluates the gates and persists nothing —
  useful while iterating on a checker or an expectation, instead of writing
  to a throwaway directory. Verify-only runs the *same* solver child and the
  *same* gates in the *same* order; it skips only save-target validation and
  the persistent writes (`target_dir` and `overwrite` are ignored when
  supplied). A passing verify-only run returns `reason: null` with
  `saved: false`, `target_dir: null`, and no `files`; a failing one is
  identical to a failed save. Fixed filenames: `model.py` (always); `problem.txt`
  when `problem` is supplied; `checker.py` and `solution.json` when a checker
  is supplied; `.openconstraint-model.json` (always, the manifest). Overwrite
  is marker-gated (prior-save manifest required,
  `overwrite=true` set, no untracked files). Returns
  `SaveVerifiedPythonResult` with:
  - `saved: bool` — **persistence only, never the verdict**: true iff
    `reason` is null *and* something was written. A passing `verify_only`
    run reports `saved: false`. The verdict is `reason: null` plus the
    per-gate fields below
  - `verification_level: "none" | "reported" | "expectation" | "checked"` —
    the highest gate that passed
  - `reported_passed`, `expectation_passed` (bool or null), `checker`
    (`CpsatCheckerReport` or null) — per-gate outcomes
  - `target_dir`, `files`, and run details (`status`, `solution`,
    `objective`, `stdout`, `stderr`, `timed_out`, `truncated`, `duration_ms`)

  The manifest records only a scalar checker summary (status, error count,
  duration, timed_out, truncated) — no stdout/stderr/errors/details. It also
  records a top-level `backend` (`"cpsat_python"`) and, under `verification`,
  the save-time `script_timeout_ms` (always) and an explicit `checker_timeout_ms`
  (only when supplied) — enough to choose replay tooling and pace a checked
  replay without guessing.

  Pass `seed` (a non-bool integer in the CP-SAT `random_seed` signed-int32
  range) as a single-run replay aid: the re-run sets
  `OPENCONSTRAINT_MCP_CPSAT_SEED` so a cooperating script uses that seed, and the
  manifest records it as `verification.replay_seed`. The save gates are
  **unchanged** — a `timeout` result still fails the reported gate even with
  its seed replayed. The saved `model.py` is byte-for-byte the script and
  carries only its own seed fallback, so to reproduce a seeded save by hand
  you must set `OPENCONSTRAINT_MCP_CPSAT_SEED` to the recorded seed — or use
  `run_cpsat_python_file`'s `seed`/`config` parameters instead; see
  [Reproducing a saved CP-SAT artifact](#reproducing-a-saved-cp-sat-artifact).

### Explicit experiments

- **`run_cpsat_python_experiment(attempts, objective_sense=None, …)`** — run a list
  of **explicit attempts** and return the best accepted result plus the full
  attempt table. Each attempt is
  `{name, source | script_path, args, seed, config, script_timeout_ms}` and must set
  **exactly one** of `source` or `script_path` — both, or neither, is rejected:
  - `source` is a complete, independent inline script (the server never
    generates, diffs, or merges attempts — it only executes what the client
    supplies).
  - `script_path` is a local path to an existing UTF-8 Python script. It runs
    with `cwd` set to the script's own parent directory, exactly like
    `run_cpsat_python_file`, so a relative `open()` of a sibling data file
    resolves — several attempts can race existing on-disk scripts against
    shared data with nothing duplicated in the request. `args` is a list of
    strings appended after the path as the child's `sys.argv[1:]`; supplying
    it alongside `source` is **rejected**, not silently ignored.

  `name` defaults to
  `attempt-{index}` when omitted, and every resolved name (explicit or
  defaulted) must be unique. Every attempt — including each `script_path` — is
  validated **before any child runs**, so one bad path rejects the whole call
  rather than only its own attempt. `checker` and `problem` remain inline text
  for the whole experiment; this tool has no `checker_path`. `seed` and
  `config` are both **cooperative,
  opt-in** protocols, not server-enforced parameters:
  - `seed` sets `OPENCONSTRAINT_MCP_CPSAT_SEED`, identically to the save path's
    seeded replay.
  - `config` (a JSON object, `{}` treated identically to omitted) is written to
    a temp file and its path set as `OPENCONSTRAINT_MCP_CPSAT_CONFIG`; a
    cooperating script reads it and applies whichever fields it understands
    (e.g. `solver.parameters.num_workers`). The server never sets OR-Tools
    parameters itself.

  Attempts run through a bounded worker pool sized by `max_parallel_attempts`
  (default `1` = serial; capped at `min(server CPU count, 4)` and rejected
  above that). Coordinate it with each script's own
  `solver.parameters.num_workers` — oversubscribing the machine makes runs
  slower and less stable, not faster. When an attempt's `config` sets a
  `num_workers` key, the server checks `max_parallel_attempts * num_workers`
  against this machine's CPU count and adds a non-blocking advisory to the
  result's `warnings` list if it's exceeded — a best-effort heuristic limited
  to that one cooperative convention; it cannot see `num_workers` set any
  other way (e.g. hardcoded in the script). Results are always returned in
  **original attempt order**, and winner tie-breaks use that same order, never
  completion order.

  Acceptance is the same two ordered gates as the save path: base acceptance
  (`status` in `optimal`/`feasible`/`timeout`, non-empty `solution`, and in
  optimization mode only a finite numeric `objective`), then — only for
  base-eligible attempts — the optional checker gate (`checker`/
  `checker_timeout_ms`, same contract as `save_verified_cpsat_python`'s
  checker). In optimization mode (`objective_sense` is `"maximize"` or
  `"minimize"`), the winner is the accepted attempt with the best objective,
  ties broken by stronger status (`optimal` > `feasible` > `timeout`), then
  fastest `duration_ms`, then earliest attempt order. In feasibility mode
  (`objective_sense` omitted/null), objective is not required and winner
  selection uses stronger status, then fastest `duration_ms`, then earliest
  attempt order.

  The request is **synchronous and budget-gated**: it is rejected up front
  (before any child runs) when its projected wall-clock budget — batched by
  `max_parallel_attempts`, using each attempt's effective timeout, checker
  timeout when present, and a conservative per-child timeout/kill overhead —
  exceeds a fixed cap. Reduce attempt count/timeouts or raise
  `max_parallel_attempts` to fit.

  Returns `CpsatPythonExperimentResult`: `status` (`"winner"` or
  `"no_winner"`), `winner_index`/`winner_name`/`winner` (a full
  `CpsatPythonResult`, all present iff `"winner"`), `attempts` (every attempt,
  accepted or not, each with its resolved `name`, `source_sha256` (the inline
  text's hash, or the on-disk file's raw-byte hash — unnormalized either way),
  `config_sha256`, `used_script_path`, a diagnostic `best_objective_bound` (useful even for a
  rejected `"unknown"` attempt with no incumbent; never used for acceptance or
  winner selection), and — for a `status="error"` attempt — a bounded
  `stderr_tail` for debugging, in addition to the concise one-line `message`),
  `elapsed_ms`, `objective_sense` (or null for feasibility),
  `selection_policy`, `source_sha256` (index-aligned with `attempts`),
  `checker_sha256`, `problem_sha256`, `warnings` (non-blocking advisory
  strings: the `num_workers`-oversubscription check above when triggered,
  plus — whenever there is a winner — an unconditional reproducibility
  disclaimer; empty only when there is no winner and nothing else is
  flagged). A `timeout` winner is **reportable, not savable** —
  `save_verified_cpsat_python`'s reported gate still requires
  `optimal`/`feasible`.

  **Reproducibility:** an experiment winner reflects **one observed run**,
  not a guarantee. CP-SAT's randomized search, LNS, restarts, parallel
  portfolio search (`num_workers > 1`), and short time limits can all
  make a winner fail to reproduce its objective when
  `save_verified_cpsat_python` re-runs it fresh — this is expected solver
  behavior, not a bug, and is why the save path always re-verifies rather
  than trusting the experiment result. For stronger reproducibility, set
  explicit solver parameters such as `random_seed`, consider
  `num_workers = 1`, and verify with the same timeout — exact
  determinism is still not guaranteed.

  Pass `include_winner_stdout=False` to omit the winner's raw `stdout` from
  the returned result — `solution`/`objective` (the parsed, structured answer)
  are unaffected; for a well-behaved script `stdout` is a redundant raw-text
  copy of the same JSON. Defaults to `true` (today's behavior, `stdout`
  included).

  Pass the result as `experiment_result` to `save_verified_cpsat_python` (with
  the saved attempt's exact replay `config`, if any) to persist it with full
  provenance — see below. This works for the experiment's winner or any other
  accepted **inline-`source`** attempt you choose to save instead; a
  `script_path` attempt can win the race but can never supply save provenance
  (see below for why).

  Racing two shipped example formulations against the same benchmark instance,
  with no source duplicated into the request:

  ```json
  {
    "attempts": [
      {
        "name": "interval_nooverlap",
        "script_path": ".../examples/job_shop/model.py",
        "args": ["data_ft10.json"]
      },
      {
        "name": "pairwise_disjunctive",
        "script_path": ".../examples/job_shop/model_pairwise_disjunctive.py",
        "args": ["data_ft10.json"]
      }
    ],
    "objective_sense": "minimize",
    "default_script_timeout_ms": 20000
  }
  ```

  Both scripts read `data_ft10.json` from their own directory, so the shared
  benchmark instance stays on disk instead of being pasted into the request
  once per attempt — the duplication this option exists to remove.

  `examples/flexible_job_shop/` carries the same pattern one step further: six
  CP-SAT formulations of the *flexible* job shop problem (`model.py` canonical
  optional intervals, `model_direct_optional_intervals.py`,
  `model_pairwise_disjunctive.py`, `model_redundant_bounds.py`,
  `model_composite.py`, `model_earliest_start_branching.py`), each self-contained and taking
  `[data_file.json] [time_limit_seconds] [results_dir]` on the command line,
  with a shared `checker.py`. Every model prints its full result on stdout, and
  the printed `solution` CONTAINS the schedule rather than describing it,
  because the checked tools build the checker's payload from stdout — a summary
  that only pointed elsewhere would leave the checker nothing to grade. Writing
  that result to a file is **opt-in**: a model touches the disk only when the
  third argument names a directory (the committed runs used `results`), so
  solving through the MCP file tools never mutates the checkout on its own.
  Because the 600s runs needed for the
  larger instances exceed `run_cpsat_python_experiment`'s non-overridable
  210s wall-clock budget, those were driven with `submit_cpsat_python_file_job`
  instead, three at a time so the compared models see identical machine load.
  Such a run can carry `checker_path=examples/flexible_job_shop/checker.py`
  with `problem` set to the bare instance filename (`data_mk01.json`): the
  checker runs in its own directory, so it resolves the data file beside
  itself rather than needing the whole instance inlined into every submit.
  That bare filename is specific to the path-based checker run, though:
  `save_verified_cpsat_python` has no `checker_path` and runs its inline
  checker from a temp copy, so saving such a result later means inlining the
  instance again.
  Each model's docstring records its measured result; the short version is that
  no formulation wins outright. On mk01 all six prove the optimum of 40 in
  ~0.1s. On mk15 the plain optional-interval encoding holds the best incumbent
  (347, against `model_composite.py`'s 349 and `model_pairwise_disjunctive.py`'s
  381). At 60 machines the split is between bounds and incumbents: the
  machine-load inequality carried by `model_redundant_bounds.py` and
  `model_composite.py` is the only thing that lifts the lower bound off the
  trivial 77 (both reach 344), while a greedy dispatching heuristic's 427 still
  beats every formulation except `model_composite.py`, which combines the load
  bound with that same greedy schedule as a warm start and improves on it to
  418. `model_direct_optional_intervals.py` is a size-only ablation so far —
  measured on mk01 only, with its runtime question deliberately open.
  `model_earliest_start_branching.py` is the one *search-order* ablation: it
  forks the direct encoding and adds a decision strategy that repeatedly branches
  on the task able to start earliest and starts it there
  (`CHOOSE_LOWEST_MIN`/`SELECT_MIN_VALUE` on the starts — the non-delay
  dispatching rule), while leaving the
  model itself byte-identical in size. On mk15 it improves the incumbent at both
  budgets tested — 360 → 355 at 60s, and 345 → 339 at 1200s, where it also
  proves a stronger lower bound (333, against the baseline's 332). Neither run
  proves an optimum: closing mk15 needs the incumbent to come down to meet the
  bound, and 333 happens to be the optimum only per FJSPLib, not per the run. Its
  docstring records the configurations that lost: forcing `FIXED_SEARCH`
  collapses the 60s incumbent to 570 because it disables the LP- and
  pseudo-cost-guided branching, and a strategy over the presence literals is
  inert because `AUTOMATIC_SEARCH` leaves the booleans to the SAT heuristic.

#### Persisting an attempt from an experiment

`save_verified_cpsat_python` accepts two additional, optional arguments for
experiment provenance:

- **`config`** — the saved attempt's exact replay config (`{}`/omitted if it
  ran without one). Like `seed`, this is a replay aid: the re-run writes it to
  a temp file and sets `OPENCONSTRAINT_MCP_CPSAT_CONFIG`, then — on a
  successful save — persists it as `replay-config.json` alongside its sha256
  in the manifest.
- **`experiment_result`** — the `CpsatPythonExperimentResult` from
  `run_cpsat_python_experiment`. This is **provenance only, never verification
  evidence**: when supplied, it must be self-consistent with this save request
  — `status == "winner"` (i.e. the experiment produced at least one accepted
  attempt) and at least one **accepted** attempt in `experiment_result.attempts`
  whose `source_sha256` matches `source`, `seed` matches the supplied `seed`,
  and `config_sha256` matches the canonical hash of the supplied `config` —
  not necessarily the experiment's own `winner_index`; you can attach
  provenance for the winner or for any other accepted attempt you choose to
  save instead. A mismatch is **rejected before any child runs**; the fresh
  re-run and save gates below still decide everything.

  A matching attempt that ran from `script_path` (`used_script_path: true`)
  **does not qualify**. This save's re-run is always inline `source` with a
  fresh temp-directory `cwd`, so it can replay neither that attempt's `args`
  nor its `cwd`-relative sibling data — and `source_sha256`, which hashes
  script content only, cannot even distinguish the same script run against
  two different data files. At least one matching attempt must therefore be an
  inline-`source` one; the save is rejected when **every** match is
  `script_path`-derived, and the order attempts appear in never matters. To
  keep provenance for a formulation you raced from disk, re-run that script as
  an inline `source` attempt (or save it without `experiment_result`).

  On a successful save,
  the full attempt table is written as `experiment-log.json` — a
  **provenance summary**, not an archive: every attempt row carries only
  hashes and scalar outcomes (`index`, `name`, `seed`, `source_sha256`,
  `config_sha256`, `used_script_path`, `script_timeout_ms`, `status`, `objective`,
  `accepted`, `checker_status`, `message`, `timed_out`, `truncated`,
  `duration_ms`).
  **Non-saved attempts' full `config` objects are never persisted** — only
  the saved attempt's own config is, via `replay-config.json`.

  Saved seed/config provenance **improves replayability but does not
  guarantee bit-for-bit reproducibility** — CP-SAT randomness, parallel
  search, solver version changes, and script-level nondeterminism can still
  produce a different incumbent; the fresh save-time verification run is
  always the authority.

### Reproducing a saved CP-SAT artifact

There is no dedicated inspect/rerun tool: a saved directory is a plain local
folder, and its manifest is a JSON file a client can read directly.

1. Read `.openconstraint-model.json` in the saved directory. It names the
   `backend` (`"cpsat_python"`), and — under `verification` — `script_timeout_ms`,
   `replay_seed` when the save was seeded, `replay_config_sha256` when it was
   configured, and `checker_timeout_ms` when one was explicitly supplied.
2. Call `run_cpsat_python_file` with `script_path` pointing at the saved
   `model.py`, `script_timeout_ms` from the manifest, `seed` from
   `verification.replay_seed` when present, and — when a `replay-config.json`
   sibling file exists — its parsed JSON contents as `config`. No manual
   environment variables are needed; the tool builds the
   `OPENCONSTRAINT_MCP_CPSAT_SEED`/`OPENCONSTRAINT_MCP_CPSAT_CONFIG` overlay for
   you.
3. Compare the returned `CpsatPythonResult` against the manifest's
   `verification.reported_status`/`objective` and the saved `solution.json`
   (when the save included a checker).

**Replaying the checker too.** `run_cpsat_python_file` runs the script only, so
step 2 re-verifies a `checked`-level save at the `reported` level. To re-run the
saved checker in the same call, use `run_cpsat_python_file_checked`
(full profile) with `script_path` = the saved `model.py`, `checker_path` = the
saved `checker.py`, `problem` = the verbatim contents of `problem.txt`,
`checker_timeout_ms` from `verification.checker_timeout_ms`, plus the same
`script_timeout_ms`/`seed`/`config`.

For full **gate** replay — every original gate, including the objective
`expectation` that `run_cpsat_python_file_checked` does not evaluate — call
`save_verified_cpsat_python` again with `verify_only=true`. That mode re-runs
every original gate and needs no `target_dir` at all — and ignores one if you
pass it, so to persist the replay itself, omit `verify_only` (or pass
`verify_only=false`) and supply a real `target_dir`. Along with it, pass the
saved source (read from `model.py`), checker (read from `checker.py`), `seed`,
`config`, and — whenever the saved directory or manifest has them — the
original `problem` (read verbatim from `problem.txt`), `expectation`
(rebuilt from `verification.expectation.objective_sense` /
`objective_threshold`), and `script_timeout_ms` (from `verification.script_timeout_ms`).
Omitting any of these replays something different from the original:
`problem` is passed straight through to the checker payload, so a
**data-driven checker** — one that parses its instance out of
`payload["problem"]` rather than hardcoding it, the shape the
`cpsat_python_solution_workflow` prompt asks for — validates against
different input, or returns `error` outright, when it is dropped or
reworded; `expectation` is a gate that runs and can fail *before* the
checker ever does, so leaving it out silently skips the objective-threshold
check; and `script_timeout_ms` is the re-run budget (and the checker's timeout too,
unless `checker_timeout_ms` was set explicitly) — a different value is a
looser or stricter re-run, not a weaker one. This is not a
new tool; it is the same save path already documented above, applied to a
saved artifact's own inputs.

### Background CP-SAT jobs

For long-running CP-SAT solves (`script_timeout_ms` of minutes), the synchronous
`run_cpsat_python` / `run_cpsat_python_file` tools will block past most MCP
client per-call timeouts. Use the background-job surface instead — the
CP-SAT analogue of the MiniZinc `submit_solve_job` / `get_solve_job` pair:

- **`submit_cpsat_python_job(source: str, script_timeout_ms: int = 30000, problem:
  str | None = None, checker: str | None = None, checker_timeout_ms: int |
  None = None)`** — submit inline OR-Tools CP-SAT Python source as a
  background job. Returns a `CpsatPythonJobStatus` with an opaque `job_id` and
  an initial `state` of `"queued"` or `"running"` (a very fast job may already
  be terminal). The same output contract as `run_cpsat_python` applies.
  `problem` / `checker` / `checker_timeout_ms` attach the same optional
  problem-specific checker as `save_verified_cpsat_python`'s checker gate —
  see the checked-jobs note below.
- **`submit_cpsat_python_file_job(script_path: str, script_timeout_ms: int = 30000,
  args: list[str] | None = None, problem: str | None = None, checker: str |
  None = None, checker_path: str | None = None, checker_timeout_ms: int | None
  = None)`** — submit a local
  script file as a background job. The
  path is validated before admission (missing / non-file / empty / non-UTF-8 →
  MCP error, no job created). The script runs in its own directory so relative
  imports and data-file opens resolve. `args` becomes the script's
  `sys.argv[1:]`, as in `run_cpsat_python_file`, and is recorded at admission,
  so a job that waits in the queue still runs the values supplied on submit.
  Takes the same optional checker inputs as `submit_cpsat_python_job`, plus
  `checker_path` — a local path to an **on-disk** checker, the path-based
  counterpart of the inline `checker` string. The two are **mutually
  exclusive**: pass at most one, and supplying both is rejected at admission
  with no job created. A `checker_path` checker runs **in place**, with its
  working directory set to its own parent directory (as
  `run_cpsat_python_file_checked` does), so a checker that opens a relative
  sibling file finds it — which means `problem` can be a bare data filename
  next to the checker instead of a large instance inlined into every submit.
  It is validated and resolved at admission exactly like `script_path`, so a
  job that waits in the queue still runs the file named on submit; a checker
  file deleted before the checker phase runs surfaces as a `status="error"`
  checker report on the finished job, not a failed job.
- **`get_cpsat_python_job(job_id: str)`** — poll a job by `job_id` (works
  for both inline and file submits). Returns a `CpsatPythonJobStatus`: `state`
  (`"queued"`, `"running"`, `"succeeded"`, `"failed"`, `"timeout"`,
  `"cancelled"`), timing fields, an optional `result` (the full
  `CpsatPythonResult`), an optional `message`, and — for a checked job — the
  checker outcome fields described below. **State contract:** `result`
  is present exactly when `state` is `"succeeded"` or `"timeout"`; absent for
  all other states. A script-level error (`status="error"`) is a `"succeeded"`
  job (the child ran and produced a result); `"failed"` means the job machinery
  raised before any result was produced. A `"timeout"` job carries its partial
  `CpsatPythonResult` (`timed_out=True`, best-so-far `solution`/`objective`).
  Pace polling against `script_timeout_ms - elapsed_ms` (plus `checker_timeout_ms`
  for a checked job).
- **`cancel_cpsat_python_job(job_id: str)`** — terminate a running job's child
  process tree (the solver child, or the checker child if the job is in its
  checker phase). Best-effort and idempotent; the job reaches `"cancelled"`
  (with `result is None` — cancelling during the checker phase discards the
  already-completed solver result).
- **`list_cpsat_python_jobs()`** — list the retained CP-SAT jobs, one
  `CpsatPythonJobStatus` each. Both inline-source and file-based jobs appear.

#### Checked background jobs (diagnostic only)

Submitting a job with `checker` (a Python checker script source string, same
protocol as `save_verified_cpsat_python`'s checker gate) — or, for
`submit_cpsat_python_file_job`, with `checker_path` (the same protocol, but an
on-disk checker run in its own directory) — runs the checker as a
second bounded child after the solver child finishes — but only when the
result carries a usable incumbent (`status` of `optimal`, `feasible`, or
`timeout` with a non-empty `solution`). While the checker runs, the job stays
`"running"`: `script_timeout_ms` caps the solver child only, and the job status
echoes the effective `checker_timeout_ms` (the supplied value, else
`script_timeout_ms`) so a polling client can pace the checker phase too.

> **Note:** `examples/` is no longer tracked in this repository (see git
> history for the last tracked snapshot); the `open(...)` calls below are
> illustrative of the shape of the call, not a runnable snippet on a clean
> checkout.

```python
# Submit returns immediately with a job_id; poll until a terminal state,
# then read the diagnostic checker verdict off the job status.
job = await mcp.call_tool(
    "submit_cpsat_python_job",
    {
        "source": open("examples/cpsat_python/graph_coloring.py").read(),
        "checker": open("examples/cpsat_python/graph_coloring_checker.py").read(),
    },
)
status = await mcp.call_tool("get_cpsat_python_job", {"job_id": job["job_id"]})
# Poll get_cpsat_python_job until status["state"] is a terminal state, then:
# status["checker"]["status"] == "accepted" iff the checker accepted the solution
```

On a result-bearing terminal state the job status carries at most one of:

- `checker` — the `CpsatCheckerReport` (`accepted` / `rejected` / `error` /
  `timeout`). A checker infrastructure fault becomes a `status="error"` report
  on the completed job; it never discards the solver result or fails the job.
- `checker_skipped_reason` — set when the supplied checker did not run (for
  example `status='infeasible'` or an empty solution).

The checker result is **diagnostic, not a save gate**: a checked `"timeout"`
job stays `"timeout"` and its recovered incumbent stays unsavable, and saving
always re-runs verification through `save_verified_cpsat_python`. Bad checker
arguments (`checker_timeout_ms` without a checker of either form, a
non-positive timeout, an empty checker, `checker` and `checker_path` together,
an invalid `checker_path`) are rejected before a job is admitted.

#### Configuring CP-SAT registry bounds

The CP-SAT job registry has its own three bounds, independently configurable
from the MiniZinc registry:

| Env var | Meaning | Default | Minimum |
| --- | --- | --- | --- |
| `OPENCONSTRAINT_MCP_CPSAT_MAX_RUNNING_JOBS` | CP-SAT jobs running concurrently | `4` | `1` |
| `OPENCONSTRAINT_MCP_CPSAT_MAX_QUEUED_JOBS` | Submissions queued past the running cap | `16` | `0` |
| `OPENCONSTRAINT_MCP_CPSAT_MAX_RETAINED_TERMINAL` | Finished jobs kept for status polling | `64` | `1` |

An invalid value — non-integer or below the minimum — **fails fast at server
start, naming the offending variable** (no silent fallback to the default).

### Security posture

**The server executes user-provided Python locally. It is not sandboxed.**
Timeout + output-cap + process-tree kill is a **robustness** boundary, not
a security sandbox. The child is also launched with its stdin closed
(`DEVNULL`) so a script that reads `input()`/`sys.stdin` gets an immediate
EOF instead of consuming the server's JSON-RPC stream when running over
stdio. There is no AST filtering, no network blocking, no import allowlist.
This tool is local-only; a cloud/multi-tenant deployment would require a
real sandbox. The **server wrapper** makes no network calls,
but the executed child process is arbitrary code.

### Example scripts

> **Note:** `examples/` is no longer tracked in this repository (see git
> history for the last tracked snapshot). The paths below describe the
> shape of the example scripts; real industrial examples will replace
> them here.

`examples/cpsat_python/` holds reference scripts with the canonical emit
snippet:

- **`examples/cpsat_python/assignment.py`** — 4 workers × 4 tasks, minimize total cost.
- **`examples/cpsat_python/scheduling.py`** — 3 tasks on a single machine, minimize makespan.
- **`examples/cpsat_python/graph_coloring.py`** — 3-color a 5-vertex graph
  (satisfaction problem, no objective). Pair with `graph_coloring_checker.py`
  to demonstrate the checker gate.
- **`examples/cpsat_python/graph_coloring_checker.py`** — standalone checker
  that reads the payload from `sys.argv[1]` and verifies no two adjacent
  vertices share the same color. Returns `{"status": "accepted", "errors": [],
  "details": {}}` on success or `"rejected"` with a per-edge error message.
  `tests/test_cpsat_python_examples.py` runs it both ways: a valid 3-coloring
  is `accepted`, and a plausible-looking coloring that is correct on five of
  the six edges but repeats a color across the wrap-around edge is
  `rejected` — a checker catching a wrong-but-plausible solver result.
- **`examples/cpsat_python/clinic_roster_checker.py`** — standalone checker
  demonstrating the checker protocol against a 7-day urgent-care nurse
  rostering instance. It covers shift coverage, night-shift skills, time off,
  rest after nights, and workload bounds, and independently recomputes the
  preference/fairness objective before accepting a solution. Its tests cover
  both a valid roster (`accepted`) and plausible-looking but invalid ones —
  an unqualified night-shift assignment and a missing night-then-day rest gap
  (`rejected`, with the specific violated rule in `errors`).
- **`examples/golomb_ruler/cpsat_python/`** — order-12 Golomb ruler saved at
  the `checked` verification level: both an `expectation` gate
  (`objective_threshold`) and a `checker` passed, and `problem.txt` records
  the exploratory `run_cpsat_python_experiment` comparison behind the saved
  formulation (see [Persisting an attempt from an
  experiment](#persisting-an-attempt-from-an-experiment)).
- **`examples/social_golfers/cpsat/`**, **`examples/social_golfers/cpsat_best/`**,
  and **`examples/social_golfers/cpsat_24/`** — CP-SAT saves for social-golfers
  boundary instances. `cpsat/` and `cpsat_best/` cover the same 7-3-10 instance
  (21 golfers, 7 groups of 3, 10 weeks) via a compact Fano-plane formulation.
  `cpsat/` is an earlier `feasible` incumbent saved with the reported gate
  only; `cpsat_best/` supersedes it with a `checked`-level save — a checker,
  a `replay-config.json` from a cooperative `OPENCONSTRAINT_MCP_CPSAT_CONFIG`
  sweep (see `RESULT.md` for the sweep table), and file-based background-job
  replay coverage in `tests/pyexec/test_jobs_integration.py`
  (`test_submit_file_with_real_checker_reaches_optimal_and_accepted`) — the
  `submit_cpsat_python_file_job` + checker + saved-artifact workflow in one
  example. `cpsat_24/` is a reported-gate save for the 8-3-11 instance.

The `examples/cpsat_python/` scripts can be run standalone
(`python examples/cpsat_python/assignment.py`), and the first two are used as
integration-test anchors for `run_cpsat_python`. The clinic roster and graph
coloring checkers are exercised directly (independent of the specific CP-SAT
script that produced a solution) as standalone checker-protocol tests, each
covering an accepted and a rejected verdict — these tests need no `ortools`
solve or managed runtime and run in the default `just check`.
`run_cpsat_python_experiment`'s own integration test
(`tests/pyexec/test_experiment_integration.py`) is self-contained rather than
reusing the files above: a tiny two-variable optimization problem solved by
two distinct explicit source variants, a script that reads the
cooperative `OPENCONSTRAINT_MCP_CPSAT_CONFIG` protocol for real, and two
on-disk `script_path` attempts raced in parallel from two sibling
directories, each reading its own sibling data file — all fast and fully
deterministic.

#### Comparing explicit source variants

```python
# The client supplies every attempt's complete script; the server never
# generates, diffs, or merges them — it only executes, verifies, and picks
# the winner.
result = await mcp.call_tool(
    "run_cpsat_python_experiment",
    {
        "attempts": [
            {"name": "baseline", "source": open("model_v1.py").read()},
            {"name": "redundant_constraint", "source": open("model_v2.py").read()},
        ],
        "objective_sense": "minimize",
    },
)
# result["status"] == "winner" and result["winner_name"] name the best accepted
# attempt; result["attempts"] carries every attempt's status/objective/verdict.
```

When the variants already exist on disk, name them with `script_path` instead
of reading them in — each attempt then runs from its own script's directory,
so a shared sibling data file is read once per child rather than inlined once
per attempt:

```python
result = await mcp.call_tool(
    "run_cpsat_python_experiment",
    {
        "attempts": [
            {
                "name": "baseline",
                "script_path": "/abs/path/model_v1.py",
                "args": ["data_ft10.json"],
            },
            {
                "name": "pairwise",
                "script_path": "/abs/path/model_v2.py",
                "args": ["data_ft10.json"],
            },
        ],
        "objective_sense": "minimize",
    },
)
# These rows carry used_script_path=True, so neither can be attached as
# save_verified_cpsat_python provenance — see "Persisting an attempt from an
# experiment".
```

#### Satisfaction save with a checker

```python
# Pass the checker source directly; the server runs it in a child process
# and only commits when it returns accepted with an empty errors list.
checker_source = open("examples/cpsat_python/graph_coloring_checker.py").read()
result = await mcp.call_tool(
    "save_verified_cpsat_python",
    {
        "source": open("examples/cpsat_python/graph_coloring.py").read(),
        "target_dir": "/absolute/path/to/save-dir",
        "problem": "3-color a 5-vertex pentagon graph",
        "checker": checker_source,
    },
)
# result.verification_level == "checked" iff the checker accepted
```

#### Optimization save with an expectation threshold

```python
# Expectation gate: quality check, NOT a proof of global optimality.
# A script may pass this threshold and still not be the theoretically
# best solution — the server only verifies what the script reported.
result = await mcp.call_tool(
    "save_verified_cpsat_python",
    {
        "source": open("examples/cpsat_python/assignment.py").read(),
        "target_dir": "/absolute/path/to/save-dir",
        "expectation": {"objective_sense": "minimize", "objective_threshold": 5},
    },
)
# result.verification_level == "expectation" iff both reported and threshold gates passed
# result.expectation_passed == True means objective <= 5 (not that no lower cost exists)
```

### MiniZinc vs. CP-SAT Python

| | MiniZinc path | CP-SAT Python path |
|---|---|---|
| Input | Declarative model (`.mzn`) | Executable Python (`ortools`) |
| Execution | Managed MiniZinc runtime | Local child process |
| Install | `install-runtime` needed | Zero-install (ortools bundled) |
| Sandboxing | Runtime reads model, no exec | **Not sandboxed** |
| LLM fluency | High (MiniZinc is LLM-friendly) | High (Python is LLM-friendly) |

Use MiniZinc for declarative, verifiable models where the managed runtime
provides the execution boundary. Use the CP-SAT Python path when the problem
is naturally imperative, needs custom Python data structures, or you prefer
direct OR-Tools APIs.

## Tabular data I/O (Excel/CSV)

Real problem data usually arrives as a spreadsheet, and the answer usually has
to go back as one. Two backend-agnostic tools move scalars between local
`.xlsx`/`.csv` files and MCP — feeding either solving path:

- **`load_tabular_data(path, sheet=None, has_header=True, row_offset=0, max_rows=1000)`**
  → `TabularData` (`headers`, `rows`, `sheet_name`, `available_sheets`,
  `row_offset`, `next_row_offset`, `total_rows`, `truncated`,
  `truncation_reason`).
- **`write_tabular_result(headers, rows, target_path, overwrite=False)`**
  → `TabularWriteResult` (`status`, `message`, `target_path`, `sha256`,
  `format`, `rows_written`).

The server performs **mechanical I/O only** — it never infers what a column
*means*. Interpreting columns and building `.dzn` data or CP-SAT structures is
the client LLM's job: **LLM proposes, server verifies**, the same division of
labour as the solving tools.

### The cell contract

A cell is a **JSON scalar only**: string, number, boolean, or `null`. Nested
arrays/objects and non-finite numbers (`NaN`, `Infinity`) are rejected by the
tool's input schema, before any file is touched.

**Headers are always strings.** A date/time header becomes ISO-8601, any other
non-string becomes its text form, and a **blank** header (missing or empty)
becomes the positional name `col_1`, `col_2`, … — as do all columns when
`has_header=false`, where positional names are derived from the widest row in
the file so they stay stable across pages. Duplicate header names are preserved
as-is (de-duplicating them would be interpretation).

**Types.** On an XLSX read, date/time cells are converted to ISO-8601 strings
while numeric and boolean cells keep their scalar types. **CSV is textual**:
every cell reads back as a string, so `"3"` must be converted client-side
before use as a number. CSV parsing uses one fixed dialect (comma-separated,
`"`-quoted, UTF-8, BOM tolerated); semicolon and other locale dialects are
deliberately not sniffed. A type-preserving CSV round trip is **not** promised —
use `.xlsx` when types matter.

### Pagination and the response ceiling

`row_offset` is a zero-based offset among **data** rows (the header is not a
data row) and `max_rows` caps the page. The structured page body (`headers`,
`rows`, and pagination metadata) is additionally capped at a hard **1 MiB**
ceiling, independent of `max_rows` — whichever bound binds first. The ceiling
does not cover the tool call's separate human-readable text summary, so the
full MCP response is somewhat larger. Only **whole rows** are ever returned; a
cell or row is never silently cut.

When `truncated` is true, `truncation_reason` is `max_rows` or `max_bytes` and
`next_row_offset` is the offset to request next — pass it straight back to page
forward. At EOF both are `null`. `total_rows` always counts every data row in
the file, and headers are repeated on every page, so each page is
self-describing. A single row (or the headers alone) too large for the ceiling
is an **error naming the offending offset**, never a silent truncation.

Pagination bounds the *response*, not the scan: each call streams the file from
the start to count rows and reach the offset.

### Formula safety

The server never emits executable spreadsheet code. XLSX stores every string as
an explicit **string cell**, so `"=1+1"` is written and read back as the literal
text `=1+1`.

A CSV field cannot encode "this is literal text", so a CSV write **rejects** any
string whose first non-whitespace character is `=`, `+`, `-`, or `@`. Note this
also rejects a **number sent as a string**: send `-5` as the numeric cell `-5`,
not the string `"-5"` — or write `.xlsx`, which stores the text literally. There
is no opt-in formula path.

An XLSX cell string is capped at Excel's 32,767 characters; a longer one is
rejected rather than silently truncated. XLSX writes a single sheet named
`Sheet1`.

### XLSX round-trip hazards

Six more XLSX write rejections exist because the underlying writer
(openpyxl) has no error of its own for them — letting them through would
silently change the value (or make the file unreadable) on the next read:

- An **empty-string** row cell (`""`) is rejected: openpyxl cannot tell an
  empty string apart from `null` and always reads it back as `null`. Send
  `null` for "no value" instead. (A blank **header** is unaffected — it
  already collapses to a positional name by design; see above.)
- A **number past 16 significant digits** is rejected: XLSX serializes every
  numeric cell through a fixed 16-significant-digit format, so an integer
  past `2**53` or a float needing a 17th significant digit would otherwise
  come back changed. Send it as a string instead, or reduce its precision.
- A number whose **int/float type would silently flip** on read-back is
  rejected: XLSX has no separate int/float cell type — it's inferred purely
  from whether that same 16-significant-digit text contains a `.`/`e` — so
  an integral float like `100.0` formats as `"100"` and reads back an `int`,
  and a large int like `10**16` formats as `"1e+16"` and reads back a
  `float`. Send it as a string instead if the type must be preserved exactly.
- A string containing a **character XML cannot represent** (a lone surrogate,
  or the noncharacters `U+FFFE`/`U+FFFF`) is rejected: openpyxl's own check
  only catches C0 control characters, so one of these would otherwise write a
  numeric character reference the XML spec forbids, producing a file that
  cannot be re-parsed at all. Remove the character before writing.
- A **zero-column table** (`headers=[]`) is rejected: with no cells anywhere
  in the sheet, XLSX has nothing to derive a row count from and silently
  drops every row on read. (CSV has no such limitation.)
- A string containing a **carriage return** (`\r`, whether alone or as
  `\r\n`) is rejected: `\r` is legal XML, so the write "succeeds", but XML
  1.0 requires every parser to normalize a lone CR or a CRLF pair to a plain
  `\n` while parsing, so the value would silently come back changed on the
  next read. Use `\n` instead, or write `.csv`, which preserves `\r`/`\r\n`
  exactly.

A malformed or corrupt XLSX file (not a valid zip, or missing the parts an
XLSX workbook requires) is reported as an `invalid_request` diagnostic on
read, not a raw parser crash.

### The overwrite contract

`target_path` must be an explicit **absolute** local path whose parent directory
exists — the server never opens a file dialog.

The write is **atomic** and by default **cannot clobber**: the file is staged in
the target's own directory and published with a hard link, so with
`overwrite=false` an existing target — *even one created while the write was in
flight* — wins and is left byte-for-byte untouched, and the call is an error.
`overwrite=true` atomically replaces exactly that one file. A rejected write
leaves the filesystem untouched, and the staged file is removed on every path,
best-effort — a failure to remove it never overrides the outcome of the write
itself, so it may rarely leave a `.tabular-staging-*` file behind. (A
filesystem without same-directory hard links fails the no-overwrite write
safely rather than falling back to a clobber-prone commit.)

`sha256` is the digest of the staged file's bytes, computed before the commit
publishes them — identical to the committed file's bytes, since the commit is
a rename/link of that same staged file.

### Known limits

Reads take a formula cell's **cached** result (`data_only`) — the server never
evaluates a formula, so an uncalculated one reads as `null`. A merged cell
exposes its value only in the top-left position; the rest read blank. No `.ods`,
no `pandas`, no multi-sheet writes. Both tools are local-only: no network, no
telemetry, no subprocess, and no managed-runtime dependency.

## MCP prompts

The stdio server exposes four MCP prompts for client-side LLMs. One,
`solve_constraint_problem`, is available in **both** profiles; the other three
are **full**-profile only — start the server with
`openconstraint-mcp stdio --toolset full` to expose them (see [CLI](#cli)).

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
  [Reproducing a saved CP-SAT artifact](#reproducing-a-saved-cp-sat-artifact))
  are specialized Python constructions for the tougher 7-3-10 boundary instance.
- **`examples/golomb_ruler`** — find an optimal order-5 Golomb ruler: 5 marks
  on a ruler with all pairwise differences distinct, minimizing the ruler's
  length (`solve minimize`). A `save_verified_minizinc_model` artifact (see
  [`save_verified_minizinc_model`](#mcp-tools) above) — `problem.md` and
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
`find_unsat_core` under [MCP tools](#mcp-tools) for the tool's full contract,
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
     --index https://test.pypi.org/simple/ \
     openconstraint-mcp --help
   ```

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
