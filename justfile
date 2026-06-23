@_default: (help)

# Show available recipes
help:
    @just --list

# Run the test suite (pass extra args to pytest, e.g. just test tests/test_runner.py)
[positional-arguments]
test *args:
    uv run pytest "$@"

# Run the test suite and collect coverage data (pass extra args to pytest)
[positional-arguments]
coverage *args:
    uv run pytest --cov=repoactive --cov-report=term-missing --cov-report=html "$@"
    @if command -v xdg-open >/dev/null 2>&1; then xdg-open htmlcov/index.html; else open htmlcov/index.html; fi

# Run quick CI checks: treefmt, type check, tests, config + schema validation
ci:
    treefmt
    # scripts/prek.sh run --all-files
    uv run ty check
    uv run pytest -m "not slow"
    uv run nox -s validate_config
    uv run nox -s check_schema

# Run pyright (from PATH if available, else bundled node.js via uvx)
pyright:
    @if command -v pyright >/dev/null 2>&1; then pyright; else uvx 'pyright[nodejs]'; fi

# Write the config JSON schema to config-schema.json
dump-schema:
    uv run repoactive dump-schema -o config-schema.json

# Install repoactive in editable mode
dev:
    uv tool install -e .

# Build the repoactive Docker image
docker-build:
    docker build -t repoactive .

# Update the nix flake lockfile
update-flake:
    ./scripts/update-flake.sh
