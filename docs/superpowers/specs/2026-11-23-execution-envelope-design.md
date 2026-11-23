# Execution Envelope — Search → Qualification Contract (Design Spec, ETAP 4)

**Status:** REVISION 1.2 — OD-9 approved by the user (2026-11-23); plan-review corrections A–E incorporated. Implementation still NOT started. Awaiting separate GO for implementation.
**Revision 1.1 (user corrections):** A full handoff accounting, B precise SHA-256 scope, C `not_run` query state, D retry/resume/scope-change rules, E bounded auditability claims, F all-found-excluded outcome case.
**Revision 1.2 (plan-review corrections):** OD-9 `ALL_EXCLUDED` approved with exact conditions; A execution/scope fingerprints (§4.2); B safe locking + atomic write (§5.6); C post-seal immutability (§5.7); D qualification accounting keyed to the handoff list (§5.3); E staged validation (§4.3).
**Scope:** formal contract between `sc-dataset-search` (v0.4.1) and `sc-dataset-qualification` (v0.2.0). Design only — no implementation, no TDD, no schema changes, no commits.
**Methodology:** superpowers:brainstorming (option analysis, open decisions), agent-development (repo conventions, delegation model), superpowers:writing-skills (skill-edit implications: every SKILL.md change requires RED/GREEN pressure-testing before deployment — deferred to implementation phase).

---

## 1. Current architecture and gaps (verified in ETAP 1/2/3)

Today's flow (all links are agent-mediated prose, nothing is structurally enforced):

```
sc-dataset-search  ->  geo_search.py stdout envelope {queries[], records[], dropped_no_gse}
                     (per-query status: success|partial|failed; no run_id, no tool/skill version,
                      no timestamps, no aggregate status)
   | (agent copies GSE ids into notes / run-dir JSON — informal)
selection / dedup    (ad hoc; e.g. run 20261105 candidates.json, new_candidate_ids.json)
   |
sc-dataset-database  ->  geo_database.py check / save-dataset / save-sample (registration,
                        metadata via geo_metadata.py — no retry, no status recording — F-5)
   |
sc-dataset-qualification -> save-qualification --sample-results-file (atomic batch OK)
   |
DuckDB               ->  geo_qualification.qualified_at is the ONLY run trace (no run_id — F-1)
```

Gaps:
- **G-1** No execution identity: nothing links a set of qualified rows to the search run that produced them.
- **G-2** No aggregate search status; `partial`/`failed` per-query facts die in stdout unless the agent remembers them.
- **G-3** Qualification has no input gate: a missing/failed/partial search is indistinguishable from a complete one at qualification time ("absence of status != success").
- **G-4** No handoff record: "found 313 -> transferred N" is unrecoverable; zero-result vs skipped-candidates is ambiguous.
- **G-5** The orchestration rules live in `geo-dataset-production-pipeline` — a PROFILE skill outside git (`/opt/data/skills/research/...`), duplicating rules that should be structural, and referencing `sc-dataset-catalog` (F-4 collision).
- **G-6** Re-delivery/restart has no defined semantics (idempotency, conflict detection).

## 2. Responsibility split

| Layer | Owns | Never owns |
|---|---|---|
| `sc-dataset-search` | query generation vocabulary, NCBI calls, per-query truthfulness, emitting the SEARCH section of the envelope with an honestly-derived aggregate status | qualification criteria, DB writes |
| `sc-dataset-qualification` | eligibility criteria and decisions (GSE + per-GSM), reading the envelope, refusing invalid/partial/failed input, writing its own QUALIFICATION section | discovery, persistence mechanics, criteria leaking into search |
| Execution Envelope (artifact + validator tool) | identity, status, timestamps, counts, handoff, provenance, error/warning logs | any biological judgment — it is a data-and-control contract only |
| `sc-dataset-database` / DuckDB | storage + already-verified atomic batch validation | being the source of truth for run state |
| Orchestrator (pipeline skill) | sequencing, remediation attempts, requesting user acceptance | inventing statuses, bypassing gates |

Criteria stay exclusively in Qualification — nothing from section 3 of the qualification skill moves into search.

## 3. Envelope location and lifecycle (OD-2)

