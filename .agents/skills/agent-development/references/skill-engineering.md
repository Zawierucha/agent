# Skill Engineering

Process for designing, evaluating, and regressing project skills under
`.agents/skills/<name>/`. Method adapted from Anthropic's skill-creator
workflow (skills are software: specify, implement, evaluate, iterate) and the
hermes-agent authoring standards, scoped to this repository's layout.

## 1. Analyze Purpose and Scope

Before writing anything, answer in writing:

- One-sentence purpose: what does this skill make the agent do reliably?
- Explicit out-of-scope list: what must the agent NOT do with this skill?
  (Project convention: every sc-dataset skill states a boundary, e.g.
  "candidate discovery only, no biological qualification".)
- Single responsibility: if the answer needs "and", split into two skills or
  a skill + a tool.

A skill earns its existence only if it removes process variance on real,
recurring tasks. If an agent already does the task reliably without it, do
not write it.

## 2. Identify Users and Representative Tasks

- Users: which agent sessions run this skill (pipeline stage runs, ad-hoc
  analyses, maintenance)?
- Enumerate 3–6 representative tasks as concrete prompts (e.g. "inspect
  GSE12345 downloads and report candidate 10x bundles").
- These tasks become the evaluation set (section 6).

## 3. Design the Description (the Trigger)

- ≤ 60 characters, one sentence, ends with a period, states capability not
  implementation, no marketing words.
- The system-prompt index truncates around 57 chars — the trigger words must
  survive truncation.
- The description is paid in every session's context; detail belongs in the
  body, and body detail belongs in references.

## 4. Design the Body

Start from `templates/SKILL_TEMPLATE.md`. Minimum sections: When to Use
(+ counter-triggers), Goal/Scope restrictions, Tool invocation, Workflow,
Output, Pitfalls, Verification. Rules:

- Numbered steps, each ending with a checkable completion criterion.
- Reference Hermes tools in backticks (`terminal`, `read_file`, `patch`,
  `delegate_task`); frame commands as `uv run python .agents/tools/...`
  invocations through `terminal`.
- Repo-relative paths only; no machine-local absolute paths.
- Boundaries as prohibitions where history shows drift ("Do not rerun GEO
  search", "Do not write to DuckDB") — one line each, no essays.
- Report evidence and uncertainty; never let the skill claim validation it
  did not perform.

## 5. Token Discipline (progressive disclosure)

Three paid layers:

1. Frontmatter description — always in context. Keep minimal.
2. SKILL.md body — loaded when the skill triggers. Target ≤ ~150 lines;
   complex skills ≤ ~200.
3. `references/`, `templates/`, `scripts/` — loaded only on demand. Move any
   content that is not needed on every use of the skill.

Cut any line that does not change agent behavior. When adding a rule, remove
or merge the older wording it replaces — skills accumulate sediment.

## 6. Define Correct Behavior and Evaluate

Acceptance criteria per representative task, written before testing:

- **Follows scope** — performs the stage, stops at the boundary.
- **Uses the right tool** — the designated script, correct flags.
- **Reports honestly** — counts and claims come from executed output;
  uncertainty is surfaced.

Run each task against a fresh agent (clean context — `delegate_task` children
are the natural harness; `hermes chat -q` for an end-to-end check). Record
results in the skill's `references/evals.md` using the case format below.
Fix the skill wording, not the evaluation, until behavior is stable.

```
### EVAL-<skill>-NN
Task: <prompt run verbatim>
Expected: <observable behaviors>
Forbidden: <behaviors that fail the case>
Last run: <YYYY-MM-DD — pass/fail, one-line note>
```

## 7. Trigger Failure Analysis

- **Should-not-load (false positive):** agent loads the skill for unrelated
  work → tighten description and When to Use; add counter-triggers.
- **Should-load (false negative):** agent does the task without loading the
  skill → put the task's natural-language keywords into the description; do
  not fix this by duplicating the guidance into other skills.

## 8. Regression After Changes

- After any edit: re-run `scripts/validate_skill.py` on the skill, re-run all
  EVAL cases touched by the change, and re-run the repo pytest suite if a
  tool interface changed.
- Version bump: patch for wording/fixes, minor for new procedures, major for
  changed boundaries. Keep frontmatter `version` in sync with the paired
  tool's contract changes.
- A behavior that was once wrong and is now fixed gets a named EVAL case —
  that is the skill's regression test.

## Repository Conventions

- Flat layout: `.agents/skills/<name>/SKILL.md` (no category subdirs here).
- Frontmatter: `name`, `description`, `version`, `author` (human first),
  `license`, `platforms`, `metadata.hermes.tags`.
- `.rollback-broken/` suffixed directories are frozen historical copies kept
  for reference — never load, edit, or import from them.
- No router/index skills whose only content points at sibling skills.
