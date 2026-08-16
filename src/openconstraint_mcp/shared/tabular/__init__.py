"""Bounded, local tabular I/O for ``.xlsx`` and ``.csv`` files.

Mechanical I/O only: this package moves scalars between a file and a
``TabularData``/``TabularWriteResult`` model. It never infers what a column
*means*, never evaluates a formula, and never calls out to the network or a
subprocess. Interpreting columns is the client's job.

``core`` orchestrates; every other module is a single-purpose leaf. This marker
re-exports nothing — callers import the submodule they need directly.

Dependencies: stdlib + openpyxl (lazily, only where actually used) +
``schemas.tabular`` + ``shared.hashing`` + ``shared.path_checks``.
Deliberately does not import ``shared.save_target``: a single output file has
no manifest and no managed-directory policy, only the low-level "absolute,
right kind, parent exists" shape that ``shared.path_checks`` factors out.
"""
