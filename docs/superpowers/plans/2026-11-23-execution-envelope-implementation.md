# Execution Envelope Implementation Plan (ETAP 4 deliverable — awaiting go)

> **For agentic workers:** REQUIRED SUB-SKILLS: superpowers:test-driven-development (every code step), superpowers:writing-skills (SKILL.md edits are tested artifacts), superpowers:verification-before-completion (every checkpoint). Execution mode recommended: subagent-driven via `delegate_task(option="developer")` per agent-development delegation contract (preflight/postcheck every call). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Implement the Execution Envelope v1.0 contract (spec rev 1.2) between sc-dataset-search and sc-dataset-qualification: run identity with scope/criteria fingerprints, machine-derived statuses, fully-reconciled handoff with exact-scope SHA-256, staged fail-closed validation, concurrent-safe atomic writes, post-seal immutability, sealed artifacts, skill-level gates.

**Architecture:** One new pure-JSON tool `geo_envelope.py` (validator/deriver/sealer/reporter; no DB; stdlib-only) is the enforcement core; `geo_search.py` gains opt-in additive envelope emission; the two SKILL.md files switch from prose gates to "run the validator, write your section, never hand-set status". DuckDB is untouched: persistence stays an optional reference block.

**Tech Stack:** Python 3.13, click, pytest 9 (existing geo_db/conftest isolation), stdlib-only in tools (json/hashlib/datetime/fcntl/os/tempfile/subprocess for the contention test), uvx ruff gate.

**Spec:** `docs/superpowers/specs/2026-11-23-execution-envelope-design.md` (rev 1.2 — includes OD-9 approval and corrections A–E). The plan argues from the spec; executors read both.

## Global Constraints

- OD decisions as approved: OD-1 extend geo_search additively (opt-in flags, byte-compatible default output); OD-2 `data/reports/run_<id>/envelope.json`; OD-3 NO new dependencies (stdlib fcntl/os/tempfile only); OD-4 PARTIAL requires user-acceptance block; OD-5 same run_id for technical resume — **gated by identical fingerprints (spec 4.2)**; OD-6 NO DuckDB schema changes; OD-7 skill version + SKILL.md sha256 REQUIRED; OD-8 outcome dictionary; **OD-9 `ALL_EXCLUDED` APPROVED — sixth terminal outcome with the exact 6 conditions of spec 5.4a; NO_CANDIDATES/EMPTY_COMPLETE semantics unchanged.**
- Backward compatibility: `geo_search.py` default output keeps EXACT current keys `{queries, records, dropped_no_gse}`; existing 675-line search suite passes unmodified (any edit to an existing test body = STOP + report).
- Production DB: never opened for write; conftest sentinel enforces; `geo_envelope.py` contains zero duckdb imports (grep gate).
- Do not touch: `geo_database.py`, `data/schema.sql`, `.agents/skills/sc-dataset-database*`, user-dirty paths. No git commits unless the user asks — checkpoints replace commit steps.
- Repo conventions: `from __future__ import annotations`, full type hints, click CLI + JSON stdout, tests under `tests/` importing `tools.sc_dataset.*`.
- SKILL.md edits follow writing-skills: RED baseline pressure-scenario FIRST (fresh subagent, current skill), then edit, then GREEN re-test, >=5 reps per family, transcripts saved to `docs/superpowers/baselines/etap4-*.md`.
- Run everything from `/workspace`; `.venv/bin/python -m pytest -q`.

## Review Focus (failure modes; each pinned by a named test)

