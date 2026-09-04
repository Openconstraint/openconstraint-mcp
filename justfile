# justfile for openconstraint-mcp
# All Python work goes through `uv` — never raw `python` or `pip`.

# Default: list available recipes.
default: list

# Show all recipes.
list:
    @just --list

# Sync dependencies, including dev group.
sync:
    uv sync --all-groups

# Run the MCP server over stdio.
run:
    uv run openconstraint-mcp stdio

# Run the CLI with arbitrary args, e.g. `just cli check-runtime`.
cli *args:
    uv run openconstraint-mcp {{args}}

# Run the test suite (exit 5 "no tests collected" tolerated until v0 skeleton lands).
test:
    @uv run pytest -ra || [ "$?" = "5" ]

# Run unit tests with branch coverage and report missed lines.
coverage:
    uv run coverage run --branch --source=src/openconstraint_mcp -m pytest
    uv run coverage report --show-missing

# Run pytest with arbitrary args, e.g. `just pytest tests/test_cli.py::test_help_lists_all_commands -v`.
pytest *args:
    uv run pytest {{args}}

# Run integration tests using real subprocesses, platform behavior, or a managed runtime.
integration:
    uv run pytest -m integration -v

# Lint the source tree with ruff.
lint:
    uv run ruff check .

# Auto-format with ruff: apply lint autofixes (e.g. import sorting), then format.
format:
    uv run ruff check --fix .
    uv run ruff format .

# Type-check the package source and the example scripts.
typecheck:
    uv run mypy src examples

# Full local gate: lint + typecheck + test.
check: lint typecheck test

# Build the sdist and wheel into dist/.
build:
    uv build

# Stage explicit files, commit, and push the current branch.
# Usage: just push "commit message" path/one path/two ...
# Safer default — only the listed files end up staged.
push msg +files:
    git add {{files}}
    git commit -m {{quote(msg)}}
    git push -u origin HEAD

# Stage *all* changes (incl. untracked), commit, and push the current branch.
# Usage: just push-all "commit message"
# Convenience for fully-trusted working trees — risks staging .env/secrets.
push-all msg:
    git add -A
    git commit -m {{quote(msg)}}
    git push -u origin HEAD

# Squash the latest commits, retain the newest message, then safely force-push.
# Usage: just squash 3
# On a stacked branch, pass the branch it sits on: just squash 3 origin/feat-a
squash commits base="origin/master":
    @test "{{commits}}" -ge 2
    @git diff --quiet
    @git diff --cached --quiet
    @test -n "$(git branch --show-current)"
    @test "$(git branch --show-current)" != master
    @test "$(git branch --show-current)" != main
    # Never reach past this branch's own commits: without this, `just squash 10`
    # on a 3-commit branch would rewrite shared history behind the force-push,
    # which --force-with-lease cannot catch because the lease is on the branch
    # ref, not on the base's. Counting must be --first-parent to match how
    # `reset --soft HEAD~N` walks; a plain count over-counts across merges.
    @test "{{commits}}" -le "$(git rev-list --first-parent --count {{base}}..HEAD)"
    git reset --soft HEAD~{{commits}}
    git commit --reuse-message=ORIG_HEAD
    git push --force-with-lease

# Remove caches and build artefacts.
clean:
    rm -rf .pytest_cache .ruff_cache .mypy_cache build dist .coverage
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
