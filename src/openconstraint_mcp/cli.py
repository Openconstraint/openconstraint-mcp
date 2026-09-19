from __future__ import annotations

import enum
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .minizinc.core import MiniZincExecutionError, list_solvers
from .runtime import RuntimeMissingError, get_runtime_status, install_config_warning

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Local-first MCP server for constraint programming, powered by MiniZinc and OR-Tools CP-SAT.",
)
_console = Console()
_stderr_console = Console(stderr=True)


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _warn_on_corrupt_install_config() -> None:
    """Surface a present-but-unparseable install config to stderr instead of
    silently falling back to the default runtime location."""
    warning = install_config_warning()
    if warning is not None:
        _stderr_console.print(f"[yellow]Warning:[/yellow] {warning}")


class Toolset(enum.StrEnum):
    """Advertised MCP toolset for the ``stdio`` server (Typer choice values)."""

    core = "core"
    full = "full"


@app.command()
def stdio(
    toolset: Toolset = typer.Option(  # noqa: B008  (typer-standard pattern)
        Toolset.core,
        "--toolset",
        help=(
            "Advertised MCP toolset. 'core' (default) exposes 8 essential tools "
            "for a smaller tools/list payload, plus the backend-neutral "
            "solve_constraint_problem prompt; 'full' exposes every tool and all "
            "four prompts, adding background jobs, solver portfolios, explicit "
            "experiments, verified saves, inspection/unsat-core diagnostics, and "
            "tabular I/O."
        ),
    ),
) -> None:
    """Run the MCP server over stdio."""
    # Lazy-imported so metadata/runtime commands do not load the MCPServer
    # layer (and its httpx dependency) on the CLI's cold import path.
    from .server import run_stdio

    run_stdio(toolset.value)


@app.command("install-runtime")
def install_runtime(
    runtime_dir: Path | None = typer.Option(  # noqa: B008  (typer-standard pattern)
        None,
        "--runtime-dir",
        help=(
            "Install location. Overrides $OPENCONSTRAINT_MCP_RUNTIME_DIR, the "
            "persisted install config, and the platformdirs default. Skips the "
            "interactive path prompt."
        ),
    ),
    yes: bool = typer.Option(  # noqa: B008  (typer-standard pattern)
        False,
        "--yes",
        "-y",
        help=(
            "Non-interactive: skip the path prompt and the overwrite-confirm "
            "prompt for a prior managed install. Required for non-TTY runs."
        ),
    ),
) -> None:
    """Download and install the managed MiniZinc runtime.

    Supported platforms: Linux x86_64, macOS arm64, Windows x86_64.
    """
    # Lazy-imported so httpx/rich.progress stay out of stdio/check-runtime/list-solvers
    # cold paths. Enforced by the cold-import subprocess tests in tests/test_cli.py.
    from .runtime import get_runtime_dir, write_install_config
    from .runtime_install.core import (
        check_supported_platform,
        install_managed_runtime,
        validate_install_target,
    )
    from .runtime_install.errors import RuntimeInstallError

    try:
        check_supported_platform()
    except RuntimeInstallError as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    _warn_on_corrupt_install_config()

    if runtime_dir is not None:
        target = runtime_dir
    elif yes or not _stdin_is_tty():
        target = get_runtime_dir()
    else:
        default = get_runtime_dir()
        answer = typer.prompt(
            f"Install MiniZinc runtime at [{default}]",
            default=str(default),
            show_default=False,
        )
        target = Path(answer.strip() or str(default))

    target_resolved = target.expanduser()
    try:
        target_resolved = validate_install_target(target_resolved)
    except RuntimeInstallError as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    effective_yes = yes
    if target_resolved.is_dir() and any(target_resolved.iterdir()):
        # At this point validate_install_target guarantees it is a managed installation.
        if yes:
            effective_yes = True
        elif _stdin_is_tty():
            answer = typer.prompt(
                f"{target_resolved} is a prior managed runtime. Overwrite? [y/N]",
                default="n",
                show_default=False,
            )
            if answer.strip().lower() in {"y", "yes"}:
                effective_yes = True
            else:
                _console.print("aborted, nothing was changed")
                raise typer.Exit(code=0)
        else:
            _console.print(
                f"[red]{target_resolved} is a prior managed runtime; pass --yes "
                "to overwrite it non-interactively.[/red]"
            )
            raise typer.Exit(code=1)

    try:
        installed = install_managed_runtime(target_resolved, yes=effective_yes, console=_console)
    except (RuntimeInstallError, OSError) as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    try:
        write_install_config(installed)
    except OSError as exc:
        _console.print(
            f"[yellow]Runtime installed at {installed}, but could not save "
            f"the install config ({exc}). Set "
            f"OPENCONSTRAINT_MCP_RUNTIME_DIR={installed} or fix the config "
            f"directory and re-run install-runtime to persist the location."
            "[/yellow]"
        )


@app.command("configure-runtime")
def configure_runtime(
    runtime_dir: Path = typer.Option(  # noqa: B008  (typer-standard pattern)
        ...,
        "--runtime-dir",
        help=(
            "Path to an existing MiniZinc install (a directory containing "
            "bin/minizinc). The path is persisted so check-runtime and "
            "list-solvers find it without setting OPENCONSTRAINT_MCP_RUNTIME_DIR."
        ),
    ),
) -> None:
    """Point openconstraint-mcp at an existing MiniZinc install (no download)."""
    from .runtime import write_install_config

    target = runtime_dir.expanduser().resolve()
    if not target.is_dir():
        _console.print(f"[red]Not a directory: {target}[/red]")
        raise typer.Exit(code=1)

    binary_name = "minizinc.exe" if sys.platform == "win32" else "minizinc"
    binary = target / "bin" / binary_name
    if not binary.is_file():
        _console.print(
            f"[red]{target} does not look like a MiniZinc install: "
            f"expected {binary} to exist.[/red]"
        )
        raise typer.Exit(code=1)
    if sys.platform != "win32" and not os.access(binary, os.X_OK):
        _console.print(f"[red]{binary} is not executable.[/red]")
        raise typer.Exit(code=1)

    try:
        write_install_config(target)
    except OSError as exc:
        _console.print(
            f"[red]Could not save install config ({exc}). Set "
            f"OPENCONSTRAINT_MCP_RUNTIME_DIR={target} as a workaround.[/red]"
        )
        raise typer.Exit(code=1) from exc

    _console.print(f"[green]Configured runtime at {target}[/green]")


@app.command("check-runtime")
def check_runtime() -> None:
    """Report whether the managed MiniZinc runtime is installed."""
    _warn_on_corrupt_install_config()
    status = get_runtime_status()
    if status.installed:
        _console.print(f"[green]Runtime installed[/green] at {status.minizinc_binary}")
        return
    _console.print(f"[red]Runtime not installed.[/red] Expected at {status.runtime_dir}")
    raise typer.Exit(code=1)


@app.command("list-solvers")
def list_solvers_cmd() -> None:
    """List solvers available in the managed MiniZinc runtime."""
    _warn_on_corrupt_install_config()
    try:
        result = list_solvers()
    except (RuntimeMissingError, MiniZincExecutionError) as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Available solvers")
    table.add_column("id")
    table.add_column("name")
    table.add_column("version")
    for solver in result.solvers:
        table.add_row(solver.id, solver.name, solver.version or "")
    _console.print(table)