1. Missing/absent status treated as success → `test_missing_search_status_blocks`, `test_absent_per_query_record_is_error` (T2).
2. PARTIAL relabelled COMPLETE (hand-set or via forgotten not_run) → `test_declared_status_must_match_derived`, `test_not_run_forbids_complete` (T3).
3. A found GSE vanishes between Search and Qualification → `test_reconciliation_V1..V7` incl. missing-accounting (T2).
4. Hash over mutable envelope / trusting stored hash → `test_hash_recomputation_mismatch_blocks`, `test_hash_immune_to_later_stage_sections` (T1, T2).
5. Duplicate handoff re-qualifies processed GSEs → `test_duplicate_handoff_is_idempotent_noop` (T4).
6. Resume with drifted scope/criteria accepted under same run_id → `test_resume_scope_drift_refused`, `test_resume_criteria_drift_refused` (T4).
7. flock on the data file racing os.replace (lost update / torn read) → `test_two_processes_concurrent_mutation_no_lost_update`, `test_readers_never_observe_torn_file` (T4).
8. Post-seal mutation silently accepted or rewriting → `test_mutation_after_seal_blocked_bytes_identical`, `test_reseal_identical_is_noop` (T4).

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `.agents/tools/sc_dataset/geo_envelope.py` | envelope model, canonical JSON, handoff hash + scope/criteria fingerprints, staged validator, derivations, lock+atomic write, seal/refuse/report CLI | CREATE |
| `tests/test_geo_envelope.py` | unit tests T1–T4 | CREATE |
| `tests/test_envelope_scenarios.py` | integration S1–S16 (T7) | CREATE |
| `tests/fixtures/envelopes/*.json` | scenario fixtures (valid + one per error code) | CREATE |
| `.agents/tools/sc_dataset/geo_search.py` | opt-in envelope emission + identity/fingerprint writing | MODIFY (additive) |
| `tests/test_geo_search.py` | new envelope-mode tests appended; existing tests untouched | MODIFY (append-only) |
| `.agents/skills/sc-dataset-search/SKILL.md` | envelope docs; 0.4.1 → 0.5.0 | MODIFY (after RED) |
| `.agents/skills/sc-dataset-qualification/SKILL.md` | REQUIRED input-contract block, refusals, fingerprints; 0.2.0 → 0.3.0 | MODIFY (after RED) |
| `docs/superpowers/baselines/etap4-skill-red.md` | pressure-test transcripts | CREATE (T6) |

---

### Task 1: Core model — canonical JSON, handoff hash, fingerprints

**Files:** Create `geo_envelope.py`; Test `tests/test_geo_envelope.py`.

**Interfaces:**
- Produces: `canonical_json_bytes(obj: dict) -> bytes`; `hash_payload(env) -> dict`, `compute_handoff_hash(env) -> str` (spec 4.1 EXACT payload); `scope_payload(search) -> dict`, `criteria_payload(qual) -> dict`, `compute_fingerprint(payload) -> str` (spec 4.2); constants `ENVELOPE_VERSION="1.0"`, `HASH_VERSION="handoff-payload-v1"`, `IDENTITY_VERSION="exec-identity-v1"`, `SCOPE_FP_VERSION="scope-fp-v1"`, `CRITERIA_FP_VERSION="criteria-fp-v1"`; enums `QUERY_STATES={"success","partial","failed","not_run"}`, `SEARCH_STATES`, `QUAL_STATES={"COMPLETE","PARTIAL","NOT_APPLICABLE","FAILED","BLOCKED_INVALID_ENVELOPE","BLOCKED_UPSTREAM"}`, `OUTCOME_STATES={"SUCCESS","EMPTY_COMPLETE","NO_CANDIDATES","ALL_EXCLUDED","PARTIAL_SUCCESS","FAILURE"}`, `EXCLUSION_REASONS={"already_registered","out_of_scope","duplicate_superseries","user_directive","other"}`; `new_run_id(now=None) -> str`; `make_run_dir(workspace, run_id) -> Path`; `envelope_skeleton(run_id, created_at, skill, tool, tool_version, params, limits) -> dict` (writes `execution_identity.search_scope.sha256` immediately).
- Consumes: stdlib only.

