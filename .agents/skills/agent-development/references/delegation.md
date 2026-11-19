# Delegation Model

Uses Hermes' existing `delegate_task` configuration only. Do not create new
models, providers, or routing; if the option list has changed, adapt to what
`delegate_task` actually exposes and say so in the report.

## Routing

| Work | Route |
|---|---|
| Skill prose edits, wording, small frontmatter fixes | main model, no delegation |
| Tiny script tweaks, test additions under ~50 lines | main model |
| New tool modules, architecture/design work, refactors, nontrivial debugging, test-suite design | `delegate_task` with `option="developer"` |
| Several independent workstreams | parallel `developer` children (one task each) |

`developer` is configured in this deployment on `openai/gpt-6-luna`, reasoning
high (verify against the current option description).

## Briefing a Developer Child

Children start with zero context — the brief must be self-contained:

- Goal and acceptance criteria ("Done when:" list).
- Exact in-scope paths and the repo conventions that apply:
  `.agents/skills/` layout, `click` CLI + JSON stdout, full type hints,
  `from __future__ import annotations`, tests under `tests/` importing via
  `pythonpath=[".agents"]`, no new dependencies, no config edits.
- Out-of-scope list, including pre-existing dirty files from `git status`
  that must remain untouched.
- Mandatory reporting: the model actually used, files changed, exact test
  and lint outputs. A summary claiming "tests pass" without pasted output is
  treated as unverified.
- `output_schema` when the return feeds further automation.

## Verifying Delegated Work (parent duty)

Child summaries are self-reports, not facts. Before relaying results:

1. `git status` / `git diff --stat` — scope check against the brief.
2. Read the changed code (Review dimensions in
   `engineering-principles.md`).
3. Re-run the gates yourself: `uv run pytest tests -q`, `uvx ruff check
   <files>`, `scripts/validate_skill.py` for touched skills.
4. Only then report — with the child's claimed model and your own verified
   results, noting any discrepancy.

## Boundaries

- Children cannot ask the user questions or delegate further; anything
  ambiguous goes back to the user by the parent before dispatch.
- Never let a child commit, push, install dependencies, or write outside
  `/workspace`.
- Use `action="continue"` for follow-ups to keep the child's context; stop
  and re-spawn only when the direction changed materially.
