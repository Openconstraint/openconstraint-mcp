"""Orchestration for the two public operations: read a page, write a file."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ...schemas.tabular import (
    ChartSpec,
    GanttSpec,
    TableStyle,
    TabularCell,
    TabularData,
    TabularWriteResult,
)
from ..hashing import path_sha256
from .charts import ResolvedChart, render_charts, resolve_charts
from .gantt import ResolvedGantt, render_gantt, resolve_gantt
from .guards import (
    _reject_columnless_xlsx,
    _reject_csv_formulas,
    _reject_illegal_xlsx_characters,
    _reject_lossy_xlsx_numbers,
    _reject_oversized_xlsx_strings,
    _reject_xlsx_carriage_returns,
    _reject_xlsx_empty_strings,
    _validate_cells,
)
from .limits import DEFAULT_MAX_ROWS, MAX_TABULAR_RESPONSE_BYTES, WRITE_SHEET_NAME
from .paths import _format_for, validate_tabular_read_path, validate_tabular_write_path
from .reading import (
    _body_size,
    _build_page,
    _normalize_header,
    _scan_source,
    _trim_to_byte_ceiling,
)
from .style import ResolvedStyle, apply_style, resolve_style
from .writing import _write_csv, _write_xlsx, build_xlsx_workbook


def read_tabular_data(
    path: Path,
    *,
    sheet: str | None = None,
    has_header: bool = True,
    row_offset: int = 0,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> TabularData:
    """Read one bounded page of rows from a ``.xlsx`` or ``.csv`` file.

    ``row_offset`` is a zero-based offset among data rows (the header, when
    present, is not a data row); ``max_rows`` caps the page. The serialized
    body is additionally capped at ``MAX_TABULAR_RESPONSE_BYTES`` — whichever
    bound first is reported as ``truncation_reason``, and the page always
    contains whole, unmodified rows. Every later page is directly reachable via
    the returned ``next_row_offset``.

    Scan cost is proportional to the whole file, not the page: reaching an
    offset and counting ``total_rows`` both require streaming from the start.
    """
    if row_offset < 0:
        raise ValueError(f"row_offset must be >= 0 (got {row_offset})")
    if max_rows < 1:
        raise ValueError(f"max_rows must be >= 1 (got {max_rows})")

    resolved = validate_tabular_read_path(path)
    scan, sheet_name, available_sheets = _scan_source(
        resolved, sheet, has_header=has_header, row_offset=row_offset, max_rows=max_rows
    )

    # Headers must be identical on every page, so a headerless file derives its
    # positional names from the WIDEST row in the file — not from whichever
    # rows this page happens to contain. Likewise, a header row narrower than
    # some later data row (ragged CSV) still needs a name for every cell any
    # page could contain, so the header count covers the wider of the two.
    if scan.header_values is not None:
        header_count = max(len(scan.header_values), scan.width)
        headers = [
            _normalize_header(
                scan.header_values[index] if index < len(scan.header_values) else None, index
            )
            for index in range(header_count)
        ]
    else:
        headers = [f"col_{index + 1}" for index in range(scan.width)]

    candidate = _build_page(
        headers=headers,
        rows=scan.page,
        sheet_name=sheet_name,
        available_sheets=available_sheets,
        row_offset=row_offset,
        total_rows=scan.total_rows,
        byte_limited=scan.byte_limited,
    )
    if _body_size(candidate) <= MAX_TABULAR_RESPONSE_BYTES:
        return candidate
    return _trim_to_byte_ceiling(
        headers=headers,
        rows=scan.page,
        sheet_name=sheet_name,
        available_sheets=available_sheets,
        row_offset=row_offset,
        total_rows=scan.total_rows,
    )


def _reject_csv_presentation(
    resolved: Path,
    *,
    style: TableStyle | None,
    gantt: GanttSpec | None,
    charts: list[ChartSpec] | None,
) -> None:
    """Refuse styling or diagrams on a CSV target rather than ignoring them."""
    requested = [
        name
        for name, value in (("style", style), ("gantt", gantt), ("charts", charts))
        if value
    ]
    if requested:
        raise ValueError(
            f"{', '.join(requested)} needs a workbook, but the target {resolved} is a csv "
            f"file; a CSV has no sheets, cell formatting, or charts. Write .xlsx instead."
        )


def _reject_colliding_diagram_sheets(
    gantt: ResolvedGantt | None, charts: list[ResolvedChart]
) -> None:
    """Refuse a Gantt and a chart sheet that would land on the same sheet.

    Each spec validates in isolation, so neither can see the other's
    ``sheet_name``; this is the one place that holds both. The comparison is
    case-insensitive because openpyxl matches sheet titles that way.
    """
    if gantt is None:
        return
    for chart in charts:
        if chart.sheet_name.casefold() == gantt.sheet_name.casefold():
            raise ValueError(
                f"the Gantt sheet {gantt.sheet_name!r} collides with the chart sheet "
                f"{chart.sheet_name!r} (sheet titles match case-insensitively); give one "
                f"of them another sheet_name"
            )


def write_tabular_data(
    headers: list[str],
    rows: list[list[TabularCell]],
    target_path: Path,
    *,
    overwrite: bool = False,
    style: TableStyle | None = None,
    gantt: GanttSpec | None = None,
    charts: list[ChartSpec] | None = None,
) -> TabularWriteResult:
    """Write ``headers``/``rows`` to a ``.xlsx`` or ``.csv`` file, atomically.

    Everything that could reject the write — path, row widths, cell types,
    formula safety, XLSX string lengths, and every ``style``/``gantt``/
    ``charts`` spec — is checked BEFORE any file is created, so a refused
    write leaves the filesystem untouched.

    ``style``, ``gantt``, and ``charts`` are optional and XLSX-only: omitting
    all three writes exactly what this function has always written, and a CSV
    target combined with any of them is refused rather than silently ignored.

    The commit is atomic and, with ``overwrite=False``, cannot clobber a target
    that appeared after validation: the file is staged in the target's own
    directory and published with ``os.link``, which fails if the name already
    exists. ``overwrite=True`` commits with ``os.replace`` instead. The staged
    file is removed on every path, and its removal is best-effort: it never
    overrides the outcome (success or a translated error) of the write itself.
    The staging name comes from ``tempfile.mkstemp`` rather than the target
    name, so it stays short regardless of how long the target's own filename
    is.
    """
    resolved = validate_tabular_write_path(target_path)
    fmt = _format_for(resolved)
    _validate_cells(headers, rows)
    resolved_style: ResolvedStyle | None = None
    resolved_gantt: ResolvedGantt | None = None
    resolved_charts: list[ResolvedChart] = []
    if fmt == "csv":
        _reject_csv_formulas(headers, rows)
        _reject_csv_presentation(resolved, style=style, gantt=gantt, charts=charts)
    else:
        _reject_columnless_xlsx(headers)
        _reject_oversized_xlsx_strings(headers, rows)
        _reject_xlsx_empty_strings(rows)
        _reject_lossy_xlsx_numbers(rows)
        _reject_illegal_xlsx_characters(headers, rows)
        _reject_xlsx_carriage_returns(headers, rows)
        if style is not None:
            resolved_style = resolve_style(headers, rows, style)
        if gantt is not None:
            resolved_gantt = resolve_gantt(headers, rows, gantt)
        if charts is not None:
            resolved_charts = resolve_charts(headers, rows, charts)
        _reject_colliding_diagram_sheets(resolved_gantt, resolved_charts)

    # Diagnostic only — it gives a clear early refusal, but it is NOT the
    # no-clobber gate: the file could still appear between here and the commit.
    # os.link below is the authoritative gate.
    if resolved.exists() and not overwrite:
        raise ValueError(
            f"refusing to overwrite the existing file at {resolved}; "
            f"pass overwrite=true to replace it."
        )

    try:
        fd, staging_name = tempfile.mkstemp(dir=resolved.parent, prefix=".tabular-staging-")
        os.close(fd)
    except OSError as exc:
        # e.g. a permission-denied staging directory. Same translation as the
        # write failure below — nothing has been created yet.
        raise ValueError(f"cannot write {resolved}: {exc}") from exc
    staging = Path(staging_name)
    sheets_written: list[str] = []
    diagrams_written: list[str] = []
    try:
        try:
            if fmt == "csv":
                _write_csv(staging, headers, rows)
            elif resolved_style is None and resolved_gantt is None and not resolved_charts:
                _write_xlsx(staging, headers, rows)
                sheets_written = [WRITE_SHEET_NAME]
            else:
                # One workbook, mutated by each requested addition and saved
                # once. create_sheet does not move workbook.active, so the
                # data sheet stays the one a default read returns.
                workbook = build_xlsx_workbook(headers, rows)
                sheets_written = [WRITE_SHEET_NAME]
                if resolved_style is not None:
                    apply_style(workbook[WRITE_SHEET_NAME], resolved_style, rows)
                if resolved_gantt is not None:
                    sheets_written.append(render_gantt(workbook, resolved_gantt))
                    diagrams_written.append("gantt")
                if resolved_charts:
                    sheets_written.extend(render_charts(workbook, resolved_charts))
                    diagrams_written.extend(f"chart:{chart.kind}" for chart in resolved_charts)
                workbook.save(staging)
        except OSError as exc:
            # e.g. a permission-denied staging directory or a full disk. Not the
            # no-clobber race the os.replace/os.link handling below guards
            # against — this is the write itself failing — but it needs the
            # same translation to reach the tools' ValueError-only boundary
            # instead of escaping as a raw OSError.
            raise ValueError(f"cannot write {resolved}: {exc}") from exc

        try:
            # Hashed here — the staged bytes, before publish — rather than
            # reading ``resolved`` back after the commit: a read failure (e.g.
            # a hostile umask leaving the staged file unreadable) then aborts
            # the whole write with a translated error instead of leaving an
            # already-published target behind that the call reports as failed.
            digest = path_sha256(staging)
        except OSError as exc:
            raise ValueError(f"cannot hash the staged write for {resolved}: {exc}") from exc

        if overwrite:
            try:
                os.replace(staging, resolved)
            except OSError as exc:
                # e.g. a permission-denied target or a filesystem that
                # rejects the replace outright. Translate it the same way as
                # the no-clobber os.link failure below rather than let it
                # escape as a raw OSError past the tools' ValueError-only
                # boundary.
                raise ValueError(f"cannot replace {resolved}: {exc}") from exc
        else:
            try:
                os.link(staging, resolved)
            except FileExistsError as exc:
                # The target was created after the check above. The existing
                # file wins and stays byte-for-byte untouched — never fall back
                # to replacing it.
                raise ValueError(
                    f"refusing to overwrite the existing file at {resolved}; "
                    f"pass overwrite=true to replace it."
                ) from exc
            except OSError as exc:
                # Some filesystems/mounts (e.g. certain network shares) reject
                # hard links entirely (EPERM/ENOTSUP), which is not the
                # no-clobber race this no-overwrite path is meant to guard
                # against. Translate it the same way rather than let it escape
                # as a raw OSError past the tools' ValueError-only boundary.
                raise ValueError(
                    f"cannot publish {resolved}: the filesystem rejected the "
                    f"no-clobber hard-link commit ({exc}); pass overwrite=true "
                    f"to write with a plain replace instead."
                ) from exc
    finally:
        # Best-effort: a failure here (e.g. the same ENAMETOOLONG/EACCES class
        # of error the commit above already guards against) must never replace
        # whatever the try block just raised, nor report a successful publish
        # as a failure.
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass

    return TabularWriteResult(
        message=f"Wrote {len(rows)} row(s) and {len(headers)} column(s) to {resolved}.",
        target_path=str(resolved),
        sha256=digest,
        format=fmt,
        rows_written=len(rows),
        # A CSV is one flat file with no sheets at all, which is why this is
        # empty there rather than naming the XLSX data sheet.
        sheets_written=sheets_written,
        diagrams_written=diagrams_written,
    )
