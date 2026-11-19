# Tool Engineering

Rules for custom tools under `.agents/tools/<package>/<tool>.py`. Tools are
deterministic CLIs that skills invoke; judgment belongs in skills, work in
tools. Start new tools from `templates/tool_template.py`.

## Requirements Before Interface

Write down before coding:

- Inputs (types, sources), outputs (schema, file targets), side effects
  (files written, network calls, exit codes).
- What the tool must NOT do — project convention: generic tools carry no
  domain policy (the GEO search tool contains no IBD terms; domain
  vocabulary lives in skills).
- Which existing tool or adapter could be extended instead. Forking a tool
  for a 10% difference is rejected; extending the same-responsibility module
  is the default.

## Interface Design

- Python ≥ 3.13, `from __future__ import annotations`, full type hints,
  docstring on `main()` becomes the `--help` text.
- CLI: `click` (project dependency); required options explicit, numeric
  bounds via `click.IntRange`, defaults with `show_default=True`.
- Machine-readable JSON on stdout by default; `--pretty` opt-in;
  diagnostics/errors to stderr with nonzero exit (`raise
  click.ClickException("actionable message")`).
- Structured values as dataclasses; pluggable variants as `Protocol` +
  registration modules (see `adapters/base.py`), never class inheritance
  trees.
- Idempotence: rerunning with the same inputs must not duplicate work or
  corrupt outputs (write reports to their own file; never overwrite the
  manifest or source data — existing stage convention).

## Validation and Errors

- Validate at the boundary: types and ranges via click, content
  expectations in the core with explicit `ClickException`/`ValueError`
  messages naming the offending value and the expectation.
- Fail fast; never silently coerce, skip, or guess. Unknown values are
  reported, not defaulted.
- Catch third-party exceptions narrowly and re-raise with context; no bare
  `except:`, no swallowed errors.

## Side Effects and Safety (least privilege)

- Touch only declared paths; document the default input/output roots in the
  `--help` text and refuse `--root`-style escapes for fixed-layout stages.
- Network: read-only HTTP with timeouts and bounded retries on
  403/429/5xx; no writes to external systems unless the tool's stated job is
  that.
- No shell-outs, no `eval`, no imports outside stdlib + declared project
  dependencies, no credential reading beyond explicitly passed config.
- Anything destructive (delete/overwrite of pre-existing data) requires an
  explicit opt-in flag; the default must be safe.
- Secrets never appear in code, logs, or outputs.

## Separation of Logic and Interface

- Pure core function (e.g. `search_geo(...)`) takes data/dependency
  arguments; the click `main()` only parses args, calls the core, serializes.
- Injectable seams: pass `session=requests.Session()` (or a context manager)
  so tests substitute mocks — this is the existing pattern in
  `tests/test_geo_search.py`.
- I/O (files, network, DB) lives at the edges; the middle is testable without
  a filesystem or network.

## Tests (mandatory)

- Location: `tests/test_<tool>.py`; imports resolve as `tools.<pkg>.<module>`
  via `pythonpath = [".agents"]`.
- Unit: core function happy path, empty/whitespace inputs, deduplication,
  malformed records, limits, error branches — `monkeypatch`/`Mock`, no live
  network.
- Integration: CLI wiring for nontrivial flows (tmp_path fixtures; assert on
  JSON output and exit codes).
- Every bug fix adds a regression test that failed before the fix.
- Run: `uv run pytest tests -q`; report actual counts (see
  `quality-assurance.md`).

## Documentation

- Module docstring: one-paragraph responsibility + what it must not do.
- A tool is used through its skill: when a tool's flags or output schema
  change, update the calling skill's Tool/Workflow sections in the same
  change set and bump the skill version.
- Deprecation: remove a tool only together with its skill references and
  tests — orphans fail review.
