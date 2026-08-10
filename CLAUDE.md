# CLAUDE.md

## Version control

This repo uses jj (Jujutsu): inspect state with `jj st`, `jj diff`, `jj log`.
Describing, splitting, and creating commits are the user's call — do them only
when asked.

## Commands

```bash
just test                          # full suite; extra args pass through to pytest
just test tests/test_runner.py     # a single file
just test -k some_test             # tests matching an expression
just test -m "not slow"            # skip slow/integration tests
uv run ty check                    # type check on its own
uv run nox -s tests                # the suite across Python 3.12-3.15
```

Work is finished when `just ci` passes: it runs treefmt, then the type check,
the fast tests, and the config + schema checks. Invoke `treefmt` on its own —
no arguments, no paths — and it formats and ruff-fixes the changed files
itself.

## Comments and docstrings

 - Use plain text. No reStructuredText/Sphinx markup: never wrap identifiers in
   double backticks (write `job`, not ``` ``job`` ```), and don't use other RST
   roles or directives. These docstrings are read as source, not rendered.
 - Keep them short. Say what the function does and why; leave out what the code
   already shows. Don't restate the signature, narrate the implementation
   line-by-line, or repeat a rule that already lives at the call site or on the
   thing being called.
 - Keep each docstring self-contained. Spell out what this function does rather
   than pointing at a sibling ("same as above", "see `foo`").
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

## Architecture

`docs/adr/` holds the architecture decision records, indexed in
`docs/adr/README.md`. Read the relevant record before changing behaviour it
covers. When a change settles a decision that outlives it, add a new numbered
record and reference it from the CHANGELOG entry.
