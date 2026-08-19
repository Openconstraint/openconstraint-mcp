# CP-SAT Python execution path

In addition to the MiniZinc declarative path, `openconstraint-mcp` exposes a
second solving path: the client's LLM writes OR-Tools CP-SAT Python, and the
server runs it in a **local child process**.

## Four separate steps

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

## Recommended generated-script layout

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

## File-backed instances (spreadsheets and large data)

When the problem instance arrives as an `.xlsx`/`.csv`, or is simply too large
to paste, the instance stays on disk and **never passes through the client's
context**. `load_tabular_data` is the wrong tool for moving it: it returns at
most `max_rows` rows (default 1000, and further trimmed by the 1 MiB response
ceiling), so an instance hardcoded from a truncated read silently solves a
different problem. Use it to learn the shape — `headers`, `available_sheets`,
`total_rows`, and a sample of rows — and decide what each column means, which
is the one judgement the server never makes for you.

The child process runs on the server's own interpreter, which ships `openpyxl`
alongside `ortools`, so a generated script can open the workbook directly:

```python
def read_input() -> list[dict]:
    workbook = openpyxl.load_workbook(sys.argv[1], read_only=True, data_only=True)
    try:
        sheet = workbook["Orders"]
        # Load-bearing, and it has to come before any iteration. In read-only
        # mode openpyxl trusts the sheet's stored <dimension> ref and trims
        # EVERY row to it -- the header row included -- so a writer that
        # understates or omits that ref makes a populated sheet read as
        # narrow, or as empty, with no error raised and no width mismatch for
        # a zip() check to catch. Clearing the cached bounds makes iter_rows
        # yield each row at its real width.
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        header = next(rows)
        if any(name is None for name in header):
            raise ValueError("blank header cell: name every column before solving")
        if len(set(header)) != len(header):
            # dict() keeps only the last cell of each duplicated name, which
            # would drop a column silently. load_tabular_data preserves
            # duplicates because it reports rows positionally; a dict cannot.
            raise ValueError(f"duplicate column names: {header}")
        records = []
        for row in rows:
            if not any(cell is not None for cell in row):
                continue  # trailing blank row
            if len(row) > len(header):
                raise ValueError(f"row of {len(row)} cells under {len(header)} headers")
            # Natural width means a row whose trailing cells are blank is
            # SHORT, not None-padded -- pad it back rather than letting
            # strict=True reject a perfectly ordinary sheet. strict=True then
            # only asserts that this padding was right.
            padded = tuple(row) + (None,) * (len(header) - len(row))
            records.append(dict(zip(header, padded, strict=True)))
        return records
    finally:
        workbook.close()  # required in read-only mode
```

`read_only=True` skips building openpyxl's per-cell object graph, which is the
dominant cost of reading a large workbook — but the function above still
returns every row, so peak memory tracks the instance, not just the solve. When
the workbook is larger than the model needs, filter or aggregate inside
`read_input()` rather than materializing rows the model will never look at.
`openpyxl` reads workbooks only — for a `.csv` instance use the standard
library's `csv.DictReader`, which streams the same way and needs the same
explicit width and duplicate-header checks, since a short row yields `None`
values, a long one lands under `restkey`, and a repeated fieldname is silently
collapsed. `args` cannot substitute for this — it is capped at 32 KiB
(`MAX_CHILD_ARGV_BYTES`) precisely because it is a flag and path list, not a
data channel.

Run the pair with the checked file tool:

```python
run_cpsat_python_file_checked(
    script_path="/abs/model.py",
    args=["/abs/instance.xlsx"],          # -> the child's sys.argv[1]
    checker_path="/abs/checker.py",
    problem={
        "request": "<the user's original words, verbatim>",
        "data_path": "/abs/instance.xlsx",
        "sheet": "Orders",
    },
)
```

The checker child is launched with the **payload path as its only argument**,
so `args` never reaches it — the instance file's path has to travel inside
`problem`, in the same flat JSON object that must also carry the user's
original request. A path-based checker runs with its working directory set to
its own parent, so a relative sibling reference resolves as well.

A checker that **rereads the workbook** grades against the source of truth
rather than against the script's own parse, which makes the verdict independent
of a transcription slip. It does not make it independent of a shared
*misreading* — if both parsers agree that column D is the due date when it is
the release date, the checker still accepts. Confirm the column mapping with
the user; no tool can verify it.

