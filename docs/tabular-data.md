# Tabular data I/O (Excel/CSV)

Real problem data usually arrives as a spreadsheet, and the answer usually has
to go back as one. Two backend-agnostic tools move scalars between local
`.xlsx`/`.csv` files and MCP — feeding either solving path:

- **`load_tabular_data(path, sheet=None, has_header=True, row_offset=0, max_rows=1000)`**
  → `TabularData` (`headers`, `rows`, `sheet_name`, `available_sheets`,
  `row_offset`, `next_row_offset`, `total_rows`, `truncated`,
  `truncation_reason`).
- **`write_tabular_result(headers, rows, target_path, overwrite=False, style=None, gantt=None, charts=None)`**
  → `TabularWriteResult` (`status`, `message`, `target_path`, `sha256`,
  `format`, `rows_written`, `sheets_written`, `diagrams_written`).

The server performs **mechanical I/O only** — it never infers what a column
*means*. Interpreting columns and building `.dzn` data or CP-SAT structures is
the client LLM's job: **LLM proposes, server verifies**, the same division of
labour as the solving tools.

## The cell contract

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

## Pagination and the response ceiling

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

## Formula safety

The server never emits executable spreadsheet code. XLSX stores every string as
an explicit **string cell**, so `"=1+1"` is written and read back as the literal
text `=1+1`.

A CSV field cannot encode "this is literal text", so a CSV write **rejects** any
string whose first non-whitespace character is `=`, `+`, `-`, or `@`. Note this
also rejects a **number sent as a string**: send `-5` as the numeric cell `-5`,
not the string `"-5"` — or write `.xlsx`, which stores the text literally. There
is no opt-in formula path.

An XLSX cell string is capped at Excel's 32,767 characters; a longer one is
rejected rather than silently truncated. The XLSX **data sheet** is always named
`Sheet1` and stays the workbook's active sheet; only the presentation options
below add further sheets beside it, and every string they write — task labels,
lane names, a Gantt title — is stored as an explicit string cell too.

## XLSX round-trip hazards

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

These restrictions are not limited to data cells. The illegal-character and
carriage-return rejections also cover every string the presentation options
below write — a `gantt`/`charts` `sheet_name` or `title`, and a
`columns[*].number_format` code — because all of them reach the same XML
writer. A `gantt.title` lands in a real cell, so it additionally takes the
32,767-character cap and the empty-string rejection (omit `title` rather than
send `""`); a chart `title` is chart rich text, not a cell, and takes neither.
A chart also needs at least one data row to plot: `charts` over a zero-row
table is rejected rather than written with an empty series.

A malformed or corrupt XLSX file (not a valid zip, or missing the parts an
XLSX workbook requires) is reported as an `invalid_request` diagnostic on
read, not a raw parser crash.

## The overwrite contract

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

## Presentation: styling, Gantt, and charts

`style`, `gantt`, and `charts` are **optional and XLSX-only**. Omit all three and
the output is exactly the plain table described above — byte-for-byte the same
content as before these options existed. A `.csv` target combined with any of
them is **rejected**, never silently ignored: a CSV has no sheets, no cell
formatting, and no charts.

Every rejection below happens *before* the staging file is created, so a refused
styled write leaves the filesystem untouched, exactly like every other rejection.
Columns are named by their **header string** in all three options; a duplicated
header is ambiguous and rejected rather than resolved to the first match.

### `style` — the polished preset

`TableStyle(preset="polished", columns={})` formats the data sheet: bold, filled
header row, `freeze_panes` on row 2, an auto filter over the used range, banded
alternate rows, and a per-column width fitted to the widest rendered cell
(clamped to 8–60 characters). The preset is fixed — this is a presentation
switch, not a style DSL.

