@_default: (help)

# Show available recipes
help:
    @just --list

# Run the test suite (pass extra args to pytest, e.g. just test tests/test_runner.py)
[positional-arguments]
test *args:
    uv run pytest "$@"

# Run quick CI checks: treefmt, type check, tests
ci:
    treefmt
    # scripts/prek.sh run --all-files
    uv run ty check
    uv run pytest -m "not slow"
    uv run nox -s validate_config

# Install repoactive in editable mode
dev:
    uv tool install -e .

# Build the repoactive Docker image
docker-build:
    docker build -t repoactive .