- [ ] Step 1: RED — `test_handoff_hash_is_canonical_and_stable` (key-insertion order and later sections irrelevant; one changed transferred id changes digest; excluded list order irrelevant). Run `pytest tests/test_geo_envelope.py -q` → FAIL (module missing).
- [ ] Step 2: GREEN — implement canonical_json_bytes (sort_keys, separators (",",":"), ensure_ascii=False, utf-8), hash_payload, compute_handoff_hash exactly per spec 4.1.
- [ ] Step 3: RED — `test_reason_code_change_changes_handoff_hash` + `test_reason_note_change_does_not_change_handoff_hash` (hash scope per 4.1: reason_code IN, note/by/at OUT) + `test_provenance_change_detected_in_handoff_hash` (tool_version, skill_version, finished_at).
- [ ] Step 4: GREEN (payloads already cover it; assert explicitly).
- [ ] Step 5: RED — fingerprint tests: `test_scope_fingerprint_binds_queries_max_results_species_pagination` (each variation changes digest; reordering params does not), `test_criteria_fingerprint_binds_version_and_skill_md_sha256`, `test_skeleton_identity_matches_params_recomputation` (validator rule 1).
- [ ] Step 6: GREEN — implement scope_payload/criteria_payload/compute_fingerprint + skeleton writing identity; `test_new_run_id_format` (regex `\d{8}T\d{6}Z-[0-9a-f]{8}`).
- [ ] Step 7: **Checkpoint 1:** module tests green; `uvx ruff@0.17.5 check` clean.

### Task 2: Staged fail-closed validator (spec 4, 4.3, 5.2; corrections A/B/C/E)

**Files:** Modify `geo_envelope.py`; Test `tests/test_geo_envelope.py`; fixtures.

**Interfaces:**
- Produces: `ValidationIssue(severity, code, path, message)` dataclass; `validate_envelope(env: dict, stage: str = "seal") -> list[ValidationIssue]` cumulative per spec 4.3; CLI `validate <path> [--stage ...]` exit 0 iff no ERROR; error codes `E_STATUS_MISSING, E_QUERY_UNACCOUNTED, E_UNKNOWN_FIELD, E_UNKNOWN_ENUM, E_TIMESTAMP_FORMAT, E_HANDOFF_V1..V7, E_HASH_MISMATCH, E_IDENTITY_TAMPER, E_HANDOFF_MISSING, E_QUERY_ID_UNKNOWN, E_ATTEMPT_SEQUENCE`.
- Consumes: Task 1.

- [ ] Step 1: RED — structure tests: missing search status → `E_STATUS_MISSING`; unknown field/enum; planned query absent from per_query → `E_QUERY_UNACCOUNTED`; **per_query entry with query_id NOT in planned → `E_QUERY_ID_UNKNOWN`**; duplicate attempt numbers / attempt not starting at 1 / gaps → `E_ATTEMPT_SEQUENCE`; non-ISO-Z timestamps → `E_TIMESTAMP_FORMAT`.
- [ ] Step 2: GREEN — implement search-stage validation incl. identity rule 1 (recompute scope fp from params; mismatch with `execution_identity` → `E_IDENTITY_TAMPER`).
- [ ] Step 3: RED — `test_post_search_envelope_valid_at_search_stage`: skeleton with finished search section, NO handoff → `validate(env, "search")` = no ERRORs; `validate(env, "handoff")` → ERROR `E_HANDOFF_MISSING`; `check_admission(env)` refuses with `BLOCKED_INVALID_ENVELOPE`.
- [ ] Step 4: GREEN — stage gating logic (cumulative).
- [ ] Step 5: RED — `test_reconciliation_V1..V7` parametrized (count mismatch; duplicates in transferred/excluded; intersection; GSE unaccounted; GSE not in found; exclusion entry missing each of reason_code/reason_note/by/at_utc → V-6 per-field subcases; hash mismatch → V-7).
- [ ] Step 6: GREEN — V-1…V-7 exact.
- [ ] Step 7: RED — `test_hash_recomputation_mismatch_blocks`, `test_hash_immune_to_later_stage_sections` (append full qualification section → hash still valid).
- [ ] Step 8: **Checkpoint 2:** all green; fixture per error code exists; ruff clean.

