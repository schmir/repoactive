@_default: (help)

# Show available recipes
help:
    @just --list

# Run the test suite (pass extra args to pytest, e.g. just test tests/test_runner.py)
[positional-arguments]
test *args:
    uv run pytest "$@"

# Run CI checks: prek, type check, tests
ci:
    prek run --all-files
    uv run ty check
    uv run pytest

# Install repoactive in editable mode
dev:
    uv tool install -e .

# Build the repoactive Docker image
docker-build:
    docker build -t repoactive .
