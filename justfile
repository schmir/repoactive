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
    uv run nox -s ty tests-3.14 check-schema validate-config  -- -m 'not slow'

# Run pyright (from PATH if available, else bundled node.js via uvx)
pyright:
    @if command -v pyright >/dev/null 2>&1; then pyright; else uvx 'pyright[nodejs]'; fi

# Write the config JSON schema to config-schema.json
dump-schema:
    uv run repoactive dump-schema -o config-schema.json

# Install repoactive in editable mode
dev:
    uv tool install -e .

# Build the repoactive container image (CONTAINER_ENGINE overrides; podman preferred when present)
build-image:
    #!/usr/bin/env bash
    set -euo pipefail
    engine="${CONTAINER_ENGINE:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}"
    "$engine" build -t repoactive .
    "$engine" image ls repoactive

# Build the image and smoke-test it against a fresh clone (needs a container engine + network)
smoketest:
    uv run nox -s smoketest

# Update the flake inputs and show the resulting dev shell package changes.
flake-update-diff:
    #!/usr/bin/env bash
    set -euo pipefail
    system=$(nix eval --impure --raw --expr builtins.currentSystem)
    target=".#devShells.${system}.default"
    # Build the dev shell closure before and after updating, then diff the two.
    before=$(nix build --no-link --no-warn-dirty --print-out-paths "$target")
    # Three --quiet flags drop nix below warn level, hiding the "updating
    # lock file" notice; errors still print and still abort the recipe.
    nix flake update --no-warn-dirty --quiet --quiet --quiet
    after=$(nix build --no-link --no-warn-dirty --print-out-paths "$target")
    nix shell nixpkgs#nvd --command nvd diff "$before" "$after"

# Remove build artifacts, caches, and the virtualenv
clean:
    #!/usr/bin/env zsh
    setopt +O nullglob
    PS4="  clean: "
    set -x
    rm -rf .venv(/) dist(/) htmlcov(/) .coverage(.)
    rm -rf **/.nox **/.pytest_cache **/.ruff_cache **/.tox **/__pycache__
