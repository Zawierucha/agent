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

- Do not mix contradictory constraints in one brief (e.g. "preserve existing
  tests verbatim" plus "fix any defect in these files"). State deliberately
  which relaxations apply, and if children may strengthen existing tests,
  require each changed test to remain a superset of the original assertions
  and have the parent verify the changed-line region function by function.
- Runtime `Model:` metadata on a child result is authoritative over the
  child's self-report for routing; a mismatch means the brief's work was done
  on the wrong route and the responsible phase must be re-dispatched.

## Verifying Delegated Work (parent duty)

Child summaries are self-reports, not facts. Before relaying results:

1. `git status` / `git diff --stat` — scope check against the brief.
2. Read the changed code (Review dimensions in
   `engineering-principles.md`).
3. Re-run the gates yourself: `uv run pytest tests -q`, `uvx ruff check
   <files>`, `scripts/validate_skill.py` for touched skills.
4. Only then report — with the child's claimed model and your own verified
   results, noting any discrepancy.

## Delegation Contract (mandatory gates)

Route selection is real only when validated structurally before dispatch and
confirmed from runtime metadata after completion. The gates apply to EVERY
`delegate_task` call, any action included:

1. **Preflight (before every call):** compose the exact payload JSON, run
   `uv run python .agents/skills/agent-development/scripts/delegation_check.py
   preflight --options developer --payload <file|->`. Exit != 0 means the call
   would silently route wrong; never dispatch unvalidated. Preflight enforces:
   no top-level `option`/shorthand keys, `tasks[]` shape, `option` present on
   every task when used, option values within the configured set, and ONE
   route per call — a mixed-option fan-out is guaranteed to deadlock
   postcheck, so split routes into separate calls.
2. **Postcheck (before accepting results):** run
   `delegation_check.py postcheck --route <name> --text <batch notification>`
   on the completion message; the authoritative signal is the runtime
   `Role: ... Model: <id>` metadata, never the child's self-report. No Model
   line, or a mismatch => the phase is NOT delegated: report it, stop the
   child, and re-dispatch a FRESH spawn (preflight again). Never
   `action="continue"` a child that ran on the wrong route — continue inherits
   the child's route and preflight cannot repair it.

Scenario coverage:

- **Single delegation:** one call, one task, `option` inside `tasks[0]`; both
  gates apply.
- **Parallel same-route fan-out:** allowed; postcheck requires EVERY `Model:`
  line in the batch to equal the route model.
- **Different subagents (routes):** one `delegate_task` call per route;
  preflight rejects mixed options; postcheck each notification against its own
  route.
- **continue/steer/stop:** preflight (warns that tasks/option are ignored);
  after the child's new result, postcheck against the ORIGINAL route.
- **Re-dispatch after a mismatch:** stop child -> fresh spawn -> preflight ->
  postcheck. Partial wrong-route output must not be accepted as the delegated
  phase.

Manifest governance:

- `templates/delegation_manifest.json` records route -> model ONLY from
  runtime `Model:` evidence of a real dispatch (pass the notification line as
  `--evidence`). The agent must NEVER assume the host `model_map` matches the
  manifest, and must never record a model from belief.
- A postcheck mismatch is first a host-config question for the user. Only
  after the user confirms the new mapping:
  `manifest record --route <name> --model <id> --evidence <Model line>`.

## Boundaries

- Children cannot ask the user questions or delegate further; anything
  ambiguous goes back to the user by the parent before dispatch.
- Never let a child commit, push, install dependencies, or write outside
  `/workspace`.
- Use `action="continue"` for follow-ups to keep the child's context; stop
  and re-spawn only when the direction changed materially.