`columns` maps a header name to `ColumnStyle(number_format=None, width=None)`:
`number_format` is an openpyxl format code applied to that column's **data**
cells (not its header), and `width` (1–255) overrides the fitted width. Styling
never touches a cell's value or type, so the round-trip guarantees above still
hold for a styled workbook — with one format code **rejected** to keep them
holding: a *date* `number_format` (`yyyy-mm-dd`, `hh:mm:ss`, …) makes the reader
hand back a `datetime`, which `load_tabular_data` renders as an ISO-8601 string,
turning a numeric column into text. Numeric and text codes (`0.00`, `#,##0`,
`0%`, `@`, `General`) are display-only and accepted.

### `gantt` — a cell-grid timeline sheet

`GanttSpec(task_column, start_column, end_column=None, duration_column=None,
lane_column=None, sheet_name="Gantt", title=None)` adds one sheet whose column A
holds task labels and whose remaining columns are one discrete time unit each,
numbered from 0. A task fills the columns `[start, end)`.

- Times are **discrete non-negative integers** — the native shape of CP-SAT and
  MiniZinc scheduling output. A float, a numeric string, or `null` is rejected
  naming the offending row, never coerced.
- Exactly one of `end_column` (an absolute end) or `duration_column` (a length)
  must be given. Either way a task must span **at least one time unit**: a
  duration below 1, and an end not strictly after its start, are both rejected.
- The grid is capped at **512 time columns**; a wider schedule is rejected with
  the computed horizon in the message. The cap is fixed, not configurable.
- `lane_column` colours tasks by lane from a validated categorical palette and
  adds a lane → colour legend below the grid, so identity never rests on colour
  alone. Lanes past the eighth reuse the palette from the start.
- `title` is free text (not a column reference): it lands in `A1` and shifts the
  grid down one row.
- A `null` task label renders as `(untitled task)` and a `null` lane as
  `(no lane)` — a blank cell would read back as null and leave a legend swatch
  unnamed, putting lane identity back on colour alone.

No cell on the sheet is ever merged — the read path exposes only a merge's
top-left value, so a merge would silently lose data.

### `charts` — bar, line, and scatter

Each `ChartSpec(kind, x_column, y_columns, title=None, sheet_name="Charts")`
adds one native chart plotted from the **data sheet's own columns** — no hidden
helper range, no synthetic series. The header row supplies the series titles and
`title` becomes the chart object's own title.

Every `y_columns` column must hold numbers; for `kind="scatter"` the `x_column`
must too, because openpyxl's scatter chart uses a numeric x-axis (bar and line
treat `x_column` as plain category labels). Specs sharing an identical
`sheet_name` stack on that one sheet, spaced vertically; two names differing only
by case are rejected, since openpyxl matches sheet titles case-insensitively.

A chart sheet holds drawings and no rows, so `load_tabular_data` pointed at it
returns **zero rows** — read `Sheet1` for the data.

### Sheet names and the result

A diagram `sheet_name` must be non-empty, at most 31 characters, free of
`[]:*?/\`, and must not collide — case-insensitively — with `Sheet1`. Charts
naming the *identical* `sheet_name` deliberately share that one sheet; what is
rejected as ambiguous is a pair of names differing only by case, and a
`gantt.sheet_name` matching a `charts` sheet name case-insensitively.

The result reports what was written: `sheets_written` lists every sheet, data
sheet first (empty for a CSV, which has none), and `diagrams_written` holds one
token per rendered diagram in render order — `gantt`, `chart:bar`, `chart:line`,
`chart:scatter` — with duplicates preserved, so its length is the number of
diagrams. Styling is not a diagram and contributes no token.

## Known limits

Reads take a formula cell's **cached** result (`data_only`) — the server never
evaluates a formula, so an uncalculated one reads as `null`. A merged cell
exposes its value only in the top-left position; the rest read blank. No `.ods`,
no `pandas`, and no multi-sheet **data** writes — a write always puts every row
on `Sheet1`; the extra sheets `gantt`/`charts` add are diagrams, not data. Both
tools are local-only: no network, no telemetry, no subprocess, and no
managed-runtime dependency.
