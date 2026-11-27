---
name: sc-dataset-pipeline
description: Run the gated GEO pipeline - search to sealed registration.
version: 0.1.0
author: Piotr Zawierucha, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [bioinformatics, single-cell, GEO, orchestration, pipeline]
    category: research
---

# sc-dataset-pipeline

Authoritative, versioned orchestration contract for the GEO single-cell
pipeline in this repository. It is the source of truth for stage ORDER and
GATES; domain rules live in the component skills (`sc-dataset-search`,
`sc-dataset-qualification`, `sc-dataset-database`, `sc-dataset-audit`).
The external profile skill `geo-dataset-production-pipeline` must mirror
this file, never contradict it.

## Required order (13 steps, never reorder)

1. **init run** — `python .agents/tools/sc_dataset/geo_envelope.py init-run <dir>/envelope.json --run-id <id> --query <q> [--query <q> ...]`.
2. **search** — `python .agents/tools/sc_dataset/geo_search.py --query <q> --output-envelope <dir>/envelope.json --run-id <id>` (exit 0; per-query statuses recorded in the envelope).
3. **build handoff** — `python .agents/tools/sc_dataset/geo_envelope.py build-handoff <dir>/envelope.json <handoff.json>` (handoff JSON carries transferred/excluded/allowed_to_proceed/acceptance); every found GSE
   is transferred or excluded with a reason (V-1..V-7 enforced, exit 1). Check GEO `!Series_relation` SuperSeries/SubSeries links first: with the single-owner `gsm_id` catalog, transfer the SubSeries members and
   exclude the SuperSeries as `duplicate_superseries` (GEO semantics), unless coverage math forces the inverse — then state it.
4. **validate/admission** — `python .agents/tools/sc_dataset/geo_envelope.py validate <dir>/envelope.json --stage handoff`, then `python .agents/tools/sc_dataset/geo_envelope.py admit <dir>/envelope.json` (exit 0 only on ADMIT). Never proceed on
   BLOCKED_UPSTREAM or BLOCKED_INVALID_ENVELOPE.
5. **qualification** — per `sc-dataset-qualification` against transferred
   GSEs only; record via `python .agents/tools/sc_dataset/geo_envelope.py write-qualification <dir>/envelope.json <qualification.json> --skill-md <SKILL.md>` (requires
   ADMIT and a valid handoff; refuses sealed envelopes).
6. **metadata fetch** — `python .agents/tools/sc_dataset/geo_metadata.py <GSE> --run-dir <dir> --run-id <id> [--max-mb N]`; retries are
   internal (max 4, equal-jitter); deferred (Retry-After > 60 s) exits 75;
   permanent failures exit 1 and are recorded as events, never silently skipped.
7. **preflight** — `python .agents/tools/sc_dataset/geo_metadata.py preflight-registration <dir> <GSE> --db <db> --run-id <id>`; exit 0 = `ready` + `preflight/<GSE>.grant.json`;
   exit 2 = `ownership_conflict`; exit 1 = preflight_failed. No DB write may
   happen without the matching grant.
8. **registration** — `python .agents/tools/sc_dataset/geo_database.py --db <db> save-dataset <GSE> --preflight <grant>` (same for `save-sample <GSM> --gse-id <GSE>` and `save-characteristics <GSM> --gse-id <GSE> --characteristics '<json>'`); exit 0; exit 1 =
   preflight_required/grant_invalid/grant_stale_artifact/gse-mismatch;
   exit 2 = ownership_conflict (transaction rolled back, zero DB change).
   Writes without `--preflight` are refused BEFORE touching the DB.
9. **verify registration** — `python .agents/tools/sc_dataset/geo_metadata.py verify-registration <dir> <GSE> --db <db> --run-id <id>`; exit 0 only on full set/feature agreement;
   it appends the `registered` event (`registration-v1` payload).
10. **qualification review** — per `sc-dataset-qualification-review`: ensure-schema,
    DB backup, then `geo_review.py save-review` (dry-run first) against the persisted
    first-pass decisions; `validate` + `report`. Review is append-only; the first-pass
    rows are never modified. Runs only after step 8/9 `save-qualification` exists — the
    tool refuses `qualification_missing` otherwise. Seal does not depend on review
    presence, but a production run that skips review must state so in the report.
11. **registered** — assert every transferred GSE (in a run that declares
    `qualification.persistence`) has a latest-status `registered` event with
    matching run_id and GSM set; `failed`/`deferred` after `registered` is
    blocking.
12. **derive outcome** — exactly one of: SUCCESS, EMPTY_COMPLETE,
    NO_CANDIDATES, ALL_EXCLUDED, PARTIAL_SUCCESS, FAILURE (machine-derived,
    never declared).
13. **seal** — `python .agents/tools/sc_dataset/geo_envelope.py seal <dir>/envelope.json` (exit 0 = immutable envelope + derived outcome; any validation issue exits 1 and the file bytes stay unchanged). A run whose admission was BLOCKED never reaches seal: record `python .agents/tools/sc_dataset/geo_envelope.py refuse <dir>/envelope.json --stage qualification --status BLOCKED_UPSTREAM --reason <r>`, outcome FAILURE stays derived from the file, the envelope remains unsealed (seal fail-closes with E_HANDOFF_MISSING).

## Hard prohibitions
- Never rewrite first-pass qualification rows from the review stage; review is append-only (`geo_review_decisions`).
- Never write `/workspace/data/geo.duckdb` outside an explicitly approved
  production run; tests use temp DBs only.
- Never reorder steps 7–9; the grant (not the preflight invocation) is the
  enforcement token at the DB boundary.
- Never translate a technical fetch failure into a biological decision, and
  never weaken a validator to make a run pass.
- Never resume a run whose scope or criteria fingerprints changed — issue a
  new run_id with `resumed_from_run_id`.
- Never treat an empty search result as proof of no data; account every
  query (status derived from per-query results).

## Qualification classifier warning

If dataset/GSM decisions are produced by a scripted classifier instead of
per-dataset assessment: the aggregate rule must not fall through to
`uncertain` when all GSMs are decisively `exclude`, and text gates (colon
keyword, sc/snRNA keyword, organoid-in-title) must not veto a dataset that
already has fully eligible GSMs. Rerun the classifier draft and check counts
against the summary before committing.

## GEO accession bucket rule

Strip the final three digits and append `nnn` (`GSE9686 → GSE9nnn`,
`GSE12 → GSEnnn`). Verify with one spaced HEAD per claim; a 403 after 200s
is throttling, not a wrong bucket.

## Recovery and resume

- Interrupted run: `python .agents/tools/sc_dataset/geo_metadata.py recover <dir>` republishes
  parsed artifacts from verified raw bytes without HTTP; then continue at
  step 7.
- `PARTIAL` search may proceed ONLY via an explicit acceptance block in the
  handoff policy (by/reason/accepted_at_utc).
- Deferred (exit 75): keep the run open; re-run step 6 for those GSEs later
  under the same run_id and fingerprints.

## Completeness gates before reporting

Report query-level counts and statuses, transferred vs excluded with
reasons, per-GSE fetch/retry/deferred history, DB deltas (datasets, GSMs,
characteristics, qualifications), registered-event agreement, envelope
outcome and seal hash, and pre/post database counts. Never declare the
pipeline complete on tool exit codes alone.
