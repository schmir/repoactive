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
    uv run nox -s check_schema

# Run pyright (bundled node.js) over src and tests
pyright:
    uvx 'pyright[nodejs]'

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
    nix flake update
