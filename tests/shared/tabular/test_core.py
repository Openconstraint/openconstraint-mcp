"""Tests for the write orchestration: reported result, staging, atomic commit."""

from __future__ import annotations

import csv
import hashlib
import os
import sys
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

from openconstraint_mcp.schemas.tabular import ChartSpec, ColumnStyle, GanttSpec, TableStyle
from openconstraint_mcp.schemas.tabular import TabularCell as Cell
from openconstraint_mcp.shared.tabular.core import read_tabular_data, write_tabular_data

# --- the plain-write baseline -----------------------------------------------------
#
# Captured from the unmodified write_tabular_data, BEFORE the style/gantt/chart
# machinery was wired into it. A raw whole-file sha256 is only reproducible for
# .csv: openpyxl stamps the save time into docProps/core.xml on every save, so
# the .xlsx side compares every OTHER zip member instead — which is exactly the
# part this feature could regress.

_BASELINE_HEADERS = ["task", "start", "duration", "lane"]
_BASELINE_ROWS: list[list[Cell]] = [["cut", 0, 3, "alpha"], ["polish", 3, 2, "beta"]]

_BASELINE_CSV_SHA256 = "62e7a1f38fd67ce64f94d756340106bd375601e0c98742f4d8fe860b82e74a7f"

_BASELINE_XLSX_MEMBERS = {
    "[Content_Types].xml": "2fd871365a885c9d2ef12c9df3bcb523737614c8ba8581c36cd462abb5e74635",
    "_rels/.rels": "ec869ae44fc833c25e58afa6ae766147aa72a99147522f6070e511475222827d",
    "docProps/app.xml": "209fca6b00afe72a5029754b94be5953d8f16d96f67130325566b9366ad4ccc5",
    "xl/_rels/workbook.xml.rels": (
        "8b55e81b6390446109e3c54f68f9ed0922fea7fc7c805eb84eb23523bb638a0e"
    ),
    "xl/styles.xml": "c7b12b9e3bb2ee4e41fde2c6b76e18c100aa411d8d8cdcba5ca7b75735cb3717",
    "xl/theme/theme1.xml": "d15e8ebf78ef7b9720839d7ae8fdc81a7df5bc24706d8e137df61a5683c358d9",
    "xl/workbook.xml": "bf01fc6be7920694df91103069c7801ba60335355f432c0be5e2406aa8d64a3e",
    "xl/worksheets/sheet1.xml": "cbe5284d24c0a7022ca4a3203ecae216abd5d3c2ac0c997097e30bbad33d106b",
}


def _xlsx_members(path: Path) -> dict[str, str]:
    """Digest every zip member except the one openpyxl timestamps."""
    with zipfile.ZipFile(path) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in sorted(archive.namelist())
            if name != "docProps/core.xml"
        }