### Task 3: Derivations, admission gate, outcome engine

**Files:** Modify `geo_envelope.py`; Test `tests/test_geo_envelope.py`.

**Interfaces:**
- Produces: `final_attempts(per_query) -> dict[str, dict]`; `derive_search_status(planned: list[str], per_query: list[dict]) -> str`; `check_admission(env) -> tuple[str, list[ValidationIssue]]` returning ADMIT | `BLOCKED_INVALID_ENVELOPE` | `BLOCKED_UPSTREAM`; `derive_qualification_status(qual: dict, transferred_gse_ids: list[str]) -> str` — **MUST take the handoff list (spec 5.3, correction D)**; `derive_pipeline_outcome(env) -> str` implementing spec 5.4 incl. the six-condition `ALL_EXCLUDED` (OD-9); CLI `derive <path>` (read-only, prints derived values, writes nothing).

- [ ] Step 1: RED — search derivation table: all-success→COMPLETE (incl. 0-candidate case), success+partial→PARTIAL, success+failed→PARTIAL, all-failed→FAILED, all-not_run→FAILED(+`no_queries_attempted`), not_run+success→PARTIAL; `test_declared_status_must_match_derived`; `test_not_run_forbids_complete`.
- [ ] Step 2: GREEN.
- [ ] Step 3: RED — admission matrix (spec 5.2): no envelope / missing status → BLOCKED_INVALID_ENVELOPE; FAILED → BLOCKED_UPSTREAM; PARTIAL without acceptance block → BLOCKED_UPSTREAM; PARTIAL + partial_accepted + valid acceptance → ADMIT; hash mismatch → BLOCKED_INVALID_ENVELOPE; reconciliation gap → BLOCKED_INVALID_ENVELOPE; post-search-without-handoff → BLOCKED_INVALID_ENVELOPE (`E_HANDOFF_MISSING`).
- [ ] Step 4: GREEN.
- [ ] Step 5: RED — qualification accounting tests (correction D): `test_qual_complete_requires_all_transferred_accounted` (one transferred GSE missing from processed/failed/skipped → validation ERROR, not PARTIAL); `test_gse_in_two_categories_is_error` (V-Q2); `test_decisions_lists_must_partition_processed` (V-Q3, incl. overlap include/uncertain); `test_untransferred_gse_in_section_is_error` (V-Q4); `test_failed_entries_require_errors_detail` (each failed_gse_id has an errors[] entry; each skipped_gse_id has skipped_reasons[]); `test_empty_transferred_is_not_applicable`.
- [ ] Step 6: GREEN.
- [ ] Step 7: RED — outcome tests S1–S12 fixtures: every terminal scenario asserts `derive_pipeline_outcome`; **`test_all_excluded_exact_conditions` (all six OD-9 conditions; each individually violated → outcome is NOT ALL_EXCLUDED and validation flags the specific rule); `test_no_candidates_vs_all_excluded_separation` (found==0→NO_CANDIDATES; found>0 all-excluded→ALL_EXCLUDED; mislabelling either way is an error)**; `test_open_envelope_has_no_outcome`.
- [ ] Step 8: **Checkpoint 3:** green + ruff clean.

### Task 4: Mutation commands — lock protocol, atomic write, handoff, refuse, resume, seal, duplicate/conflict

**Files:** Modify `geo_envelope.py`; Test `tests/test_geo_envelope.py`.

