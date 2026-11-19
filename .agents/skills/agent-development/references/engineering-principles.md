# Engineering Principles

Standards for all code and skill prose in this repository. Distilled from
named sources; rules were selected for behavior change, not copied wholesale.
Each rule below either changes what the agent does or provides a reviewable
check — boilerplate advice was dropped.

Sources:

- **Karpathy Guidelines** — Think Before Coding, Simplicity First, Surgical
  Changes, Goal-Driven Execution.
- **Google Engineering Practices** — code review criteria and maintainability
  (small, self-contained, testable changes).
- **Google Python Style Guide** — naming, typing, docstrings.
- **Anthropic skill-creator methodology** — skills are engineered and
  evaluated like software (see `skill-engineering.md`).

## Think Before Coding

- Read every file in scope before proposing changes; do not redesign from
  names alone.
- State assumptions explicitly; where a requirement is ambiguous and the
  ambiguity changes behavior, ask instead of guessing.
- Prefer deleting code over adding abstraction. Every new module, class, or
  config flag must be traceable to a stated requirement.
- Before acting, say what could break (shared functions, skills that call the
  tool, existing tests).

## Simplicity First

- Prefer the simplest solution that satisfies the requirement.
- No speculative abstraction: no base classes, plugins, factories, or config
  options for needs that were not requested.
- No unneeded inheritance or third-party dependencies.
- Small, focused functions and dataclasses; split when a function does two
  jobs or exceeds ~40 meaningful lines, not by a hard quota.
- Optimize for readability and maintainability first; performance work needs
  a measured reason.

## Clean Code

- Unambiguous names: functions are verb phrases, booleans read as predicates
  (`is_complete`, `require_manifest`); no `data2`, `process()`, `utils.do_it`.
- PEP 8 formatting; Ruff defaults as the enforcement gate.
- Type hints everywhere; `from __future__ import annotations` at the top of
  every module (project convention).
- Docstrings on public functions, classes, and modules stating what they do
  and, when non-obvious, what they must not do.
- One responsibility per component; keep responsibilities of adjacent modules
  disjoint (see the Tool/Skill split convention in `tool-engineering.md`).
- Flat logic: guard clauses over nested ifs; keep nesting depth low.
- Explicit error handling with actionable messages; never swallow exceptions
  silently, never emit raw tracebacks as user-facing errors.
- Comments explain why, never what; delete comments that restate code.

## Architecture

- Apply SOLID, DRY, and KISS where they pay for themselves — the trigger is
  duplication or instability observed in the code, not theory.
- Composition over inheritance: this repo's pattern is `Protocol` interfaces
  plus small modules (see `.agents/tools/sc_dataset/adapters/base.py`);
  do not build class hierarchies.
- Low coupling: business logic must not import orchestration or I/O layers;
  pass dependencies (sessions, paths) as arguments.
- Separate pure logic from I/O so components are independently testable;
  thin `main()`/CLI wrappers carry no logic worth unit-testing.
- Boundaries are contracts: a tool's JSON output and its manifest/report
  files are consumed by skills and later pipeline stages — change them only
  with the consumers updated in the same change set.

## Surgical Changes

- Touch only files related to the task; no drive-by refactors or reformatting.
- No behavior changes without a stated reason in the report.
- Never delete or loosen existing tests to get a green run; fix the code.
- No architecture edits (moving modules, renaming packages) inside a
  feature or fix change set — propose them separately.
- Record unrelated dirty files seen in `git status` before starting and
  verify they remain untouched afterwards.

## Goal-Driven Execution

- Every phase ends with a checkable completion criterion (the workflow
  sections use "Done when:" for this).
- "Done" means every stated acceptance criterion was verified by executed
  commands — a written plan, a passing subset, or a plausible file tree is
  not completion.
- Report real results: counts, exit codes, and failing test names come from
  actual output, never from memory or inference.

## Code Review Dimensions (Google-derived)

Review every change (self-review included) on four axes, in order:

1. **Correctness** — does it do what the requirement says, including edge
   cases and failure modes?
2. **Impact** — what existing behavior, contracts, or other skills/tools does
   it touch? Is the scope minimal?
3. **Tests** — is new behavior tested, is a fix covered by a regression test,
   was the existing suite run and is its state reported?
4. **Readability** — naming, typing, structure per the rules above; reviewers
   optimize for long-term maintainability, not the author's speed.