- **Canonical carrier:** one JSON file per run: `data/reports/run_<run_id>/envelope.json` (+ stage artifacts alongside: per-query raw outputs, metadata fetch logs, sample-results JSONs). Source of truth for run state = the file, NOT DuckDB (independence requirement).
- `geo_search.py` emits the envelope skeleton (search section) to stdout with `--run-id` / auto-created; the orchestrator persists it and passes it by path to later stages.
- DuckDB may later MIRROR the envelope (projection table, e.g. `geo_execution_runs`) — explicitly deferred (OD-6), never required.
- Later stages append their own sections; the handoff hash therefore NEVER covers the whole envelope (see 4.1).
- Lifecycle: `OPEN` -> each stage appends its section under an advisory file lock -> `SEALED` (validator refuses further modification). Sealing is required before the final report claims completion.

**run_id (OD-1b):** `YYYYMMDDTHHMMSSZ-<uuid4-first-8>` e.g. `20261123T170500Z-3f9a1c2e` — sortable, collision-safe, reproducible format.

## 4. Execution Envelope v1.0 structure

Full example (annotated with required/optional):

```json
{
  "envelope_version": "1.0",
  "run_id": "20261123T170500Z-3f9a1c2e",
  "created_at_utc": "2026-11-23T17:05:00Z",
  "sealed_at_utc": null,
  "execution_identity": {
    "identity_version": "exec-identity-v1",
    "search_scope": {"payload_version": "scope-fp-v1", "sha256": "<see 4.2>"},
    "qualification_criteria": null
  },
  "search": {
    "skill": {"name": "sc-dataset-search", "version": "0.4.1"},
    "tool": "geo_search.py",
    "tool_version": "1.1.0",
    "status": "PARTIAL",
    "params": {
      "queries": [{"query_id": "q1", "query": "...", "max_results": 10000}],
      "species_constraint": "Homo sapiens [Organism]"
    },
    "per_query": [
      {"query_id": "q1", "status": "success", "count": 201, "retrieved": 201,
       "error": null, "substituted_query": null, "attempt": 1},
      {"query_id": "q9", "status": "not_run", "count": null, "retrieved": 0,
       "error": "run aborted before this query", "substituted_query": null, "attempt": 1}
    ],
    "results": {
      "unique_gse_count": 313,
      "gse_ids": ["GSE12", "GSE14"],
      "dropped_no_gse": 0
    },
    "limits": {"retstart_pagination": false, "max_results_per_query": 10000},
    "errors": [], "warnings": [],
    "finished_at_utc": "2026-11-23T17:12:41Z"
  },
  "handoff": {
    "from_stage": "search", "to_stage": "qualification",
    "found_count": 313,
    "transferred_gse_ids": ["GSE12", "GSE14"],
    "excluded": [
      {"gse_id": "GSE99", "reason_code": "already_registered",
       "reason_note": "present in catalog since run 20261105T095519Z",
       "by": "agent", "at_utc": "2026-11-23T17:20:02Z"}
    ],
    "allowed_to_proceed": "partial_accepted",
    "acceptance": {"by": "user", "reason": "...", "accepted_at_utc": "..."},
    "hash_version": "handoff-payload-v1",
    "content_sha256": "...",
    "created_at_utc": "..."
  },
  "qualification": {
    "skill": {"name": "sc-dataset-qualification", "version": "0.2.0"},
    "status": "COMPLETE",
    "criteria_profile": {"name": "ibd-colon-default", "skill_version": "0.2.0",
                          "skill_sha256": "..."},
    "processed_gse_ids": ["GSE14"], "failed_gse_ids": [], "skipped_gse_ids": [],
    "decisions": {"include": ["GSE14"], "exclude": [], "uncertain": ["GSE12"]},
    "sample_counts": {"include": 120, "exclude": 55, "uncertain": 17},
    "persistence": {"media": "duckdb", "database": "data/geo.duckdb",
                    "tables": ["geo_datasets", "geo_qualification", "geo_sample_qualification"],
                    "verified_counts": {"datasets": 2, "gsm": 192}},
    "finished_at_utc": "..."
  },
  "pipeline": {
    "outcome": "PARTIAL_SUCCESS",
    "outcome_rationale": "q7 partial; user accepted; all transferred GSEs qualified"
  },   // outcome enum (OD-9 approved): SUCCESS | EMPTY_COMPLETE | NO_CANDIDATES
       //                 | ALL_EXCLUDED | PARTIAL_SUCCESS | FAILURE
  "resume": {"attempt": 1, "resumed_from_run_id": null}
}
```

