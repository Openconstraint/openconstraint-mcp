# Tabular data I/O (Excel/CSV)

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
rejected rather than silently truncated. XLSX writes a single sheet named
`Sheet1`.

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

## Known limits

Reads take a formula cell's **cached** result (`data_only`) — the server never
evaluates a formula, so an uncalculated one reads as `null`. A merged cell
exposes its value only in the top-left position; the rest read blank. No `.ods`,
no `pandas`, no multi-sheet writes. Both tools are local-only: no network, no
telemetry, no subprocess, and no managed-runtime dependency.