**Interfaces:**
- Consumes: T1–T3.
- Produces: `read_envelope_locked(path) -> (env, lock_fh)` / `write_envelope_atomic(path, env)` implementing spec 5.6 EXACTLY (separate `envelope.json.lock`, flock on the lock file only, tmp file + fsync + os.replace + dir fsync); all mutation commands acquire this protocol; `build_handoff(env, transferred, excluded, allowed, acceptance, now) -> env` (recomputes counts + `content_sha256` via T1, re-validates at stage `handoff`); `write_qualification_section(env, qual, skill_md_path, now)` (computes criteria fingerprint from real SKILL.md bytes, fills `execution_identity.qualification_criteria` on first write, refuses drift); `refuse(env, stage, status, reason, now)` (adds BLOCKED section + error codes, never touches decisions); `apply_resume(env, new_invocation, now)` (fingerprint gates per spec 4.2/5.5; `resume.attempt`++; skips already processed); `seal(env, now)` (full `seal`-stage validation before setting `sealed_at_utc`; already-sealed rules per 5.7); `report(env) -> str` (non-coverage statements, handoff accounting table, outcome rationale); CLI subcommands `init-run | attach-search | build-handoff | write-qualification | refuse | resume | seal | validate | derive | report | criteria-hash`.

- [ ] Step 1: RED — idempotency/conflict: `test_duplicate_handoff_is_idempotent_noop` (same run_id + same recomputed hash → W_REPEATED_HANDOFF, processed preserved), `test_conflicting_handoff_same_run_id_fails` (E_ENVELOPE_CONFLICT).
- [ ] Step 2: RED — fingerprint gates: `test_resume_scope_drift_refused` (extra/modified planned query → E_SCOPE_FINGERPRINT_MISMATCH; new run with resumed_from_run_id succeeds), `test_resume_criteria_drift_refused` (different SKILL.md bytes/version mid-run → E_CRITERIA_FINGERPRINT_MISMATCH), `test_resume_identical_scope_appends_attempts_only` (retry keeps identity, per_query gains attempt 2, aggregate from finals).
- [ ] Step 3: RED — seal rules: `test_seal_success_sets_timestamp`, `test_seal_rejects_inconsistency` (declared COMPLETE + partial query → refuse seal), `test_mutation_after_seal_blocked_bytes_identical` (all six mutation commands; compare sha256 of file before/after), `test_reseal_identical_is_noop` (W_ALREADY_SEALED, bytes unchanged), `test_reseal_different_content_fails`, `test_reads_still_allowed_after_seal`.
- [ ] Step 4: GREEN — implement lock+atomic write + all commands.
- [ ] Step 5: RED — real contention: `test_two_processes_concurrent_mutation_no_lost_update` — spawn two subprocesses (sys.executable -c importing the module) each performing N sequential build_handoff/report-free mutations (e.g. append warnings entries) against one envelope; assert final file valid at seal-stage validation, contains ALL 2N mutations (no lost update), never a parse failure mid-run (reader subprocess polling during the storm); `test_readers_never_observe_torn_file`; `test_crash_before_replace_leaves_data_intact` (child killed after tmp write, before replace → envelope unchanged, stale tmp tolerated). Use tmp_path only; no repo paths.
- [ ] Step 6: GREEN — fix protocol until contention passes deterministically (bounded retries on LOCK_NB or blocking LOCK_EX; document choice).
- [ ] Step 7: **Checkpoint 4:** full module suite green, no sleeps>2s per test, ruff clean.

### Task 5: geo_search.py additive envelope emission (OD-1)

**Files:** Modify `.agents/tools/sc_dataset/geo_search.py`; Test `tests/test_geo_search.py` (append-only).

**Interfaces:**
- Produces: `TOOL_VERSION = "1.1.0"`; new CLI options `--run-id`, `--output-envelope <path>`, `--skill-version <v>` (fallback `SC_DATASET_SEARCH_VERSION`, else "unknown"); when `--output-envelope` set: writes envelope via geo_envelope primitives (skeleton + identity fingerprint + search section with derived status + finished_at + not_run entries for planned-but-unattempted queries on mid-run abort) and echoes current keys PLUS `run_id`, `envelope_path`; default invocation stdout unchanged (same keys, same values).
- Consumes: `geo_envelope` (same package; stdlib-only deps respected).

