# Quality Assurance

Checker status in this repository (audited 2026-11, re-audit before relying
on it — run the probe commands):

| Checker | Status | How to run |
|---|---|---|
| pytest 9.x | installed in `.venv` | `uv run pytest tests -q` |
| Ruff | not in `.venv`; ephemeral via uvx (verified `uvx ruff` 0.17.5) | `uvx ruff check .agents tests --output-format concise` |
| Mypy / Pyright | not installed | do not run silently; propose first (see below) |
| pre-commit | no config in repo | not used |
| skill structure validator | shipped here | `uv run python .agents/skills/agent-development/scripts/validate_skill.py <dir>` |

Rules:

- Never install a package into the project or edit `pyproject.toml` to add a
  dev dependency or config without the user's explicit decision. Proposals
  (e.g. `[tool.ruff]` block, adding ruff/mypy to the dev group, a
  pre-commit config) go in the final report as separate, justified options.
- Always use `uv run ...` so the project venv and pytest `pythonpath` apply.

## Gate Matrix

| Change type | Required gates |
|---|---|
| New tool / module | pytest (unit+integration incl. edge cases), Ruff on changed files, validate_skill.py if a skill references it |
| Tool behavior change | new/updated tests first, full `tests/` run, Ruff, consumer skills updated |
| Bug fix | regression test that failed before the fix, full suite run |
| New skill | validate_skill.py, EVAL cases run and recorded, Ruff N/A for prose |
| Skill edit | validate_skill.py, re-run EVAL cases the edit touches |
| Test-only change | full suite run, Ruff on test files |

## Baseline Discipline (regression detection)

1. Before changing anything: `git status` (record dirty files that are not
   yours) and `uv run pytest tests -q` (record pass/fail counts as the
   baseline — this repo carries work-in-progress tests, so pre-existing
   failures are possible and must be reported, not fixed silently).
2. After changes: rerun; the result must be no new failures and any fixed
   failure explained.
3. `git diff --stat` must list only in-scope files.

## Reporting Real Results

The final report states, per gate: command run, exit code, counts
(e.g. `142 passed, 3 failed — 3 failures pre-existing at baseline`).
A gate not run is listed with the reason (e.g. "Mypy not installed — type
correctness covered by explicit annotations and test review only").
Never describe an unexecuted check as passed.
