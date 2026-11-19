---
name: <skill-name>                 # lowercase-hyphen; must equal the directory name
description: <Capability sentence, <=60 chars, ends with a period.>
version: 0.1.0
author: <Human Name>, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [<tag>, <tag>]
    category: <research|software-development|...>
---

# <Skill Title>

<One-sentence purpose. One sentence stating what this skill explicitly does
not do (boundary).>

## When to Use

- <trigger scenario>

Don't use for:

- <counter-trigger, incl. sibling skill if adjacent>

## Goal

<What "finished" means for this skill's task.>

## Scope restrictions

- <hard prohibitions that bound the stage; keep one line each>

## Tool

Run through the `terminal` tool:

`uv run python .agents/tools/<package>/<tool>.py --<flag> ...`

The tool is generic; domain vocabulary and judgment live in this skill.

## Workflow

1. <step> Done when: <checkable criterion>.
2. <step> Done when: <checkable criterion>.
3. <stop condition> — do not start the next pipeline stage.

## Output

<Fields/shape to report; state uncertainty explicitly; no claims beyond
executed evidence.>

## Pitfalls

1. <known trap, why it happens, what to do instead>

## Verification

- [ ] `uv run python .agents/skills/agent-development/scripts/validate_skill.py <this-skill-dir>` exits 0
- [ ] <task-level proof: files produced, counts from real output>

## Evaluation

Maintained per `references/skill-engineering.md` §6; add cases as behavior
drifts are observed and fixed.

```
### EVAL-<skill>-01
Task: <prompt run verbatim>
Expected: <observable behaviors>
Forbidden: <failing behaviors>
Last run: <YYYY-MM-DD — pass/fail, note>
```