Required fields: `envelope_version`, `run_id`, `created_at_utc`, `execution_identity.search_scope` (§4.2, immutable), full `search` section (status, tool_version, params, per_query, results, errors, warnings, finished_at when status is set), `handoff` before qualification, `qualification` once the stage starts (its `criteria_profile.skill_sha256` is REQUIRED, OD-7, mirrored into `execution_identity.qualification_criteria` on first write), `pipeline.outcome` once terminal. Optional: `sealed_at_utc`, `limits`, `persistence`, `resume`. REQUIRED but possibly empty: `handoff.excluded`, `search.errors`, `search.warnings`.

Design notes:
- Per-record candidate payloads (titles, organism, ...) stay in `candidates.json` in the run dir (existing production convention); the envelope carries IDs + counts + hash so it stays small and hashable.
- `decisions` maps directly onto the qualification skill output fields — no new vocabulary.
- All timestamps UTC ISO-8601 with explicit Z.
- Unknown fields -> validator ERROR (fail-closed, prevents silent drift). Unknown enum values -> ERROR.

### 4.1 Handoff SHA-256 — exact scope (correction B)

The hash binds the **immutable handoff payload**, never the whole envelope (later stages append sections to the same file). Canonicalization and payload are versioned:

```python
HASH_VERSION = "handoff-payload-v1"

def hash_payload(env: dict) -> dict:
    return {
        "hash_version": HASH_VERSION,
        "run_id": env["run_id"],
        "envelope_version": env["envelope_version"],
        "search_provenance": {                      # provenance data bound to the handoff
            "skill_version": env["search"]["skill"]["version"],
            "tool_version": env["search"]["tool_version"],
            "finished_at_utc": env["search"]["finished_at_utc"],
        },
        "found": sorted(env["search"]["results"]["gse_ids"]),
        "transferred": sorted(env["handoff"]["transferred_gse_ids"]),
        "excluded": sorted(
            [{"gse_id": e["gse_id"], "reason_code": e["reason_code"]}
             for e in env["handoff"]["excluded"]],
            key=lambda e: e["gse_id"],
        ),
        "allowed_to_proceed": env["handoff"]["allowed_to_proceed"],
        "handoff_created_at_utc": env["handoff"]["created_at_utc"],
    }

def canonical_json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")

def compute_handoff_hash(env: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(hash_payload(env))).hexdigest()
```

Rules: (1) `excluded.reason_note`, `acceptance`, `timestamps of exclusion` are deliberately OUTSIDE the hash — notes/acceptance are metadata about the decision moment; the hash binds **which GSEs went where and why, categorically**, plus provenance and handoff time. (2) The validator RECOMPUTES the hash from envelope fields; a stored hash that does not match is `BLOCKED_INVALID_ENVELOPE`; trusting the stored value without recomputation is forbidden. (3) Accessions are ASCII; Unicode normalization is not required but canonical form rejects non-string gse ids. (4) A future payload change increments HASH_VERSION — v1 is frozen with this spec.

### 4.2 Execution-identity fingerprints (plan-review correction A)

Two immutable fingerprints identify the LOGICAL execution; `content_sha256` of the handoff does NOT replace them — the handoff hash binds *result routing* (which found GSEs went where), the fingerprints bind *intent* (what was planned to be searched / under which criteria to qualify).

```python
SCOPE_FP_VERSION = "scope-fp-v1"

def scope_payload(search: dict) -> dict:
    return {
        "payload_version": SCOPE_FP_VERSION,
        "tool": search["tool"],
        "species_constraint": search["params"]["species_constraint"],
        "planned_queries": sorted(
            [{"query_id": q["query_id"], "query": q["query"],
              "max_results": q["max_results"]}
             for q in search["params"]["queries"]],
            key=lambda q: q["query_id"],
        ),
        "retstart_pagination": search["limits"]["retstart_pagination"],
    }

CRITERIA_FP_VERSION = "criteria-fp-v1"

def criteria_payload(qual: dict) -> dict:
    cp = qual["criteria_profile"]
    return {
        "payload_version": CRITERIA_FP_VERSION,
        "skill_name": qual["skill"]["name"],
        "skill_version": qual["skill"]["version"],
        "profile_name": cp["name"],
        "skill_md_sha256": cp["skill_sha256"],   # sha256 of qualification SKILL.md bytes (OD-7)
    }

def compute_fingerprint(payload: dict) -> str:      # canonicalization identical to 4.1
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
```