def test_a_plain_csv_write_still_produces_the_baseline_bytes(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == _BASELINE_CSV_SHA256


def test_a_plain_xlsx_write_still_produces_the_baseline_workbook(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target)
    assert _xlsx_members(target) == _BASELINE_XLSX_MEMBERS


def test_csv_write_reports_the_written_file(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    result = write_tabular_data(["a"], [["1"], ["2"]], target)
    assert (result.status, result.format, result.rows_written, result.target_path) == (
        "written",
        "csv",
        2,
        str(target),
    )


def test_csv_write_hashes_the_committed_bytes(tmp_path: Path) -> None:
    import hashlib

    target = tmp_path / "out.csv"
    result = write_tabular_data(["a"], [["1"]], target)
    assert result.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()


def test_a_rejected_write_creates_no_file(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError):
        write_tabular_data(["a", "b"], [["1"]], target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


# --- writing: the overwrite contract -------------------------------------------------


def test_write_refuses_an_existing_target_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    write_tabular_data(["a"], [["1"]], target)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_tabular_data(["a"], [["2"]], target)


def test_a_refused_overwrite_leaves_the_target_untouched(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    write_tabular_data(["a"], [["1"]], target)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        write_tabular_data(["a"], [["2"]], target)
    assert target.read_bytes() == before


def test_overwrite_replaces_the_target(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    write_tabular_data(["a"], [["1"]], target)
    write_tabular_data(["a"], [["2"]], target, overwrite=True)
    assert read_tabular_data(target).rows == [["2"]]


def test_overwrite_replace_failure_is_a_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A permission-denied target or a filesystem that rejects the replace
    # outright raises a raw OSError from os.replace — that must not escape
    # past the tools' ValueError-only translation.
    target = tmp_path / "out.csv"
    write_tabular_data(["a"], [["1"]], target)

    def rejecting_replace(*args: object, **kwargs: object) -> None:
        raise OSError("replace rejected by this filesystem")

    monkeypatch.setattr(os, "replace", rejecting_replace)
    with pytest.raises(ValueError, match="cannot replace"):
        write_tabular_data(["a"], [["2"]], target, overwrite=True)


def test_a_csv_staging_write_failure_is_a_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A permission-denied staging directory or a full disk raises a raw OSError
    # from the write itself, before os.replace/os.link ever run — that must
    # not escape past the tools' ValueError-only translation either.
    target = tmp_path / "out.csv"

    def rejecting_writer(*args: object, **kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(csv, "writer", rejecting_writer)
    with pytest.raises(ValueError, match="cannot write"):
        write_tabular_data(["a"], [["1"]], target)


def test_an_xlsx_staging_write_failure_is_a_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openpyxl import Workbook

    target = tmp_path / "out.xlsx"

    def rejecting_save(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Workbook, "save", rejecting_save)
    with pytest.raises(ValueError, match="cannot write"):
        write_tabular_data(["a"], [["1"]], target)


def test_a_target_created_after_validation_wins_over_a_no_overwrite_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The no-clobber gate must be the atomic os.link commit, not the earlier
    # existence check: a target that appears in between must survive intact.
    target = tmp_path / "out.csv"
    original = b"i was here first\n"

    real_writer = csv.writer

    def racing_writer(handle: object, *args: object, **kwargs: object) -> object:
        # Runs while the staged file is being written — i.e. after validation
        # saw no target, before the commit tries to publish one.
        if not target.exists():
            target.write_bytes(original)
        return real_writer(handle, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(csv, "writer", racing_writer)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_tabular_data(["a"], [["2"]], target)
    assert target.read_bytes() == original


def test_a_lost_no_overwrite_race_leaves_no_staged_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.csv"
    real_writer = csv.writer

    def racing_writer(handle: object, *args: object, **kwargs: object) -> object:
        if not target.exists():
            target.write_bytes(b"first\n")
        return real_writer(handle, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(csv, "writer", racing_writer)
    with pytest.raises(ValueError):
        write_tabular_data(["a"], [["2"]], target)
    assert [entry.name for entry in tmp_path.iterdir()] == ["out.csv"]


def test_a_filesystem_that_rejects_hard_links_raises_a_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Some mounts (e.g. certain network shares) reject os.link outright with
    # an OSError that is not FileExistsError (EPERM/ENOTSUP) — that must not
    # escape as a raw OSError past the tools' ValueError-only translation.
    target = tmp_path / "out.csv"

    def rejecting_link(*args: object, **kwargs: object) -> None:
        raise OSError("hard links are not supported on this filesystem")

    monkeypatch.setattr(os, "link", rejecting_link)
    with pytest.raises(ValueError, match="no-clobber hard-link commit"):
        write_tabular_data(["a"], [["1"]], target)
    assert not target.exists()


def test_a_successful_write_leaves_no_staged_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    write_tabular_data(["a"], [["1"]], target)
    assert [entry.name for entry in tmp_path.iterdir()] == ["out.csv"]


def test_a_successful_overwrite_leaves_no_staged_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    write_tabular_data(["a"], [["1"]], target)
    write_tabular_data(["a"], [["2"]], target, overwrite=True)
    assert [entry.name for entry in tmp_path.iterdir()] == ["out.xlsx"]


def test_a_hash_failure_is_a_value_error_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hash is computed from the staged file before the commit
    # (os.link/os.replace) runs — a failure there must translate to
    # ValueError like every other write-path OSError, and (unlike hashing the
    # already-published file after the commit) must leave no published target.
    # Patched on core itself: that is the module that imported path_sha256, so
    # patching the package (or shared.hashing) would leave this test vacuous.
    import openconstraint_mcp.shared.tabular.core as tabular_core_module

    target = tmp_path / "out.csv"

    def rejecting_hash(*args: object, **kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(tabular_core_module, "path_sha256", rejecting_hash)
    with pytest.raises(ValueError, match="cannot hash the staged write"):
        write_tabular_data(["a"], [["1"]], target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename length limit")
def test_write_succeeds_with_a_target_name_near_the_filesystem_limit(tmp_path: Path) -> None:
    # A staging name derived from the target name (the old scheme) could push
    # a 255-byte target name's staging name past the OS filename limit and
    # leak a raw ENAMETOOLONG; tempfile.mkstemp's short, target-independent
    # name does not depend on the target name's length at all.
    name = "x" * 251 + ".csv"
    assert len(name) == 255
    target = tmp_path / name
    result = write_tabular_data(["a"], [["1"]], target)
    assert target.exists()
    assert result.rows_written == 1


def test_a_staging_cleanup_failure_does_not_mask_a_successful_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A finally-block exception in Python replaces whatever the try block
    # already raised or returned; a cleanup failure after a successful publish
    # must not surface as a write failure.
    target = tmp_path / "out.csv"
    real_unlink = os.unlink
    calls = 0

    def flaky_unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("cleanup rejected by this filesystem")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    result = write_tabular_data(["a"], [["1"]], target)
    assert result.rows_written == 1
    assert target.read_text(encoding="utf-8").splitlines() == ["a", "1"]


# --- writing: styling and diagrams ----------------------------------------------


def _gantt() -> GanttSpec:
    return GanttSpec(task_column="task", start_column="start", duration_column="duration")


def test_csv_rejects_a_style(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="csv.*\\.xlsx"):
        write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target, style=TableStyle())


def test_csv_rejects_a_gantt(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="csv.*\\.xlsx"):
        write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target, gantt=_gantt())


def test_csv_rejects_charts(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    charts = [ChartSpec(kind="bar", x_column="task", y_columns=["duration"])]
    with pytest.raises(ValueError, match="csv.*\\.xlsx"):
        write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target, charts=charts)


def test_a_rejected_style_leaves_the_directory_untouched(tmp_path: Path) -> None:
    # Every diagram rejection happens before the staging file is created.
    target = tmp_path / "out.xlsx"
    style = TableStyle(columns={"absent": ColumnStyle(width=12)})
    with pytest.raises(ValueError):
        write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target, style=style)
    assert list(tmp_path.iterdir()) == []


def test_a_rejected_gantt_leaves_the_directory_untouched(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    rows: list[list[Cell]] = [["cut", "0", 3, "alpha"]]
    with pytest.raises(ValueError):
        write_tabular_data(_BASELINE_HEADERS, rows, target, gantt=_gantt())
    assert list(tmp_path.iterdir()) == []


def test_a_gantt_sheet_name_xlsx_cannot_store_is_rejected_before_staging(tmp_path: Path) -> None:
    # U+FFFF clears the schema's Excel-structural sheet-name rules but is not
    # an XML character: unchecked, the write "succeeds" and leaves a workbook
    # no parser can reopen.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="sheet_name"):
        write_tabular_data(
            _BASELINE_HEADERS,
            _BASELINE_ROWS,
            target,
            gantt=GanttSpec(
                task_column="task",
                start_column="start",
                duration_column="duration",
                sheet_name="Gan￿tt",
            ),
        )
    assert list(tmp_path.iterdir()) == []


def test_a_gantt_sheet_name_colliding_with_a_chart_sheet_is_rejected(tmp_path: Path) -> None:
    # Each spec validates alone, so only core sees both at once.
    target = tmp_path / "out.xlsx"
    charts = [ChartSpec(kind="bar", x_column="task", y_columns=["duration"], sheet_name="plan")]
    with pytest.raises(ValueError, match="Plan"):
        write_tabular_data(
            _BASELINE_HEADERS,
            _BASELINE_ROWS,
            target,
            gantt=GanttSpec(
                task_column="task",
                start_column="start",
                duration_column="duration",
                sheet_name="Plan",
            ),
            charts=charts,
        )


def test_a_chart_over_a_zero_row_table_is_rejected_before_staging(tmp_path: Path) -> None:
    # A chart's series would reference rows the data sheet does not have.
    target = tmp_path / "out.xlsx"
    charts = [ChartSpec(kind="bar", x_column="task", y_columns=["duration"])]
    with pytest.raises(ValueError, match="zero rows"):
        write_tabular_data(_BASELINE_HEADERS, [], target, charts=charts)
    assert list(tmp_path.iterdir()) == []


def test_a_zero_row_gantt_write_is_unaffected(tmp_path: Path) -> None:
    # Only a chart needs rows to reference; a zero-row table is fine otherwise.
    target = tmp_path / "out.xlsx"
    result = write_tabular_data(_BASELINE_HEADERS, [], target, gantt=_gantt())
    assert result.sheets_written == ["Sheet1", "Gantt"]


def test_a_zero_row_styled_write_is_unaffected(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    result = write_tabular_data(_BASELINE_HEADERS, [], target, style=TableStyle())
    assert result.status == "written"


def test_a_gantt_write_reports_both_sheets(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    result = write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target, gantt=_gantt())
    assert result.sheets_written == ["Sheet1", "Gantt"]


def test_a_gantt_and_two_charts_report_one_token_each_in_render_order(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    charts = [
        ChartSpec(kind="bar", x_column="task", y_columns=["duration"]),
        ChartSpec(kind="line", x_column="task", y_columns=["start"]),
    ]
    result = write_tabular_data(
        _BASELINE_HEADERS, _BASELINE_ROWS, target, gantt=_gantt(), charts=charts
    )
    assert result.diagrams_written == ["gantt", "chart:bar", "chart:line"]


def test_a_styled_only_write_reports_no_diagrams(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    result = write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target, style=TableStyle())
    assert result.diagrams_written == []


def test_a_csv_write_reports_no_sheets(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    result = write_tabular_data(_BASELINE_HEADERS, _BASELINE_ROWS, target)
    assert result.sheets_written == []


def test_the_data_sheet_stays_active_when_diagrams_are_added(tmp_path: Path) -> None:
    # load_tabular_data defaults to the active sheet, so an added sheet must
    # not steal it.
    target = tmp_path / "out.xlsx"
    charts = [ChartSpec(kind="bar", x_column="task", y_columns=["duration"])]
    write_tabular_data(
        _BASELINE_HEADERS, _BASELINE_ROWS, target, gantt=_gantt(), charts=charts
    )
    assert load_workbook(target).active.title == "Sheet1"


def test_a_fully_styled_workbook_round_trips_its_rows(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    charts = [ChartSpec(kind="bar", x_column="task", y_columns=["duration"])]
    write_tabular_data(
        _BASELINE_HEADERS,
        _BASELINE_ROWS,
        target,
        style=TableStyle(columns={"duration": ColumnStyle(number_format="0.00", width=12)}),
        gantt=_gantt(),
        charts=charts,
    )
    assert read_tabular_data(target).rows == _BASELINE_ROWS
