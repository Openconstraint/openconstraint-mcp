from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .errors import RuntimeInstallError

MINIZINC_VERSION: str = "2.9.7"

BundleKind = Literal["tgz", "dmg", "nsis"]


class BundleSpec(BaseModel):
    """One pinned upstream MiniZinc release asset the installer can fetch."""

    model_config = ConfigDict(frozen=True, strict=True)

    filename: str
    url: str
    sha256: str
    kind: BundleKind


def _release_url(filename: str) -> str:
    return (
        f"https://github.com/MiniZinc/MiniZincIDE/releases/download/{MINIZINC_VERSION}/{filename}"
    )


# SHA256s of the upstream MiniZincIDE GitHub release assets. Recompute
# alongside MINIZINC_VERSION when bumping.
_LINUX_X86_64_FILENAME = f"MiniZincIDE-{MINIZINC_VERSION}-bundle-linux-x86_64.tgz"
_LINUX_X86_64_BUNDLE = BundleSpec(
    filename=_LINUX_X86_64_FILENAME,
    url=_release_url(_LINUX_X86_64_FILENAME),
    sha256="7e78d3a1d6feec2f5b6a43628632decb6995755ade92ff4e51a2188c54ca6399",
    kind="tgz",
)

# The macOS asset is a universal (x86_64 + arm64) build; gating it to Apple
# Silicon is a deliberate v0 scope choice, not an upstream constraint.
_MACOS_FILENAME = f"MiniZincIDE-{MINIZINC_VERSION}-bundled.dmg"
_MACOS_ARM64_BUNDLE = BundleSpec(
    filename=_MACOS_FILENAME,
    url=_release_url(_MACOS_FILENAME),
    sha256="504d04d3315f2a76455b71feff2cc2b3105ecd5533e8194fa2365bc41289d9d9",
    kind="dmg",
)

# Windows ships the runtime only as an NSIS installer (no portable archive); it
# is run silently into the managed runtime dir (see archive._install_nsis_bundle).
_WINDOWS_FILENAME = f"MiniZincIDE-{MINIZINC_VERSION}-bundled-setup-win64.exe"
_WINDOWS_X86_64_BUNDLE = BundleSpec(
    filename=_WINDOWS_FILENAME,
    url=_release_url(_WINDOWS_FILENAME),
    sha256="475547257801629012dc09fac21a7511a73a8931499dd08b3f88893d2e700d5a",
    kind="nsis",
)


def select_bundle() -> BundleSpec:
    """Resolve the managed bundle for the current platform.

    Raises :class:`RuntimeInstallError` when no managed bundle exists for this
    platform/architecture, so callers gate before prompting or downloading.
    """
    machine = platform.machine()
    if sys.platform == "linux" and machine == "x86_64":
        return _LINUX_X86_64_BUNDLE
    if sys.platform == "darwin" and machine == "arm64":
        return _MACOS_ARM64_BUNDLE
    if sys.platform == "win32" and machine == "AMD64":
        return _WINDOWS_X86_64_BUNDLE
    raise RuntimeInstallError(
        "openconstraint-mcp install-runtime currently supports Linux x86_64, "
        "macOS arm64 (Apple Silicon), and Windows x86_64 only. On other "
        "platforms, install MiniZinc manually and point openconstraint-mcp at "
        "it with `openconstraint-mcp configure-runtime --runtime-dir <dir>` or "
        "by setting OPENCONSTRAINT_MCP_RUNTIME_DIR."
    )


def _download_archive(
    url: str,
    dest: Path,
    expected_sha256: str,
    console: Console,
) -> None:
    """Stream ``url`` to ``dest`` with a rich progress bar and SHA256 verify.

    On any HTTP error or checksum mismatch ``dest`` is unlinked and a
    :class:`RuntimeInstallError` is raised so callers never see a partial or
    tampered file on disk.
    """
    timeout = httpx.Timeout(30.0, read=120.0)
    hasher = hashlib.sha256()
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise RuntimeInstallError(
                        f"download failed: HTTP {response.status_code} from {url}"
                    )
                total = int(response.headers.get("content-length", 0)) or None
                with Progress(
                    TextColumn("[bold blue]Downloading"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                    transient=True,
                ) as progress:
                    task_id = progress.add_task("download", total=total)
                    with dest.open("wb") as fh:
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            fh.write(chunk)
                            hasher.update(chunk)
                            progress.update(task_id, advance=len(chunk))
    except RuntimeInstallError:
        if dest.exists():
            dest.unlink()
        raise
    except httpx.HTTPError as exc:
        if dest.exists():
            dest.unlink()
        raise RuntimeInstallError(f"download failed for {url}: {exc}") from exc

    digest = hasher.hexdigest()
    if digest.lower() != expected_sha256.lower():
        if dest.exists():
            dest.unlink()
        raise RuntimeInstallError(
            "checksum mismatch for downloaded MiniZinc archive: "
            f"expected {expected_sha256}, got {digest}"
        )