### Scaling a verified script to a bigger instance

Given a `model.py`/`checker.py` pair already validated against a small
`data.json`, moving to a large `.xlsx` changes **`read_input()` and nothing
else** — `parse_input()`, `solve()`, and `serialize_solution()` keep operating
on the records they already agree on. That is what the ordered spine buys.

`data.json` doubles as the specification: `read_input()` must return exactly
what `parse_input()` already accepts. Keep the JSON branch alive so the small
instance stays a regression check, and confirm both branches parse it
identically **before** committing to a long solve — a column mis-mapping is
much cheaper to find there than after a two-hour run.

Two consequences of a bigger instance:

- **Long runs.** The streaming solution callback the prompts mandate
  unconditionally means a run killed at `script_timeout_ms` still returns the
  last emitted incumbent — **provided the output cap was not hit first**. Keep
  the callback inside a fixed byte budget (the prompt's example uses 512 KiB of
  the combined 1 MiB stdout+stderr cap). Overrunning that cap tree-kills the
  child and flags the run `truncated`, and a partial result is recovered only
  on the *timeout* path: a truncated run that did not time out comes back
  `status="error"` with no solution at all. An unbudgeted per-improvement
  callback on a big instance is exactly how a run loses every incumbent it
  found. For a run longer than the client will wait,
  `submit_cpsat_python_file_job` accepts both `args` and `checker_path` — but
  no `seed` or `config`, and its registry does not survive a server restart.
- **Not savable.** `save_verified_cpsat_python` takes inline `source` only and
  re-runs it in a fresh temporary directory, so it can replay neither `args`
  nor a sibling data file. Keep the small inline instance as the reproducible
  saved artifact and treat the file-backed run as a production run.

Deliver the answer as a spreadsheet with
[`write_tabular_result`](tabular-data.md) when the user wants one. That is a
**presentation** step, not a way to keep a large result out of context — the
tool takes every row as a call argument, so the rows pass through the client
either way. It also does not relax the script's own obligation: the output
contract requires the complete answer in the stdout envelope. Nothing in the
server enforces that — envelope validation only checks that `solution` is a
non-empty object, and `save_verified_cpsat_python`'s reported gate only adds a
check on `status`, so a `solution` carrying nothing but `{"result_file": …}`
passes both. Only an independent checker, which grades the parsed `solution`
and never sees your filesystem, turns the completeness rule into something
verified rather than merely stated.

## Delivering several script variants

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

## Tools

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

## Explicit experiments

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

### Persisting an attempt from an experiment

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

## Reproducing a saved CP-SAT artifact

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

## Background CP-SAT jobs

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

### Checked background jobs (diagnostic only)

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

### Configuring CP-SAT registry bounds

The CP-SAT job registry has its own three bounds, independently configurable
from the MiniZinc registry:

| Env var | Meaning | Default | Minimum |
| --- | --- | --- | --- |
| `OPENCONSTRAINT_MCP_CPSAT_MAX_RUNNING_JOBS` | CP-SAT jobs running concurrently | `4` | `1` |
| `OPENCONSTRAINT_MCP_CPSAT_MAX_QUEUED_JOBS` | Submissions queued past the running cap | `16` | `0` |
| `OPENCONSTRAINT_MCP_CPSAT_MAX_RETAINED_TERMINAL` | Finished jobs kept for status polling | `64` | `1` |

An invalid value — non-integer or below the minimum — **fails fast at server
start, naming the offending variable** (no silent fallback to the default).

## Security posture

**The server executes user-provided Python locally. It is not sandboxed.**
Timeout + output-cap + process-tree kill is a **robustness** boundary, not
a security sandbox. The child is also launched with its stdin closed
(`DEVNULL`) so a script that reads `input()`/`sys.stdin` gets an immediate
EOF instead of consuming the server's JSON-RPC stream when running over
stdio. There is no AST filtering, no network blocking, no import allowlist.
This tool is local-only; a cloud/multi-tenant deployment would require a
real sandbox. The **server wrapper** makes no network calls,
but the executed child process is arbitrary code.

## Example scripts

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

### Comparing explicit source variants

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

### Satisfaction save with a checker

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

### Optimization save with an expectation threshold

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

## MiniZinc vs. CP-SAT Python

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
