# AGENTS.md

Instructions for AI coding agents (Codex CLI, Claude Code, Cursor, etc.) working in this repository.

## Project

**openconstraint-mcp** is an open-source, local-first MCP server for constraint programming and optimization. It wraps a *managed* MiniZinc runtime (bundled and controlled by this project, not the user's system install) and exposes OSS solvers — OR-Tools CP-SAT as the default, Chuffed as an optional verifier — over the Model Context Protocol.

The bar for v0 is "easy install, reliable solving, clear errors", not feature breadth.

## Working Principles

### 1. Think Before Coding

State assumptions explicitly. Present multiple interpretations when the request is ambiguous instead of silently picking one. Push back when a simpler approach exists. Stop and name what's unclear rather than guessing.

### 2. Simplicity First

Write the minimum code that solves the stated problem. No speculative features, no abstractions for single-use code, no configurability that wasn't requested, no error handling for impossible scenarios. If 200 lines could be 50, rewrite it. Test: would a senior engineer call this overcomplicated?

Avoid over-engineering and overkill: build the simplest thing that meets the stated requirements, and treat these as stop-and-ask signals rather than things to build and mention afterward — a config knob, an abstraction layer, a retry/cache/fallback, or a new module the request did not name. Stopping early applies to *design surface*, never to scope: finish the tests, docs, and every part of the ask.

### 3. Surgical Changes

Touch only what the task requires. Don't "improve" adjacent code, comments, or formatting. Match existing style even if you'd do it differently. Flag unrelated dead code — don't delete it. Remove imports/variables/functions that *your* changes orphaned; leave pre-existing dead code alone. Every changed line should trace to the user's request.

### 4. Goal-Driven Execution

Define success criteria before starting; loop until verified.

| Instead of...    | Transform to...                                       |
| ---------------- | ----------------------------------------------------- |
| "Add validation" | "Write tests for invalid inputs, then make them pass" |
| "Fix the bug"    | "Write a test that reproduces it, then make it pass"  |
| "Refactor X"     | "Ensure tests pass before and after"                  |

For multistep tasks, state a brief plan with a verification check per step.

### 5. Planning Documents Are Not Code Dumps

Plans live under `docs/plans/` (gitignored — local to your working copy, never committed). They must be concise, behavior-first execution guides. Do **not** embed full implementation code blocks for whole files, functions, or tests. They waste context, go stale quickly, and encourage agents to copy bugs mechanically.

A good plan includes:

- Goal and non-goals.
- Explicit assumptions and decisions.
- A task list with files to touch, behavior to implement, tests to add, and verification commands.
- Acceptance criteria and known risks.
- Small snippets only when they clarify an interface, command, schema, or tricky invariant.

If code is necessary in a plan, keep it to function signatures, short pseudocode, or command examples. Do not include step-by-step commit commands unless the user explicitly asks for commits.

Plans must preserve explicit user requirements. If a plan intentionally deviates from a user requirement, stop and ask for approval instead of burying the deviation in a note.

## Architecture (v0)

```
cli  ──►  server  ──►  minizinc  ──►  runtime  ──►  schemas
 │                 │
 │                 └──►  pyexec  (subprocess executor; imports shared.childrun, shared.proc, shared.save_target, shared.hashing, shared.childproc, schemas; never minizinc/runtime)
 └─────►  runtime_install   (install-time only; imports no internal modules)
```

A module may import any module to its right. Imports never flow leftward or between same-layer modules. The `pyexec` subtree is a parallel path from `server`: it executes user/LLM-provided OR-Tools CP-SAT Python in a child process (`sys.executable`), importing only the `shared` package's dependency-light leaves `shared.childrun` (the capped timeout/output-cap/tree-kill child executor, shared with `minizinc`), `shared.proc` (process-group launch + tree-kill), `shared.save_target` (manifest-gated save policy), `shared.hashing` (file-content sha256), `shared.childproc` (the `ChildProcessTracker` type), and `schemas` (the `CpsatPythonResult`/`CpsatStatus` return contract), never `minizinc` or `runtime`. `shared.childrun` itself imports only `shared.proc` + `shared.childproc` (plus stdlib). `runtime_install` is a leaf used only by `cli` (lazily, so its `httpx`/`rich.progress` deps stay off the cold paths); it imports no internal modules, so it sits outside the left-to-right chain.

## Before You Run Commands

**Always run `just --list` at the start of a session that will execute commands.** The `justfile` is the source of truth for project automation; prefer `just <recipe>` over raw `uv ...` invocations.

If `just` is unavailable in your environment, fall back to the underlying `uv run ...` commands the justfile uses. Do **not** invoke raw `python` or `pip` — this project is `uv`-managed end-to-end.

## Privacy & Network

- **Telemetry is not implemented.** Do not add it. Any future telemetry must be opt-in and documented.
- **Nothing leaves the user's machine without explicit opt-in** — no background calls, version checks, analytics, or remote logging.
- **Runtime download is user-invoked only.** `install-runtime` fetches when the user runs it — never on import, on first `stdio` boot, or as a "convenience" auto-install.
- **Installer location is user-controllable.** Any managed-runtime installer must support an explicit install directory, a sensible per-user default, and non-interactive operation for CI/client-driven flows.

## Code Style

- **Target Python 3.12** (development happens on 3.14). Avoid 3.13+ syntax and stdlib.
- **Type hints everywhere.** Public functions get full annotations. `mypy src examples` must pass — `just typecheck` gates the example scripts alongside the package, so a bare `dict` or `list` annotation in `examples/` fails the build the same way it does in `src/`.
- **Annotate local-name bindings.** Give every variable declared with a simple name assignment an explicit type. Assignments to existing attributes or container elements rely on the owning object's annotation; Python cannot annotate `for`/comprehension targets or subscript assignments.
- **Pydantic v2 models** for any structured input or output (MCP tool results, CLI structured output, config). Plain dicts are for ephemeral internal use only.
- **Pydantic everywhere, dataclasses nowhere.** This extends past the server package to `examples/` and to the worked snippet in `protocol_text/prompts.py`: the typed records a script passes across its `read_input`/`parse_input`/`solve`/`serialize_solution` boundary are Pydantic models deriving from a small local `FrozenModel` base with `model_config = ConfigDict(frozen=True, strict=True)`. `strict=True` is deliberate — it reproduces a dataclass's no-coercion behavior instead of silently widening a `float` into a `Decimal` field. One model kind repo-wide beats a per-file judgment call about whether a record is "complex enough" to deserve validation.
- **Example instance data lives in `data/`.** A new `examples/<name>/` folder that ships instance files (`.json`, `.dzn`, `.ros`/`.roster`, or similar) keeps them in an `examples/<name>/data/` subdirectory — resolved as `Path(__file__).parent / "data" / <filename>` — rather than loose in the example's root next to its scripts. A parsed/derived form that isn't the raw benchmark file (e.g. nurse_rostering's JSON conversion of its `.ros`/`.roster` XML) gets its own sibling subdirectory (`parsed/`) instead of piling into `data/`. Committed solve output kept for reference (like `flexible_job_shop/results/`) is a separate concern and stays out of both.
- **`pathlib.Path`** for filesystem work; do not pass raw strings around as paths.
- **One responsibility per file.** Files that change together live; split by responsibility, not by technical layer.
- **Keep functions testable.** Inject dependencies (paths, subprocess runners, clocks) where it makes a function meaningfully easier to mock. Avoid global state.

## Refactoring

- **Prefer direct imports; add a facade only for a real contract.** The v0 public surface is the CLI commands, MCP tools/prompts, and the `openconstraint-mcp` entry point (`openconstraint_mcp:main`) — *not* Python import paths. When a module becomes a package, callers import the submodules directly (`from pkg.module.core import X`, the way `server.py` imports `protocol_text.descriptions`) and the package `__init__.py` stays a docstring-only marker. Do **not** add a re-export facade `__init__.py` to keep an old `from pkg.module import X` path alive for hypothetical external users — in early development we delete such paths, not preserve them. Reserve a facade for an import path that is a genuine documented contract (README, `pyproject` entry point, published API); never break one of *those* without explicit user approval.
- **`core.py` holds orchestration and the public implementations; leaves are single-purpose modules** (parser, downloader, archive handler) — single-purpose, not necessarily side-effect-free. Callers import what they need from `core` and the dependency-light leaves directly; the package `__init__.py` carries no exported contract.
- **Don't couple sibling leaves just to share a primitive.** When two leaves need a common exception, constant, or helper, extract it to a dependency-light leaf both import (e.g. `runtime_install/errors.py`) rather than importing one leaf from the other. Orchestrator-to-leaf imports (`core.py` → `archive`/`download`) are intended and stay.
- **Check `shared/` (and sibling leaves) for an existing helper before writing a new one.** A quick grep beats a duplicate — `shared/hashing.py` (file-content sha256), `shared/save_target.py` (text hashing, manifest-gated save policy, atomic directory commit), and `shared/job_errors.py` (time and error-summary primitives) already hold what multiple modules need.
- **Centralize an invariant once it has two call sites.** Argv order, runtime-presence gates, path validation, and user-facing error text live in one helper (`_build_minizinc_cmd`, `_require_minizinc_binary`) so call sites can't drift.
- **Refactor tests with the code.** When a module splits, move its tests to mirror the new layout and import each extracted leaf directly, with a test for every leaf that has non-trivial behavior — proving the behavior moved. Don't add a test whose only purpose is to assert that a re-export still resolves.
- **A behavior-preserving refactor declares its invariants.** State whether behavior, dependencies, public imports, network posture, and docs changed; if the claim is "no behavior change", a test must back it.

## Solving Scope

The v0 introspection-only restriction is lifted. Solving features — `solve`, `optimize`, model validation, dry-run compilation, solution checking, global-constraint lookup, and similar — may be added incrementally as long as the following invariants hold:

- **Local-first.** All solving runs on the user's machine. No remote solving backends, no upload of models or data, no telemetry on solver runs.
- **Managed runtime (MiniZinc path).** MiniZinc solver execution must use the managed/local runtime resolved through the runtime layer (`OPENCONSTRAINT_MCP_RUNTIME_DIR` or the install config), never an arbitrary `$PATH` lookup. A second backend — OR-Tools CP-SAT Python execution — runs user/LLM-provided OR-Tools CP-SAT Python in a child process (`sys.executable`, the server's own venv which ships `ortools`), with a timeout, a stdout/stderr byte cap, and process-tree kill. **This is a local-only, robustness boundary, not a security sandbox.** v0 performs no sandboxing, no network blocking, and no AST/import filtering; a cloud or multi-tenant deployment would require a real sandbox and is out of scope. **Honest network posture:** the server *wrapper* makes no network calls on any code path; the executed child is arbitrary code that the server does not police, so "offline" is a property of the wrapper, not a guarantee about the child. The child must not generate network or file-mutating code unless the user explicitly requested it (enforced via the client-facing prompt, not by the server).
- **No server-owned LLM calls.** The MCP server must never own LLM credentials or call an LLM directly. MCP sampling through a connected client's advertised capability is allowed to help answer an explicit user request; it must not run autonomously or create a server-side agent loop.
- **No LangChain / LangGraph in the core server.** Do not pull these dependencies into the server package. They imply agent loops and LLM coupling that conflict with the deterministic, local-first posture.
- **No hidden network calls.** Solving, validation, model lookup, and result inspection must all be offline. The only sanctioned network call remains the user-invoked `install-runtime` download.

LLM-assisted modeling — natural-language → MiniZinc, model critique, repair suggestions, explanation — belongs in the **MCP client**. The server's job is to expose deterministic, verifiable MCP tools and prompts the client's LLM can call: model validation, dry-run compilation, solving, solution checking, global-constraint lookup, example retrieval, etc. The division of labor is **LLM proposes, server verifies.**

The current scope is discrete optimization through two paths: MiniZinc (expressive models, verification, rich constraint library — via the managed runtime) and OR-Tools CP-SAT Python execution (zero-install, LLM-fluent — the client LLM writes complete OR-Tools Python, the server executes it locally in a child process and returns structured results).

## v0 Scope Guards

- **Managed-runtime download is installer-only in v0.** `install-runtime` may download a pinned MiniZinc bundle when the user invokes it explicitly. No import, MCP server boot, `check-runtime`, or `list-solvers` path may download anything.

## Testing

- **Framework: `pytest`** (with `pytest-asyncio` available but only used when needed).
- **Unit tests must not require a real MiniZinc runtime.** Use the `OPENCONSTRAINT_MCP_RUNTIME_DIR` env var + `tmp_path` pattern (see `tests/conftest.py`) to point the runtime layer at an empty directory.
- **Mock all network and subprocess in unit tests.** Real-binary tests get `@pytest.mark.integration` and stay out of the default `just check`.
- **One behavior per test.** Long setup is fine; multi-assert telescopes are not.

## Documentation

- **Route reference material to `docs/`, keep `README.md` for orientation.** The README is also the PyPI long description, so it stays short enough to read top to bottom: intro, design principles, install, quick start, CLI, examples, managed runtime, limitations, licensing. The per-tool and per-prompt reference lives in `docs/mcp-tools.md`, `docs/cpsat-python.md`, `docs/mcp-prompts.md`, and `docs/tabular-data.md`. A new MCP tool or prompt is documented in the matching `docs/` file, **not** appended to the README; the README's stub section already links there and needs no edit. A new CLI command, flag, or install step *is* a README change.
- **Links from `README.md` into `docs/` must be absolute GitHub URLs** (`https://github.com/Openconstraint/openconstraint-mcp/blob/master/docs/<file>.md`). PyPI does not rewrite relative links in the long description, so a relative path renders as a 404 on the project page. Links between files inside `docs/`, and anchors within a single file, stay relative.
- **Document managed-runtime behaviour:** where the MiniZinc bundle lives, how to override it (`OPENCONSTRAINT_MCP_RUNTIME_DIR`), and what version it pins.
- **Surface third-party licenses** for anything bundled (MiniZinc, OR-Tools, Chuffed) in a `LICENSES/` directory or an equivalent README section.

## Definition of Done

A change is done when:

1. `just check` is green.
2. New behavior has unit tests; non-trivial behavior has at least one CLI- or MCP-level smoke test.
3. User-facing changes are reflected in `README.md`.
4. No new telemetry, no new hidden network calls, no new global mutable state.