- [ ] Step 1: RED — `test_default_output_keys_unchanged` (pin today's exact key set), `test_envelope_mode_writes_search_stage_valid` (`validate(env, "search")` zero ERRORs; `validate(env, "handoff")` E_HANDOFF_MISSING — expected per spec 4.3), `test_planned_not_attempted_becomes_not_run` (session raises on 2nd of 3 queries → q3 not_run, status PARTIAL, identity fp present), `test_run_id_passthrough` (--run-id respected, format validated).
- [ ] Step 2: GREEN implement; `_get_json` retry engine untouched.
- [ ] Step 3: **Checkpoint 5:** `pytest tests/test_geo_search.py -q` — ALL pre-existing tests pass unmodified (record count vs baseline 675-line suite); ruff clean on the diff.

### Task 6: SKILL.md contracts with writing-skills RED/GREEN

**Files:** `.agents/skills/sc-dataset-search/SKILL.md` (0.4.1→0.5.0), `.agents/skills/sc-dataset-qualification/SKILL.md` (0.2.0→0.3.0), `docs/superpowers/baselines/etap4-skill-red.md`.

- [ ] Step 1: RED (pre-edit baseline): fresh subagents given qualification-shaped prompts (envelope FAILED / status missing / PARTIAL without acceptance / post-search artifact without handoff) under CURRENT skills; record verbatim rationalizations for proceeding.
- [ ] Step 2: GREEN (edits): qualification skill gains REQUIRED `## Input contract` — validator command per stage, refusal table, fingerprint rules (criteria sha256 via `geo_envelope.py criteria-hash`), "never hand-set status", writes qualification section via tool; search skill documents envelope emission flags + not_run + derived aggregate. Bump versions; `validate_skill.py` exit 0.
- [ ] Step 3: Re-run scenarios WITH edited skills, ≥5 reps per family, all refuse correctly; append transcripts to baselines doc; record new SKILL.md sha256 values in the report (they feed future runs' fingerprints).
- [ ] Step 4: **Checkpoint 6.**

### Task 7: End-to-end scenario suite + final gates

**Files:** `tests/test_envelope_scenarios.py`; fixtures S1–S16 (no network; monkeypatched search like existing tests).

- [ ] Step 1: one test per S1–S16 wiring init-run → attach-search → build-handoff → (admission|refuse) → write-qualification → seal → derive/report; S13/S14 fingerprint drifts; S15 post-seal; S16 concurrency (reuse T4 machinery, single iteration).
- [ ] Step 2: **Checkpoint 7 (verification-before-completion):** fresh full `pytest -q` (baseline 145 + new tests, ZERO failures — GSE12 red no longer applies since ETAP 3); `uvx ruff@0.17.5 check` all changed files; `validate_skill.py` both skills; `grep -c "import duckdb" .agents/tools/sc_dataset/geo_envelope.py` == 0; `md5sum data/geo.duckdb` == `a830293f03f13b2e28396b5360ebc0f5`; `git status --porcelain` explained against File Structure.

### Task 8 (deferred, gated): DB projection & pipeline-skill mirror

Out of this implementation unless separately approved: OD-6 (`run_id` column / `geo_execution_runs` mirror), repo-mirror of `geo-dataset-production-pipeline` (F-4-adjacent). Toggle points: `qualification.persistence` (already reference-only), T7 counts.

## Done When

Checkpoints 1–7 pass with pasted fresh evidence; all Review Focus tests exist and are green; SKILL.md edits carry recorded RED/GREEN transcripts; OD-1–OD-9 decisions each traceable to at least one implemented test; production DB md5 unchanged; user-dirty files untouched; no commits.