Rules:
1. `execution_identity.search_scope.sha256` is written when the search section is created and NEVER legitimately changes for the run_id. Validator recomputes it from `search.params`/`limits` at every stage validation; mismatch with the stored identity = ERROR `E_IDENTITY_TAMPER`.
2. **Resume:** before applying any same-run resume, the tool recomputes the scope fingerprint from the NEW invocation's plan; mismatch -> refuse with `E_SCOPE_FINGERPRINT_MISMATCH` -> start a new run_id with `resumed_from_run_id` pointing at the old run. Same rule for `execution_identity.qualification_criteria`: filled on first qualification admission; a later invocation under a different skill version / SKILL.md hash -> `E_CRITERIA_FINGERPRINT_MISMATCH` -> new run_id.
3. The criteria fingerprint includes the sha256 of the actual qualification SKILL.md on disk at admission time (cheap, honest, OD-7) — recorded once, immutable thereafter; drift during a run is itself an error.
4. `run_id`, `envelope_version`, `hash_version` strings are pinned inside both payloads so cross-version confusion is structurally impossible.

### 4.3 Staged validation (plan-review correction E)

`validate_envelope(env, stage)` where `stage in {search, handoff, qualification, seal}` — cumulative checks:

| Stage | Checks added |
|---|---|
| `search` | search-section structure, enums, planned-vs-per_query accounting (incl. not_run), identity rule 1 (scope fp recomputation), timestamps |
| `handoff` | search-level + full reconciliation V-1…V-7 + hash recomputation |
| `qualification` | handoff-level + qualification accounting (§5.3 V-Q1…V-Q4) + criteria identity rule |
| `seal` | everything above + declared-vs-derived consistency for every status + terminal outcome derivation |

Consequence: a post-search envelope WITHOUT a handoff section is a VALID `search`-stage artifact, but admission (`check_admission`) always validates at >= `handoff` stage, so the same artifact is `BLOCKED_INVALID_ENVELOPE` (code `E_HANDOFF_MISSING`) if qualification tries to start from it. CLI: `geo_envelope.py validate <path> --stage search|handoff|qualification|seal` (default `seal`).

## 5. State machine and status derivation

### 5.1 Search status (mechanically derived by geo_search.py, never hand-set)

Query model (correction C): `params.queries` is the PLANNED set; every planned `query_id` MUST appear in `per_query` (at least one attempt, final state one of `success | partial | failed | not_run`). Absence of a per_query record is a validation error — never inferred as success. Retries append entries with the same `query_id`, increasing `attempt`; the aggregate uses the FINAL attempt per query_id only.

```
per-query (final attempt): success | partial | failed | not_run
aggregate:  every planned query final-success      -> COMPLETE   (0 candidates is STILL COMPLETE)
            >=1 partial/not_run/failed, >=1 success -> PARTIAL
            zero attempted (all not_run)           -> FAILED + error "no_queries_attempted"
            >=1 failed, no success                 -> FAILED
            otherwise                              -> PARTIAL
consistency: search.status=COMPLETE while ANY per_query final != success -> validation ERROR at seal
```

