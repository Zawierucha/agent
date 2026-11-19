---
name: agent-development
description: Design, build, test, and review Hermes skills and tools.
version: 0.1.0
author: Piotr Zawierucha, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [skill-development, tool-development, engineering, quality-assurance, meta]
    category: software-development
---

# Agent Development

Professional workflow for designing, implementing, testing, and reviewing
Hermes skills and custom tools in this repository. Domain-agnostic: it governs
how any future skill or tool is built, including IBD pipeline components.
It contains no biology rules — only engineering standards.

## When to Use

- Creating a new skill under `.agents/skills/<name>/`
- Analyzing, modifying, refactoring, or reviewing an existing skill
- Creating or extending a custom tool under `.agents/tools/`
- Adding tests for project skills or tools
- Assessing change quality and detecting regressions before a change is accepted

Don't use for:

- Running the IBD data pipeline itself — use the domain skills
- Personal skills outside this repo — `skill_manage(action='create')` targets
  `~/.hermes/skills/`, never this tree

## Hard Rules

1. All custom skills, tools, tests, and scripts live in this repository and are
   versioned by Git. Never write project artifacts under `/opt/data`,
   `/opt/hermes`, or any Hermes configuration directory.
2. Create new skills with `write_file` into `.agents/skills/<name>/`;
   `skill_manage(action='create')` cannot target this repo tree.
3. Surgical Changes: modify only files in scope. Never edit unrelated
   skills or tools to make checks pass; never delete or weaken existing tests
   to get a green run.
4. No `git commit` or `git push` unless the user explicitly asks.
5. Do not add dependencies or change project configuration (`pyproject.toml`,
   pre-commit, CI) without an explicit user decision — propose them separately.

## Workflow

Scale effort to the task: a wording fix is Understand → Implement → Report;
a new skill or tool runs all seven phases.

1. **Understand** — read the requirement and every existing file in scope
   (`read_file`, `search_files`); run `git status` first and record
   pre-existing unrelated changes so they stay untouched. Done when: affected
   files, current behavior, and constraints are listed.
2. **Plan** — state scope, acceptance criteria, and explicit out-of-scope
   items. Done when: every criterion is checkable.
3. **Design** — pick the simplest justified solution
   (`references/engineering-principles.md`); for tools, define the interface
   and side effects before writing code (`references/tool-engineering.md`).
   Done when: the design fits in a few sentences — more ceremony than the
   problem needs means redesign.
4. **Implement** — write or modify in-scope files starting from `templates/`;
   small focused functions, type hints, explicit validation at boundaries.
   Done when: each acceptance criterion is addressed.
5. **Test** — new functionality gets new tests; bug fixes get a regression
   test that failed before the fix; edge cases are covered. Run the existing
   suite and report real pytest output. Done when: pass/fail is backed by an
   executed command, never by assumption.
6. **Review** — run `scripts/validate_skill.py` on every skill touched; run
   Ruff on changed Python files; re-read `git diff` and confirm every hunk is
   in scope (`references/quality-assurance.md`). Done when: checks pass and
   the diff contains only intended files.
7. **Report** — what changed, which checks ran with actual results, what was
   not run and why, limitations, open decisions. Done when: every claim in
   the report traces back to tool output.

## Delegation

Route work per `references/delegation.md`. Summary:

- Skill prose edits and small, low-risk fixes: main model, no delegation.
- Complex implementation, architecture design, refactors, and new tools:
  `delegate_task` with `option="developer"` (configured
  `openai/gpt-6-luna`, reasoning high — verify the current mapping in the
  `delegate_task` option list). Never add new models or delegation routes.
- Every delegated brief must demand a report of the model actually used and
  of real command outputs; the parent re-verifies diff and tests before
  relaying the child's claims.

## Files

| Path | Purpose |
|---|---|
| `references/engineering-principles.md` | Coding standards and their sources |
| `references/skill-engineering.md` | Skill design, evaluation, regression process |
| `references/tool-engineering.md` | Tool interface, safety, testability rules |
| `references/quality-assurance.md` | Checkers available here and the gate matrix |
| `references/delegation.md` | Routing table, brief contents, verification |
| `templates/SKILL_TEMPLATE.md` | Starter for a new skill |
| `templates/tool_template.py` | Starter for a new CLI tool |
| `templates/test_template.py` | Starter for a new pytest file |
| `scripts/validate_skill.py` | Structural validation of skill directories |

## Pitfalls

1. **Expecting the live session to see a new skill.** The skill loader is
   cached per session; `skill_view`/`skills_list` pick up new skills only in a
   new session. Validate the new skill with `scripts/validate_skill.py`
   instead of waiting for registration.
2. **Using `skill_manage(action='create')` for a repo skill.** It writes to
   the user profile directory, not `.agents/skills/`. Use `write_file`.
3. **Importing the upstream hermes-agent authoring rules wholesale.** Bundled
   vs optional tiers and the docs generator apply to the hermes-agent repo,
   not here; project skills are flat under `.agents/skills/<name>/`.
4. **Growing SKILL.md to hold everything.** Move detail into the skill's
   references, templates, and scripts subdirectories; a loaded skill body
   pays token cost on every step of the task that uses it.
5. **Running checks with the wrong interpreter.** Use `uv run python ...` /
   `uv run pytest ...` so the project venv and `pythonpath` resolve; Ruff and
   Mypy are not installed in the venv — see `references/quality-assurance.md`.
6. **Declaring tests green without running them.** Paste counts from real
   output; if a suite fails for pre-existing reasons, report the baseline
   honestly instead of hiding it.

## Verification

- [ ] `uv run python .agents/skills/agent-development/scripts/validate_skill.py <skill-dir>` → exit 0 for every skill touched
- [ ] `uv run pytest tests -q` → no new failures versus the recorded baseline
- [ ] `uvx ruff check <changed python files>` → clean
- [ ] `git status` / `git diff --stat` → only in-scope files changed; no pre-existing IBD modifications touched
- [ ] Final report lists real check results, model used when delegated, and open decisions
