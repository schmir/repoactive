# CLAUDE.md

## What this is

See `README.md`.

## Version control

This repo uses jj (Jujutsu), not plain git. Don't create commits unless asked.

## Commands

```bash
just test                          # run the full test suite
just test tests/test_runner.py     # run a single test file
just test -k some_test             # run tests matching an expression
just test -m "not slow"            # skip slow/integration tests
just ci                            # treefmt + ty check + fast tests + validate_config + check_schema
uv run nox -s tests                # run tests across Python 3.11-3.15
uv run ruff check                  # lint
uv run ty check                    # type check
```

After changing files, run treefmt to format them. IMPORTANT: invoke it as
exactly `treefmt` with NO arguments and NO file paths — it discovers the changed
files itself. Never pass it a filename (e.g. `treefmt path/to/file.py` is wrong).

## Comments and docstrings

 - Use plain text. No reStructuredText/Sphinx markup: never wrap identifiers in
   double backticks (write `job`, not ``` ``job`` ```), and don't use other RST
   roles or directives. These docstrings are read as source, not rendered.
 - Keep them short. Say what the function does and why; leave out what the code
   already shows. Don't restate the signature, narrate the implementation
   line-by-line, or repeat a rule that already lives at the call site or on the
   thing being called.
 - Keep the non-obvious rationale: subtle invariants, race conditions, ordering
   constraints, and the reason behind a choice. Reference the relevant ADR by
   number (e.g. `ADR 0019`) instead of re-explaining it.
 - Field/attribute comments go on a single line. If one genuinely needs more,
   the type or name probably wants rethinking first.
 - Follow the surrounding density and style; match neighbours rather than
   introducing a second convention.

## Keeping things in sync

Some files are generated from or checked against the code, and CI fails if they
drift:

 - `config-schema.json` is generated. After changing the config models, run
   `just dump-schema` to regenerate it. The `check-schema` nox session fails if
   the committed file is out of date.
 - README TOML examples are tested. `tests/test_readme.py` extracts every
   ` ```toml ` block from `README.md` and validates it against the real config
   merge logic, so config snippets in the README must stay valid.
 - `CHANGELOG.md` is maintained by hand. Add user-facing changes under the top
   `## X.Y.Z - unreleased` section; mark breaking changes with `**Breaking:**`
   and link the relevant ADR.
 - Meaningful architectural decisions get a new numbered ADR in `docs/adr/`, and
   CHANGELOG entries reference it where relevant.

## Architecture

Architecture decision records live in `docs/adr/` (see `docs/adr/README.md` for
the index).