- Zero results with success/zero is `COMPLETE`; mandated wording: "no candidates found for the executed query space" — never "GEO contains no such datasets". Execution completeness and strategy coverage remain two separate dimensions (continuity with search skill 0.4.1 "Stage gate").
- A retried/substituted query adds a new per_query entry with the same query_id and `attempt` index; aggregate status uses final attempts only; both original and substituted queries stay recorded (search skill safeguard #1 continuity).

### 5.2 Qualification admission gate (fail-closed, validator-enforced)

Qualification refuses to START (writes `status=BLOCKED_*`, stops, reports) when:
- envelope missing / invalid / unparseable -> `BLOCKED_INVALID_ENVELOPE`
- search section missing or no `status` field -> `BLOCKED_INVALID_ENVELOPE` (absence != success)
- search.status = FAILED -> `BLOCKED_UPSTREAM` (normal qualification forbidden; remediation within the same run allowed)
- search.status = PARTIAL and handoff.allowed_to_proceed != `partial_accepted` with a real acceptance block -> `BLOCKED_UPSTREAM`
- handoff content_sha256 mismatch -> `BLOCKED_INVALID_ENVELOPE`
- **Handoff reconciliation (correction A) — any violation -> `BLOCKED_INVALID_ENVELOPE`:**
  - V-1 `found_count == len(search.results.gse_ids)`
  - V-2 no duplicates inside `transferred_gse_ids` and inside `excluded` (and no GSE excluded twice)
  - V-3 `transferred ∩ excluded == ∅`
  - V-4 `set(transferred) ∪ set(excluded) == set(found)` — every found GSE accounted for; a found dataset cannot silently disappear between Search and Qualification
  - V-5 `transferred ⊆ found` and `excluded ⊆ found`
  - V-6 every excluded entry carries a valid `reason_code` (control-flow vocabulary of 5.4a) + `reason_note` + `by` + `at_utc`
  - V-7 recomputed handoff hash == stored `content_sha256` (see 4.1)

### 5.3 Qualification completion status (derived from its own results)

Semantics (plan-review correction D — `derive_qualification_status(qual_section, transferred_gse_ids)` MUST receive the handoff list; status is computed against what was PROMISED, not what the section mentions):
- `processed_gse_ids` — GSEs with a final dataset-level decision (present in `decisions.include|exclude|uncertain`).
- `failed_gse_ids` — attempted but not evaluable (e.g. metadata fetch exhausted retries); each MUST appear in `errors` with its gse_id.
- `skipped_gse_ids` — deliberately not evaluated; each MUST carry a reason in `skipped_reasons` (control-flow only; biological non-assessment is not a valid skip — that is what `exclude` is for).

Accounting invariants (any violation = validation ERROR, never a silently downgraded status):
```
V-Q1  set(processed) ∪ set(failed) ∪ set(skipped) == set(transferred)   # complete accounting
V-Q2  pairwise disjoint (no GSE in two contradictory categories)
V-Q3  set(decisions.include) ∪ set(decisions.exclude) ∪ set(decisions.uncertain)
      == set(processed), the three decision lists pairwise disjoint
V-Q4  no gse_id outside `transferred` appears anywhere in the section
```

Status derivation:
```
transferred == []                                   -> NOT_APPLICABLE
any V-Q invariant violated                          -> validation ERROR (no status derived)
failed == [] and skipped == [] (all processed)      -> COMPLETE
>=1 processed and (failed or skipped non-empty)     -> PARTIAL
processed == [] and all failed/skipped              -> FAILED
```

COMPLETE cannot be derived unless every transferred GSE is properly accounted for. All-transferred-qualified-but-no-include is **COMPLETE** with `decisions.include == []` — a successful evaluation with an empty business result, not a failed run.

### 5.4 Pipeline outcome (terminal, derived — never hand-set)

- `SUCCESS` — search COMPLETE + qualification COMPLETE + >=1 include
- `EMPTY_COMPLETE` — search COMPLETE + qualification COMPLETE + 0 include
- `NO_CANDIDATES` — search COMPLETE + `found == 0` + qualification NOT_APPLICABLE — never phrased as absence in GEO
- `ALL_EXCLUDED` — **APPROVED (OD-9)** — exact conditions in 5.4a; `NO_CANDIDATES` and `EMPTY_COMPLETE` semantics unchanged
- `PARTIAL_SUCCESS` — any stage PARTIAL, or partial_accepted handoff, or skipped/failed GSEs inside an otherwise finished run
- `FAILURE` — any stage FAILED / unresolved BLOCKED
- OPEN envelope -> no outcome may be reported.

Mapping of the eight situations from the approval brief:
1. search ok + candidates -> SUCCESS | 2. search ok + zero -> NO_CANDIDATES | 3. partial search -> PARTIAL_SUCCESS (accepted) or FAILURE (refused) | 4. search failed -> FAILURE | 5. none qualified -> EMPTY_COMPLETE | 6. qualification partial -> PARTIAL_SUCCESS | 7. qualification ok -> SUCCESS/EMPTY_COMPLETE | 8. pipeline partial/error -> PARTIAL_SUCCESS/FAILURE.

### 5.4a All found candidates excluded before Qualification (correction F)

Situation: search COMPLETE, found >= 1, handoff transfers nothing (all excluded with reasons), qualification not run.

- NOT `NO_CANDIDATES`: candidates WERE found; the label would falsely report an empty discovery.
- NOT `EMPTY_COMPLETE`: that outcome means qualification RAN and passed no dataset; here no biological assessment happened at all — conflating them hides the fact that no eligibility was ever evaluated.
- NOT `SUCCESS`: there is no qualified dataset and the excluded ones carry no qualification evidence.
- NOT `PARTIAL_SUCCESS`/`FAILURE`: control flow completed cleanly; nothing failed.

**DECISION (OD-9 approved 2026-11-23): `ALL_EXCLUDED` is the sixth terminal outcome.** It applies iff ALL of: (1) `search.status == COMPLETE`; (2) `found_count > 0`; (3) `transferred_gse_ids == []`; (4) every found GSE appears in `excluded` (V-4 holds); (5) every exclusion has valid `reason_code` + `reason_note` + `by` + `at_utc` (V-6); (6) `qualification.status == NOT_APPLICABLE`. Any partial match falls back to ordinary rules (a reconciliation gap is an error, not an outcome). Original recommendation text: It is a *completed control flow with a pre-qualification business decision*, and it must surface loudly because those GSEs were catalogued as found but never assessed — the exact profile of datasets that silently vanish between stages today. Historical alternative (superseded by the OD-9 approval): `NO_CANDIDATES` with a `candidates_found_but_excluded` qualifier — rejected because it overloads one label with two opposite semantics (empty discovery vs full exclusion).

Guard: handoff exclusions are CONTROL-FLOW only (enum `already_registered | out_of_scope | duplicate_superseries | user_directive | other`). An eligibility-flavoured exclusion (e.g. "looks non-IBD") is a Qualification decision and must go through transferred + `exclude`, never through handoff — enforced by vocabulary + qualification skill prose; keeps the responsibility split of section 2 intact.

### 5.5 Re-delivery, idempotency, resume (OD-5, extended by correction D)

- Same envelope re-delivered (same run_id + same content_sha256): qualification SKIPS already-processed GSEs, appends a `repeated_handoff` warning — idempotent no-op.
- Same run_id, different content -> hard error `ENVELOPE_CONFLICT` — never silently merged.
- Interrupted stage: re-run within the same run_id skips GSEs in `processed_gse_ids`; `resume.attempt` increments.

Correction D — event taxonomy (approved rule: same run_id == same logical execution):

| Event | run_id | Mechanism |
|---|---|---|
| Query retry (transient HTTP, substitution) | same | append `per_query` attempt entry; aggregate from final attempts; original + substituted queries both retained |
| Technical resume of interrupted run (crash, restart) | same | `resume.attempt`++, skip entries already in processed lists; **precondition: recomputed `search_scope` fingerprint identical to stored identity (§4.2 rule 2)** |
| Re-delivery of the same handoff | same | hash match -> idempotent skip + `repeated_handoff` warning; hash mismatch -> `ENVELOPE_CONFLICT` |
| Change of search scope (add/remove/modify planned queries, max_results, species constraint, pagination flag) | **new** | detected by `E_SCOPE_FINGERPRINT_MISMATCH`; new envelope + `resumed_from_run_id` set; old envelope sealed or left OPEN-with-FAILED, never edited retroactively |
| Change of qualification criteria (skill version / profile / SKILL.md content) | **new** | detected by `E_CRITERIA_FINGERPRINT_MISMATCH`; same-run continuation is a validation error |

### 5.6 Envelope write-safety: separate lock file + atomic replacement (plan-review correction B)

`fcntl.flock` directly on `envelope.json` is UNSAFE with `os.replace`: the replace swaps the inode, so a process holding a lock on the old inode excludes nobody, and two new writers can lock two different inodes simultaneously. Protocol (stdlib only: `fcntl`, `os`, `tempfile`):

```python
with open(run_dir / "envelope.json.lock", "a+") as lock_fh:      # stable, never replaced
    fcntl.flock(lock_fh, fcntl.LOCK_EX)                          # advisory, cross-process
    env = json.loads((run_dir / "envelope.json").read_text())    # stage read
    env = mutate_and_validate_at_stage(env, stage)               # fail-closed BEFORE write
    tmp = run_dir / f"envelope.json.tmp.{os.getpid()}"           # same filesystem
    tmp.write_text(canonical_json_bytes(env).decode("utf-8"))
    tmp.flush(); os.fsync(tmp.fileno())
    os.replace(tmp, run_dir / "envelope.json")                   # atomic swap
    dir_fd = os.open(run_dir, os.O_DIRECTORY); os.fsync(dir_fd); os.close(dir_fd)
    # lock released on close()
```

Guarantees: mutual exclusion via the never-replaced `.lock` sibling; readers never observe torn files (atomic replace + fsync); a crash between write and replace leaves only a stale `.tmp.<pid>`. Consistent-snapshot readers take `LOCK_SH` on the same lock file. Tests must include a real two-process contention case (Task 4), not a single-threaded simulation.

### 5.7 Post-seal immutability (plan-review correction C)

Once `sealed_at_utc` is set: every mutation command (`build-handoff`, `refuse`, `resume`, section writes, `seal`) rejects with `E_ENVELOPE_SEALED` BEFORE writing anything; file bytes MUST be identical after any rejected attempt (verified via hash in tests); `seal` on an already-sealed envelope with byte-identical canonical content is a no-op returning `W_ALREADY_SEALED` (never rewrites timestamps); with different content it fails `E_ENVELOPE_SEALED`. Read commands (`validate`, `derive`, `report`) remain allowed at all times.

## 6. Partial-failure handling rules

1. Successful queries' records are always preserved in results (already true in geo_search.py — reused).
2. Failed/partial queries are recorded verbatim in `per_query[].error`; the aggregate can never "forget" them; sealing requires every known limitation to appear in `errors`/`warnings` at envelope top level.
3. `PARTIAL` search must never be labelled, reported or sealed as complete — enforced by the validator, not by prose.
4. Qualification may work on the correctly collected subset ONLY via `allowed_to_proceed: partial_accepted` + explicit user-acceptance block (OD-4).
5. Every downstream report must include the non-coverage statement generated from the envelope (queries whose coverage failed) — structurally: `geo_envelope.py report` prints it; agents may not omit it.

## 7. Versioning and auditability

- `envelope_version` (contract) + per-section `skill.version` (read from frontmatter at runtime) + tool version constant (`geo_search.py TOOL_VERSION`).
- `criteria_profile` ties decisions to the qualification ruleset revision; recording the SKILL.md sha256 alongside the version string is cheap and honest (OD-7).
- Every stage section: started/finished timestamps, actor (`"agent"` / `"user"` for acceptances).
- Lineage chain: envelope -> run-dir artifacts (raw per-query JSON, metadata logs, sample-results files) -> DuckDB rows (via gse_id + qualified_at within the run window; exact DB FK deferred, OD-6).
- Audit target: given any `geo_qualification` row and the matching sealed envelope, a third party can reconstruct which query found the dataset, which handoff transferred it, which criteria version decided it, when, and with what evidence.

## 8. Execution vs persistence (independence)

- **Execution** = envelope stage sections (status, timestamps, IDs) — file-based, works with no database at all.
- **Handoff** = `handoff` section (IDs + counts + hash) — the only input interface qualification reads.
- **Qualification** = `qualification` section — decisions live here first.
- **Persistence** = `qualification.persistence` — a REFERENCE block (media, path, tables, verified counts), OPTIONAL. A DB write is never a success signal by itself; the run succeeds only when sealed with derived `pipeline.outcome == SUCCESS`.
- DuckDB projections (mirror table, `run_id` column on qualification rows) are possible future enhancements (OD-6); no tool may assume them today.

## 9. Scenarios (validator-assertable)

- **S1 happy path:** search COMPLETE, qual COMPLETE, include>=1 -> SUCCESS.
- **S2 zero candidates:** search COMPLETE(0 ids), qual NOT_APPLICABLE -> NO_CANDIDATES (requires found==0).
- **S10 all found excluded (F, OD-9 approved):** search COMPLETE found=5, handoff excluded=5/transfer=0, qual NOT_APPLICABLE -> ALL_EXCLUDED; labelling this `NO_CANDIDATES` or `EMPTY_COMPLETE` is a validator error.
- **S11 unaccounted found GSE (A):** handoff transfers 3 of 313 found, excluded lists 309 (one missing) or lists one GSE absent from found -> `BLOCKED_INVALID_ENVELOPE` (reconciliation invariant violated).
- **S12 no query attempted (C):** all planned queries `not_run` -> search FAILED with `no_queries_attempted`; any COMPLETE claim -> validation error.
- **S3 partial accepted:** q7 failed, user accepts, subset qualified COMPLETE -> PARTIAL_SUCCESS + non-coverage list.
- **S4 all queries failed:** search FAILED -> qual BLOCKED_UPSTREAM -> FAILURE.
- **S5 invalid envelope:** missing status/garbage -> BLOCKED_INVALID_ENVELOPE, refusal recorded, DB untouched.
- **S6 duplicate handoff:** same run_id + same hash -> idempotent skip + warning.
- **S7 mid-run metadata failure (links to ETAP 5):** GSE12 fetch fails after retries -> qual PARTIAL(failed_gse_ids=[GSE12]) -> PARTIAL_SUCCESS.
- **S8 conflicting re-delivery:** same run_id, different content -> ENVELOPE_CONFLICT.
- **S9 none passed:** qual COMPLETE, include=[] -> EMPTY_COMPLETE.
- **S13 scope drift on resume (A):** same run_id, new invocation plans an extra/modified query -> `E_SCOPE_FINGERPRINT_MISMATCH`, resume refused; must fork with `resumed_from_run_id`.
- **S14 criteria drift (A):** same run continues after qualification began with a different qualification SKILL.md sha256 -> `E_CRITERIA_FINGERPRINT_MISMATCH`.
- **S15 seal immutability (C):** valid envelope seals; any later mutation -> `E_ENVELOPE_SEALED`, file bytes identical; re-seal of identical content -> no-op + `W_ALREADY_SEALED`.
- **S16 concurrent writers (B):** two processes mutate the same envelope -> `.lock` serializes; no lost update; final envelope passes `seal`-stage validation.

## 10. Proposed changes (NOT applied)

1. `geo_search.py` — additive envelope output: `envelope_version`, `run_id`, aggregate `status` (derived per 5.1), `tool_version`, timestamps, `--output-envelope <path>`; existing keys unchanged (backward compatible). [OD-1]
2. NEW `.agents/tools/sc_dataset/geo_envelope.py` — build/validate (fail-closed per 4-5), derive aggregate + outcome, seal, verify hash, detect duplicate/conflicting handoff, `report` with non-coverage statements; pure JSON, no DB; full unit tests via TDD in implementation.
3. `sc-dataset-search/SKILL.md` — document the emitted envelope section; stage-gate text references `allowed_to_proceed` derivation instead of prose-only judgment. Version 0.4.1 -> 0.5.0.
4. `sc-dataset-qualification/SKILL.md` — new REQUIRED precondition block: validated envelope path; the six refusals of 5.2; writes its own qualification section; never infers status from DB state. Version 0.2.0 -> 0.3.0.
5. `tests/` — envelope validator fixtures for S1-S9 (no DB dependency; uses geo_db fixture only where persistence REFERENCE blocks are parsed).
6. Pipeline skill — mirror `geo-dataset-production-pipeline` into the repo and replace its prose completeness gates with "run geo_envelope.py validate before/after each stage". [F-4-adjacent, separate decision]
7. `geo_database.py` — NO change in this stage.

Per writing-skills: SKILL.md edits must be RED/GREEN pressure-tested with fresh subagents (missing status, PARTIAL-as-COMPLETE, FAILED-input start) before deployment — implementation-phase work, not this design.

## 11. Open decisions (user required)

- **OD-1** Extend `geo_search.py` (touches the "closed" search stage; additive + backward compatible) vs keep it frozen and let the orchestrator assemble the envelope from raw stdout. **Recommendation: extend** — the tool is the only component that can report its own truth (structural > prompt-based).
- **OD-2** Carrier: file-per-run `data/reports/run_<id>/envelope.json` (recommended; persistence-independent; existing convention) vs stdin-only handoff.
- **OD-3** Validator without new dependencies (recommended; pyproject untouched) vs adding `jsonschema` (needs explicit user decision per agent-development rule 5).
- **OD-4** PARTIAL->qualification policy: allow only with explicit user-acceptance block (recommended; matches search skill gate #5) vs forbid entirely.
- **OD-5** Resume default: continue same run_id (recommended; one identity) vs new run_id with `resumed_from_run_id`.
- **OD-6** DB provenance: envelope-only now (recommended; respects independence, avoids schema change) vs add run_id column/projection in this cycle.
- **OD-7** Criteria fingerprint: version + SKILL.md sha256 (recommended) vs version string only.
- **OD-8** Terminal vocabulary: adopt `SUCCESS | EMPTY_COMPLETE | NO_CANDIDATES | PARTIAL_SUCCESS | FAILURE` (recommended) vs custom set.

**Decisions log:** OD-1…OD-8 APPROVED (2026-11-23). **OD-9 (`ALL_EXCLUDED`) APPROVED (2026-11-23)** with conditions in 5.4a; the 5→6-term change is limited to the outcome dictionary — `NO_CANDIDATES`/`EMPTY_COMPLETE` semantics untouched. OD-10 (opt-in vs default envelope emission) closed in the plan as **opt-in flags** (direct consequence of the OD-1 backward-compatibility requirement).

## Risks

- Touching geo_search.py threatens a "closed" stage (mitigation: additive keys; existing 675-line search suite as regression net).
- Envelope discipline in agent flows (mitigation: downstream tools refuse to run without a valid envelope — structural enforcement).
- The unversioned profile pipeline skill can drift from repo contracts (mitigation: mirror into repo — separate F-4 decision).
