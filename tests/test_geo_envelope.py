from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from tools.sc_dataset.geo_envelope import (
    compute_fingerprint,
    compute_handoff_hash,
    criteria_payload,
    envelope_skeleton,
    scope_payload,
)


def base_env() -> dict:
    return {
        "envelope_version": "1.0", "run_id": "20261123T170500Z-3f9a1c2e",
        "search": {"skill": {"name": "sc-dataset-search", "version": "0.5.0"},
                   "tool": "geo_search.py", "tool_version": "1.1.0",
                   "finished_at_utc": "2026-11-23T17:12:41Z",
                   "results": {"gse_ids": ["GSE2", "GSE1"]},
                   "params": {"queries": [{"query_id": "q1", "query": "alpha", "max_results": 10}],
                              "species_constraint": "human"},
                   "limits": {"retstart_pagination": False}},
        "handoff": {"transferred_gse_ids": ["GSE2", "GSE1"],
                    "excluded": [{"gse_id": "GSE3", "reason_code": "other", "reason_note": "note",
                                  "by": "agent", "at_utc": "2026-11-23T17:12:41Z"}],
                    "allowed_to_proceed": "complete", "created_at_utc": "2026-11-23T17:20:02Z"},
    }


def test_handoff_hash_is_canonical_and_stable() -> None:
    env = base_env()
    env["search"]["results"]["gse_ids"] = ["GSE1", "GSE2", "GSE3"]
    env["handoff"]["transferred_gse_ids"] = ["GSE2", "GSE1"]
    digest = compute_handoff_hash(env)
    reordered = deepcopy(env)
    reordered["handoff"]["transferred_gse_ids"].reverse()
    reordered["handoff"]["excluded"].reverse()
    reordered["qualification"] = {"status": "COMPLETE"}
    assert compute_handoff_hash(reordered) == digest
    changed = deepcopy(env)
    changed["handoff"]["transferred_gse_ids"][0] = "GSE9"
    assert compute_handoff_hash(changed) != digest

def test_reason_code_change_changes_handoff_hash() -> None:
    env = base_env()
    before = compute_handoff_hash(env)
    env["handoff"]["excluded"][0]["reason_code"] = "user_directive"
    assert compute_handoff_hash(env) != before

def test_reason_note_change_does_not_change_handoff_hash() -> None:
    env = base_env()
    before = compute_handoff_hash(env)
    env["handoff"]["excluded"][0]["reason_note"] = "updated"
    env["handoff"]["excluded"][0]["by"] = "user"
    env["handoff"]["excluded"][0]["at_utc"] = "2026-11-24T00:00:00Z"
    env["handoff"]["acceptance"] = {"reason": "metadata"}
    assert compute_handoff_hash(env) == before

def test_provenance_change_detected_in_handoff_hash() -> None:
    env = base_env()
    before = compute_handoff_hash(env)
    for path, value in [("tool_version", "1.2.0"), ("finished_at_utc", "2026-11-24T00:00:00Z")]:
        changed = deepcopy(env)
        changed["search"][path] = value
        assert compute_handoff_hash(changed) != before
    changed = deepcopy(env)
    changed["search"]["skill"]["version"] = "0.6.0"
    assert compute_handoff_hash(changed) != before

def test_scope_fingerprint_binds_queries_max_results_species_pagination() -> None:
    env = base_env()
    scope = compute_fingerprint(scope_payload(env["search"]))
    for mutate in (
        lambda e: e["search"]["params"]["queries"][0].update(query="beta"),
        lambda e: e["search"]["params"]["queries"][0].update(max_results=11),
        lambda e: e["search"]["params"].update(species_constraint="mouse"),
        lambda e: e["search"]["limits"].update(retstart_pagination=True),
    ):
        changed = deepcopy(env)
        mutate(changed)
        assert compute_fingerprint(scope_payload(changed["search"])) != scope
    reordered = deepcopy(env)
    reordered["search"]["params"]["queries"].append(
        {"query_id": "q0", "query": "zero", "max_results": 2}
    )
    reverse = deepcopy(reordered)
    reverse["search"]["params"]["queries"].reverse()
    assert compute_fingerprint(scope_payload(reordered["search"])) == compute_fingerprint(scope_payload(reverse["search"]))

def test_criteria_fingerprint_binds_version_and_skill_md_sha256() -> None:
    qual = {"skill": {"name": "sc-dataset-qualification", "version": "0.3.0"},
            "criteria_profile": {"name": "default", "skill_sha256": "abc"}}
    before = compute_fingerprint(criteria_payload(qual))
    changed = deepcopy(qual)
    changed["criteria_profile"]["skill_sha256"] = "def"
    assert compute_fingerprint(criteria_payload(changed)) != before
    changed = deepcopy(qual)
    changed["skill"]["version"] = "0.4.0"
    assert compute_fingerprint(criteria_payload(changed)) != before

def test_skeleton_identity_matches_params_recomputation() -> None:
    env = envelope_skeleton(
        "20261123T170500Z-3f9a1c2e", "2026-11-23T17:05:00Z",
        {"name": "sc-dataset-search", "version": "0.5.0"}, "geo_search.py", "1.1.0",
        {"queries": [{"query_id": "q1", "query": "alpha", "max_results": 10}],
         "species_constraint": "human"}, {"retstart_pagination": False},
    )
    assert env["execution_identity"]["search_scope"]["sha256"] == compute_fingerprint(scope_payload(env["search"]))

def test_new_run_id_format() -> None:
    import re

    from tools.sc_dataset.geo_envelope import new_run_id
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", new_run_id())


from tools.sc_dataset.geo_envelope import (
    check_admission,
    derive_search_status,
    validate_envelope,
)


def search_env() -> dict:
    from tools.sc_dataset.geo_envelope import envelope_skeleton
    env = envelope_skeleton(
        "20261123T170500Z-3f9a1c2e", "2026-11-23T17:05:00Z",
        {"name": "sc-dataset-search", "version": "0.5.0"}, "geo_search.py", "1.1.0",
        {"queries": [{"query_id": "q1", "query": "alpha", "max_results": 10}],
         "species_constraint": "human"}, {"retstart_pagination": False},
    )
    env["search"].update({"status": "COMPLETE", "per_query": [{"query_id": "q1", "status": "success", "count": 1, "retrieved": 1, "attempt": 1}],
                           "results": {"unique_gse_count": 1, "gse_ids": ["GSE1"], "dropped_no_gse": 0},
                           "finished_at_utc": "2026-11-23T17:12:41Z"})
    return env


def test_missing_search_status_blocks() -> None:
    env = search_env()
    del env["search"]["status"]
    assert "E_STATUS_MISSING" in {i.code for i in validate_envelope(env, "search")}


def test_unknown_search_status_is_error() -> None:
    env = search_env()
    env["search"]["status"] = "MAGIC"
    assert "E_UNKNOWN_ENUM" in {i.code for i in validate_envelope(env, "search")}


def test_absent_per_query_record_is_error() -> None:
    env = search_env()
    env["search"]["per_query"] = []
    assert "E_QUERY_UNACCOUNTED" in {i.code for i in validate_envelope(env, "search")}


def test_unknown_query_id_is_error() -> None:
    env = search_env()
    env["search"]["per_query"][0]["query_id"] = "other"
    codes = {i.code for i in validate_envelope(env, "search")}
    assert "E_QUERY_ID_UNKNOWN" in codes


def test_attempt_sequence_must_be_contiguous() -> None:
    env = search_env()
    env["search"]["per_query"] += [{"query_id": "q1", "status": "success", "attempt": 3}]
    assert "E_ATTEMPT_SEQUENCE" in {i.code for i in validate_envelope(env, "search")}


def test_non_z_timestamp_is_error() -> None:
    env = search_env()
    env["search"]["finished_at_utc"] = "2026-11-23T17:12:41+00:00"
    assert "E_TIMESTAMP_FORMAT" in {i.code for i in validate_envelope(env, "search")}


def test_post_search_without_handoff_is_valid_only_at_search_stage() -> None:
    env = search_env()
    assert not [i for i in validate_envelope(env, "search") if i.severity == "ERROR"]
    assert "E_HANDOFF_MISSING" in {i.code for i in validate_envelope(env, "handoff")}
    status, issues = check_admission(env)
    assert status == "BLOCKED_INVALID_ENVELOPE"
    assert "E_HANDOFF_MISSING" in {i.code for i in issues}

def test_search_status_derivation_uses_final_attempt() -> None:
    assert derive_search_status(["q1"], [{"query_id": "q1", "status": "failed", "attempt": 1}, {"query_id": "q1", "status": "success", "attempt": 2}]) == "COMPLETE"


def test_unknown_search_field_is_rejected() -> None:
    env = search_env()
    env["search"]["surprise"] = True
    assert "E_UNKNOWN_FIELD" in {i.code for i in validate_envelope(env, "search")}

def test_duplicate_attempt_numbers_are_rejected() -> None:
    env = search_env()
    env["search"]["per_query"].append(dict(env["search"]["per_query"][0]))
    assert "E_ATTEMPT_SEQUENCE" in {i.code for i in validate_envelope(env, "search")}


from tools.sc_dataset.geo_envelope import (
    EnvelopeError,
    build_handoff,
    derive_pipeline_outcome,
    derive_qualification_status,
    seal,
)


def handoff_env() -> dict:
    env = search_env()
    return build_handoff(env, ["GSE1"], [], now="2026-11-23T17:20:02Z")


def valid_qual(ids: list[str]) -> dict:
    return {
        "skill": {"name": "sc-dataset-qualification", "version": "0.3.0"},
        "criteria_profile": {"name": "default", "skill_version": "0.3.0", "skill_sha256": "abc"},
        "status": "COMPLETE", "processed_gse_ids": list(ids), "failed_gse_ids": [],
        "skipped_gse_ids": [], "decisions": {"include": list(ids), "exclude": [], "uncertain": []},
        "sample_counts": {"include": len(ids), "exclude": 0, "uncertain": 0},
        "errors": [], "skipped_reasons": [], "finished_at_utc": "2026-11-23T17:30:00Z",
    }

def test_declared_status_must_match_derived() -> None:
    env = search_env()
    env["search"]["status"] = "COMPLETE"
    env["search"]["per_query"][0]["status"] = "partial"
    assert "E_STATUS_MISSING" in {i.code for i in validate_envelope(env, "seal")}

def test_not_run_forbids_complete() -> None:
    env = search_env()
    env["search"]["per_query"][0]["status"] = "not_run"
    assert "E_STATUS_MISSING" in {i.code for i in validate_envelope(env, "seal")}

def test_reconciliation_count_and_hash_mismatch_block() -> None:
    env = handoff_env()
    env["handoff"]["found_count"] = 9
    env["handoff"]["content_sha256"] = "0" * 64
    codes = {i.code for i in validate_envelope(env, "handoff")}
    assert {"E_HANDOFF_V1", "E_HANDOFF_V7"} <= codes

def test_reconciliation_missing_found_id_blocks() -> None:
    env = search_env()
    invalid = dict(env)
    invalid["handoff"] = {"found_count": 1, "transferred_gse_ids": [], "excluded": [], "allowed_to_proceed": "complete", "created_at_utc": "2026-11-23T17:20:02Z", "content_sha256": ""}
    assert "E_HANDOFF_V4" in {i.code for i in validate_envelope(invalid, "handoff")}

def test_qual_complete_requires_all_transferred_accounted() -> None:
    qual = valid_qual(["GSE1"])
    qual["processed_gse_ids"] = []
    qual["decisions"]["include"] = []
    with pytest.raises(ValueError):
        derive_qualification_status(qual, ["GSE1"])

def test_gse_in_two_qualification_categories_is_error() -> None:
    qual = valid_qual(["GSE1"])
    qual["failed_gse_ids"] = ["GSE1"]
    qual["errors"] = [{"gse_id": "GSE1", "detail": "failed"}]
    with pytest.raises(ValueError, match="E_UNKNOWN_FIELD"):
        derive_qualification_status(qual, ["GSE1"])

def test_decisions_must_partition_processed_ids() -> None:
    qual = valid_qual(["GSE1"])
    qual["decisions"]["uncertain"] = ["GSE1"]
    with pytest.raises(ValueError, match="E_UNKNOWN_FIELD"):
        derive_qualification_status(qual, ["GSE1"])

def test_untransferred_gse_in_qualification_is_error() -> None:
    qual = valid_qual(["GSE1", "GSE2"])
    with pytest.raises(ValueError, match="E_QUERY_ID_UNKNOWN"):
        derive_qualification_status(qual, ["GSE1"])

def test_failed_entries_require_details_and_empty_is_not_applicable() -> None:
    qual = valid_qual([])
    assert derive_qualification_status(qual, []) == "NOT_APPLICABLE"
    failed = valid_qual([])
    failed["processed_gse_ids"] = []
    failed["failed_gse_ids"] = ["GSE1"]
    failed["decisions"] = {"include": [], "exclude": [], "uncertain": []}
    with pytest.raises(ValueError):
        derive_qualification_status(failed, ["GSE1"])

def test_all_excluded_exact_conditions() -> None:
    env = search_env()
    env["search"]["results"]["gse_ids"] = ["GSE1"]
    excluded = [{"gse_id": "GSE1", "reason_code": "other", "reason_note": "reason", "by": "user", "at_utc": "2026-11-23T17:20:02Z"}]
    env["handoff"] = build_handoff(env, [], excluded, now="2026-11-23T17:20:02Z")["handoff"]
    env["qualification"] = {"status": "NOT_APPLICABLE"}
    assert derive_pipeline_outcome(env) == "ALL_EXCLUDED"
    for key, value in [("search_status", "PARTIAL"), ("found_count", 0), ("transferred", ["GSE1"]), ("excluded_reason", None), ("qualification", "COMPLETE")]:
        changed = __import__("copy").deepcopy(env)
        if key == "search_status": changed["search"]["status"] = value
        elif key == "found_count": changed["handoff"]["found_count"] = value
        elif key == "transferred": changed["handoff"]["transferred_gse_ids"] = value
        elif key == "excluded_reason": changed["handoff"]["excluded"][0]["reason_code"] = "INVALID"
        else: changed["qualification"]["status"] = value
        assert derive_pipeline_outcome(changed) != "ALL_EXCLUDED"

def test_no_candidates_vs_all_excluded_separation() -> None:
    env = search_env()
    env["search"]["results"]["gse_ids"] = []
    env["search"]["results"]["unique_gse_count"] = 0
    env["handoff"] = build_handoff(env, [], [], now="2026-11-23T17:20:02Z")["handoff"]
    env["qualification"] = {"status": "NOT_APPLICABLE"}
    assert derive_pipeline_outcome(env) == "NO_CANDIDATES"

def test_open_envelope_has_no_outcome() -> None:
    assert derive_pipeline_outcome(search_env()) is None


def test_all_excluded_exclusion_metadata_required() -> None:
    env = search_env()
    env["search"]["results"]["gse_ids"] = ["GSE1"]
    entry = {"gse_id": "GSE1", "reason_code": "other", "reason_note": "reason", "by": "user", "at_utc": "2026-11-23T17:20:02Z"}
    env["handoff"] = build_handoff(env, [], [entry], now="2026-11-23T17:20:02Z")["handoff"]
    env["qualification"] = {"status": "NOT_APPLICABLE"}
    for field in ("reason_code", "reason_note", "by", "at_utc"):
        changed = deepcopy(env)
        changed["handoff"]["excluded"][0][field] = "INVALID" if field == "reason_code" else ""
        assert derive_pipeline_outcome(changed) != "ALL_EXCLUDED"


from tools.sc_dataset.geo_envelope import (
    apply_resume,
)


def test_duplicate_handoff_is_idempotent_noop() -> None:
    env = handoff_env()
    again = build_handoff(env, ["GSE1"], [], now="2026-11-23T17:20:02Z")
    assert again["handoff"]["content_sha256"] == env["handoff"]["content_sha256"]
    assert any(w.get("code") == "W_REPEATED_HANDOFF" for w in again["search"]["warnings"])

def test_conflicting_handoff_same_run_id_fails() -> None:
    env = handoff_env()
    with pytest.raises(EnvelopeError, match="E_ENVELOPE_CONFLICT"):
        build_handoff(env, [], [{"gse_id": "GSE1", "reason_code": "other", "reason_note": "no", "by": "user", "at_utc": "2026-11-23T17:20:02Z"}], now="2026-11-23T17:20:02Z")

def test_resume_scope_drift_refused() -> None:
    env = search_env()
    invocation = deepcopy(env)
    invocation["search"]["params"]["queries"][0]["query"] = "changed"
    with pytest.raises(EnvelopeError, match="E_SCOPE_FINGERPRINT_MISMATCH"):
        apply_resume(env, invocation)

def test_resume_identical_scope_increments_attempt() -> None:
    env = search_env()
    resumed = apply_resume(env, deepcopy(env))
    assert resumed["resume"]["attempt"] == 2

def test_resume_criteria_drift_refused(tmp_path) -> None:
    from tools.sc_dataset.geo_envelope import write_qualification_section
    skill = tmp_path / "skill.md"
    skill.write_bytes(b"first")
    qual = valid_qual(["GSE1"])
    env = handoff_env()
    written = write_qualification_section(env, qual, skill, now="2026-11-23T17:30:00Z")
    skill.write_bytes(b"second")
    with pytest.raises(EnvelopeError, match="E_CRITERIA_FINGERPRINT_MISMATCH"):
        write_qualification_section(written, qual, skill, now="2026-11-23T17:31:00Z")

def test_seal_success_and_rejects_inconsistency() -> None:
    env = handoff_env()
    env["qualification"] = valid_qual(["GSE1"])
    from tools.sc_dataset.geo_envelope import compute_fingerprint, criteria_payload
    env["execution_identity"]["qualification_criteria"] = {"payload_version": "criteria-fp-v1", "sha256": compute_fingerprint(criteria_payload(env["qualification"]))}
    sealed = seal(env, now="2026-11-23T17:40:00Z")
    assert sealed["sealed_at_utc"] == "2026-11-23T17:40:00Z"
    bad = deepcopy(env)
    bad["search"]["per_query"][0]["status"] = "partial"
    with pytest.raises(EnvelopeError):
        seal(bad)

def test_mutation_after_seal_blocked_bytes_identical(tmp_path) -> None:
    from tools.sc_dataset.geo_envelope import _mutate_path, write_envelope_atomic
    env = handoff_env()
    env["sealed_at_utc"] = "2026-11-23T17:40:00Z"
    path = tmp_path / "envelope.json"
    write_envelope_atomic(path, env)
    before = path.read_bytes()
    with pytest.raises(EnvelopeError, match="E_ENVELOPE_SEALED"):
        _mutate_path(path, lambda current: current.update({"other": True}))
    assert path.read_bytes() == before
    from tools.sc_dataset.geo_envelope import seal
    valid = handoff_env()
    valid["qualification"] = valid_qual(["GSE1"])
    from tools.sc_dataset.geo_envelope import compute_fingerprint, criteria_payload
    valid["execution_identity"]["qualification_criteria"] = {"payload_version": "criteria-fp-v1", "sha256": compute_fingerprint(criteria_payload(valid["qualification"]))}
    sealed = seal(valid, now="2026-11-23T17:40:00Z")
    already = seal(sealed, now="2026-11-23T17:40:00Z")
    assert any(w.get("code") == "W_ALREADY_SEALED" for w in already["search"]["warnings"])
    assert sealed["sealed_at_utc"] == "2026-11-23T17:40:00Z"

def test_two_processes_concurrent_mutation_no_lost_update(tmp_path) -> None:
    import subprocess
    import sys
    from pathlib import Path

    from tools.sc_dataset.geo_envelope import write_envelope_atomic
    env = handoff_env()
    path = tmp_path / "envelope.json"
    write_envelope_atomic(path, env)
    worker = "from tools.sc_dataset.geo_envelope import _mutate_path; import sys; p=sys.argv[1]; n=int(sys.argv[2]); exec(\"def update(e):\\n e[\\\"search\\\"][\\\"warnings\\\"].append({\\\"worker\\\": n}); return e\"); [ _mutate_path(p, update) for i in range(15) ]"
    procs = [subprocess.Popen([sys.executable, "-c", worker, str(path), str(n)], env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / ".agents")}) for n in (1, 2)]
    assert [proc.wait(timeout=10) for proc in procs] == [0, 0]
    final = __import__("json").loads(path.read_text())
    assert sum(w.get("worker") in (1, 2) for w in final["search"]["warnings"]) == 30


def test_readers_never_observe_torn_file(tmp_path) -> None:
    import json
    import threading

    from tools.sc_dataset.geo_envelope import _mutate_path, write_envelope_atomic
    path = tmp_path / "envelope.json"
    write_envelope_atomic(path, handoff_env())
    errors = []
    done = threading.Event()
    def reader():
        while not done.is_set():
            try: json.loads(path.read_bytes())
            except (ValueError, OSError) as exc: errors.append(exc); break
    thread = threading.Thread(target=reader); thread.start()
    for index in range(20): _mutate_path(path, lambda env, value=index: env["search"]["warnings"].append({"i": value}))
    done.set(); thread.join(timeout=2)
    assert not thread.is_alive() and errors == []
    assert path.with_name("envelope.json.lock").exists()


def test_stage_validation_has_required_exact_codes(tmp_path) -> None:
    env = search_env()
    assert "E_STATUS_MISSING" in {i.code for i in validate_envelope({"search": {}}, "search")}
    assert "E_HANDOFF_MISSING" in {i.code for i in validate_envelope(env, "handoff")}
    env["execution_identity"]["search_scope"]["sha256"] = "0" * 64
    assert "E_IDENTITY_TAMPER" in {i.code for i in validate_envelope(env, "search")}

def test_hash_recomputation_mismatch_blocks() -> None:
    env = handoff_env()
    env["handoff"]["content_sha256"] = "0" * 64
    codes = {i.code for i in validate_envelope(env, "handoff")}
    assert {"E_HANDOFF_V7", "E_HASH_MISMATCH"} <= codes


def test_resume_criteria_drift_refused_by_resume_gate(tmp_path) -> None:
    from tools.sc_dataset.geo_envelope import write_qualification_section
    skill = tmp_path / "skill.md"
    skill.write_bytes(b"first criteria")
    env = handoff_env()
    qualified = write_qualification_section(env, valid_qual(["GSE1"]), skill)
    skill.write_bytes(b"changed criteria")
    invocation = deepcopy(env)
    invocation["qualification"] = valid_qual(["GSE1"])
    from tools.sc_dataset.geo_envelope import criteria_hash
    invocation["qualification"]["criteria_profile"]["skill_sha256"] = criteria_hash(skill)
    with pytest.raises(EnvelopeError, match="E_CRITERIA_FINGERPRINT_MISMATCH"):
        apply_resume(qualified, invocation)

def test_duplicate_exclusions_do_not_derive_all_excluded() -> None:
    env = search_env()
    env["search"]["results"]["gse_ids"] = ["GSE1"]
    entry = {"gse_id": "GSE1", "reason_code": "other", "reason_note": "reason", "by": "user", "at_utc": "2026-11-23T17:20:02Z"}
    env["handoff"] = build_handoff(env, [], [entry], now="2026-11-23T17:20:02Z")["handoff"]
    env["handoff"]["excluded"].append(deepcopy(entry))
    env["qualification"] = {"status": "NOT_APPLICABLE"}
    assert derive_pipeline_outcome(env) != "ALL_EXCLUDED"


def test_stage_search_reports_hash_tampering_and_query_fields() -> None:
    env = search_env()
    del env["execution_identity"]["search_scope"]
    assert "E_IDENTITY_TAMPER" in {issue.code for issue in validate_envelope(env, "search")}
    env = search_env()
    env["search"]["per_query"][0]["unmodeled"] = 1
    codes = {issue.code for issue in validate_envelope(env, "search")}
    assert "E_UNKNOWN_FIELD" in codes
    clean = search_env()
    clean["execution_identity"]["search_scope"]["sha256"] = "0" * 64
    assert "E_IDENTITY_TAMPER" in {issue.code for issue in validate_envelope(clean, "search")}


def test_search_failed_plus_not_run_derives_failed() -> None:
    assert derive_search_status(["q1", "q2"], [{"query_id": "q1", "status": "failed", "attempt": 1}, {"query_id": "q2", "status": "not_run", "attempt": 1}]) == "FAILED"

def test_unknown_envelope_field_is_rejected() -> None:
    env = search_env()
    env["undocumented"] = True
    assert "E_UNKNOWN_FIELD" in {i.code for i in validate_envelope(env, "search")}


def test_reconciliation_v2_v3_v5_and_v6_each_fail() -> None:
    env = handoff_env()
    env["handoff"]["transferred_gse_ids"] = ["GSE1", "GSE1"]
    assert "E_HANDOFF_V2" in {i.code for i in validate_envelope(env, "handoff")}
    env = handoff_env()
    env["handoff"]["excluded"] = [{"gse_id": "GSE1", "reason_code": "other", "reason_note": "x", "by": "a", "at_utc": "2026-11-23T17:20:02Z"}]
    env["handoff"]["content_sha256"] = __import__("tools.sc_dataset.geo_envelope", fromlist=["compute_handoff_hash"]).compute_handoff_hash(env)
    assert "E_HANDOFF_V3" in {i.code for i in validate_envelope(env, "handoff")}
    env = handoff_env()
    env["handoff"]["transferred_gse_ids"].append("GSE999")
    env["handoff"]["content_sha256"] = __import__("tools.sc_dataset.geo_envelope", fromlist=["compute_handoff_hash"]).compute_handoff_hash(env)
    assert "E_HANDOFF_V5" in {i.code for i in validate_envelope(env, "handoff")}
    env = search_env()
    env["search"]["results"]["gse_ids"] = ["GSE1"]
    env["handoff"] = {"found_count": 1, "transferred_gse_ids": [], "excluded": [{"gse_id": "GSE1", "reason_code": "other"}], "allowed_to_proceed": "complete", "created_at_utc": "2026-11-23T17:20:02Z", "content_sha256": ""}
    assert "E_HANDOFF_V6" in {i.code for i in validate_envelope(env, "handoff")}


def test_qualification_vq1_vq2_vq3_vq4_and_empty_decision() -> None:
    qual = {"processed_gse_ids": ["GSE1"], "failed_gse_ids": [], "skipped_gse_ids": [], "decisions": {"include": [], "exclude": ["GSE1"], "uncertain": []}, "errors": [], "skipped_reasons": []}
    assert derive_qualification_status(qual, ["GSE1"]) == "COMPLETE"
    missing = deepcopy(qual); missing["processed_gse_ids"] = []; missing["decisions"]["exclude"] = []
    assert "E_QUERY_UNACCOUNTED" in {i.code for i in __import__("tools.sc_dataset.geo_envelope", fromlist=["_validate_qualification"])._validate_qualification(missing, ["GSE1"])}
    overlap = deepcopy(qual); overlap["failed_gse_ids"] = ["GSE1"]; overlap["errors"] = [{"gse_id": "GSE1"}]
    assert "E_UNKNOWN_FIELD" in {i.code for i in __import__("tools.sc_dataset.geo_envelope", fromlist=["_validate_qualification"])._validate_qualification(overlap, ["GSE1"])}
    decision_overlap = deepcopy(qual); decision_overlap["decisions"]["include"] = ["GSE1"]
    assert "E_UNKNOWN_FIELD" in {i.code for i in __import__("tools.sc_dataset.geo_envelope", fromlist=["_validate_qualification"])._validate_qualification(decision_overlap, ["GSE1"])}
    external = deepcopy(qual); external["processed_gse_ids"] = ["GSE9"]; external["decisions"]["exclude"] = ["GSE9"]
    assert "E_QUERY_ID_UNKNOWN" in {i.code for i in __import__("tools.sc_dataset.geo_envelope", fromlist=["_validate_qualification"])._validate_qualification(external, ["GSE1"])}


def test_mutation_api_preserves_lock_order_and_conflicts() -> None:
    env = handoff_env()
    assert env["handoff"]["hash_version"] == "handoff-payload-v1"
    assert env["handoff"]["content_sha256"] == __import__("tools.sc_dataset.geo_envelope", fromlist=["compute_handoff_hash"]).compute_handoff_hash(env)
    partial = search_env()
    partial["search"]["status"] = "PARTIAL"
    partial["search"]["per_query"][0]["status"] = "partial"
    unaccepted = build_handoff(partial, ["GSE1"], [], now="2026-11-23T17:20:02Z")
    assert check_admission(unaccepted)[0] == "BLOCKED_UPSTREAM"
    accepted = build_handoff(partial, ["GSE1"], [], "partial_accepted", {"by": "user", "reason": "ok", "accepted_at_utc": "2026-11-23T17:20:02Z"}, "2026-11-23T17:20:02Z")
    assert check_admission(accepted)[0] == "ADMIT"


def test_unknown_handoff_field_and_enum_are_rejected() -> None:
    env = handoff_env()
    env["handoff"]["unexpected"] = True
    assert "E_UNKNOWN_FIELD" in {i.code for i in validate_envelope(env, "handoff")}
    env = handoff_env()
    env["handoff"]["allowed_to_proceed"] = "anything"
    assert "E_UNKNOWN_ENUM" in {i.code for i in validate_envelope(env, "handoff")}


def test_duplicate_handoff_later_delivery_uses_original_created_time() -> None:
    env = handoff_env()
    again = build_handoff(env, ["GSE1"], [], now="2026-11-24T09:00:00Z")
    assert any(w.get("code") == "W_REPEATED_HANDOFF" for w in again["search"]["warnings"])
    assert again["handoff"]["content_sha256"] == env["handoff"]["content_sha256"]


def test_read_commands_preserve_seal_and_mutation_gate_refuses_before_write(tmp_path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import (
        _mutate_path,
        report,
        write_envelope_atomic,
    )
    env = handoff_env()
    env["sealed_at_utc"] = "2026-11-23T17:40:00Z"
    path = tmp_path / "envelope.json"
    write_envelope_atomic(path, env)
    before = path.read_bytes()
    assert "Run:" in report(json.loads(path.read_bytes()))
    with pytest.raises(EnvelopeError, match="E_ENVELOPE_SEALED"):
        _mutate_path(path, lambda current: current.setdefault("search", {}).update({"status": "FAILED"}))
    assert path.read_bytes() == before


def test_fixture_exists_for_each_plan_error_code() -> None:
    import json
    from pathlib import Path
    fixture_dir = Path(__file__).parent / "fixtures" / "envelopes"
    expected = {
        "E_STATUS_MISSING", "E_QUERY_UNACCOUNTED", "E_UNKNOWN_FIELD", "E_UNKNOWN_ENUM",
        "E_TIMESTAMP_FORMAT", "E_HANDOFF_V1", "E_HANDOFF_V2", "E_HANDOFF_V3",
        "E_HANDOFF_V4", "E_HANDOFF_V5", "E_HANDOFF_V6", "E_HANDOFF_V7",
        "E_HASH_MISMATCH", "E_IDENTITY_TAMPER", "E_HANDOFF_MISSING",
        "E_QUERY_ID_UNKNOWN", "E_ATTEMPT_SEQUENCE",
    }
    found = {json.loads(path.read_text())["expected_error"] for path in fixture_dir.glob("error_*.json")}
    assert found == expected


def test_pipeline_declared_outcome_must_be_valid_and_derived() -> None:
    env = handoff_env()
    env["qualification"] = valid_qual(["GSE1"])
    from tools.sc_dataset.geo_envelope import compute_fingerprint, criteria_payload
    env["execution_identity"]["qualification_criteria"] = {"payload_version": "criteria-fp-v1", "sha256": compute_fingerprint(criteria_payload(env["qualification"]))}
    env["pipeline"] = {"outcome": "IMAGINARY", "outcome_rationale": "bad"}
    codes = {issue.code for issue in validate_envelope(env, "seal")}
    assert "E_UNKNOWN_ENUM" in codes and "E_STATUS_MISSING" in codes


def test_qualification_invariant_uses_declared_error_code_vocabulary() -> None:
    qual = valid_qual(["GSE1"])
    qual["failed_gse_ids"] = ["GSE1"]
    qual["errors"] = [{"gse_id": "GSE1", "detail": "failed"}]
    codes = {issue.code for issue in __import__("tools.sc_dataset.geo_envelope", fromlist=["_validate_qualification"])._validate_qualification(qual, ["GSE1"])}
    assert codes <= {"E_STATUS_MISSING", "E_QUERY_UNACCOUNTED", "E_UNKNOWN_FIELD", "E_UNKNOWN_ENUM", "E_TIMESTAMP_FORMAT", "E_HANDOFF_V1", "E_HANDOFF_V2", "E_HANDOFF_V3", "E_HANDOFF_V4", "E_HANDOFF_V5", "E_HANDOFF_V6", "E_HANDOFF_V7", "E_HASH_MISMATCH", "E_IDENTITY_TAMPER", "E_HANDOFF_MISSING", "E_QUERY_ID_UNKNOWN", "E_ATTEMPT_SEQUENCE", "E_ENVELOPE_CONFLICT", "E_SCOPE_FINGERPRINT_MISMATCH", "E_CRITERIA_FINGERPRINT_MISMATCH", "E_ENVELOPE_SEALED"}


def test_each_error_fixture_reproduces_its_declared_validator_code() -> None:
    import json
    from pathlib import Path
    fixture_dir = Path(__file__).parent / "fixtures" / "envelopes"
    for fixture in fixture_dir.glob("error_*.json"):
        payload = json.loads(fixture.read_text())
        assert "envelope" in payload and "stage" in payload
        codes = {issue.code for issue in validate_envelope(payload["envelope"], payload["stage"])}
        assert payload["expected_error"] in codes, fixture.name


def test_all_not_run_search_records_no_queries_attempted_error(tmp_path) -> None:
    import json

    import tools.sc_dataset.geo_search as module
    from click.testing import CliRunner
    from tools.sc_dataset.geo_search import main
    monkeypatch_result = {"queries": [], "records": [], "dropped_no_gse": 0}
    from unittest.mock import patch
    with patch.object(module, "search_geo", return_value=monkeypatch_result):
        output = CliRunner().invoke(main, ["--query", " ", "--output-envelope", str(tmp_path / "not-used.json"), "--run-id", "20261123T170500Z-3f9a1c2e"])
    assert output.exit_code == 0
    envelope = json.loads((tmp_path / "not-used.json").read_text())
    assert {error.get("code") for error in envelope["search"]["errors"]} == {"no_queries_attempted"}


def test_valid_stage_fixtures_pass_their_named_validation_stage() -> None:
    import json
    from pathlib import Path
    fixture_dir = Path(__file__).parent / "fixtures" / "envelopes"
    for name, stage in (("valid_search.json", "search"), ("valid_handoff.json", "handoff"), ("valid_qualification.json", "qualification"), ("valid_seal.json", "seal")):
        env = json.loads((fixture_dir / name).read_text())
        assert not [issue for issue in validate_envelope(env, stage) if issue.severity == "ERROR"], name


def test_failed_and_partial_without_success_derives_failed() -> None:
    assert derive_search_status(["q1", "q2"], [{"query_id": "q1", "status": "failed", "attempt": 1}, {"query_id": "q2", "status": "partial", "attempt": 1}]) == "FAILED"


def test_crash_mid_write_leaves_previous_envelope_valid(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    from tools.sc_dataset.geo_envelope import write_envelope_atomic

    envelope_path = tmp_path / "envelope.json"
    prior = handoff_env()
    write_envelope_atomic(envelope_path, prior)
    ready_path = tmp_path / "tmp_written"
    child_script = 'import json, sys, time\nfrom pathlib import Path\nimport tools.sc_dataset.geo_envelope as module\ndef pause_before_replace(source, destination):\n    Path(sys.argv[2]).write_text("ready")\n    time.sleep(30)\nmodule.os.replace = pause_before_replace\npath = Path(sys.argv[1])\nenv = json.loads(path.read_text())\nenv["search"]["warnings"].append({"crash": True})\nmodule.write_envelope_atomic(path, env)'
    child = subprocess.Popen([sys.executable, "-c", child_script, str(envelope_path), str(ready_path)], env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / ".agents")})
    try:
        deadline = time.monotonic() + 3
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists()
        child.kill()
        child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
    assert json.loads(envelope_path.read_bytes()) == prior
    assert list(tmp_path.glob("envelope.json.tmp.*"))


def test_post_seal_refusal_api_rejects_before_serialization() -> None:
    from tools.sc_dataset.geo_envelope import refuse
    env = handoff_env()
    env["sealed_at_utc"] = "2026-11-23T17:40:00Z"
    before = __import__("tools.sc_dataset.geo_envelope", fromlist=["canonical_json_bytes"]).canonical_json_bytes(env)
    with pytest.raises(EnvelopeError, match="E_ENVELOPE_SEALED"):
        refuse(env, "qualification", "BLOCKED_UPSTREAM", "blocked")
    assert __import__("tools.sc_dataset.geo_envelope", fromlist=["canonical_json_bytes"]).canonical_json_bytes(env) == before


def test_hash_immune_to_later_stage_sections() -> None:
    from tools.sc_dataset.geo_envelope import compute_fingerprint, criteria_payload
    env = handoff_env()
    original = env["handoff"]["content_sha256"]
    env["qualification"] = valid_qual(["GSE1"])
    env["execution_identity"]["qualification_criteria"] = {"payload_version": "criteria-fp-v1", "sha256": compute_fingerprint(criteria_payload(env["qualification"]))}
    env["pipeline"] = {"outcome": "SUCCESS", "outcome_rationale": "ok"}
    assert env["handoff"]["content_sha256"] == original
    assert "E_HASH_MISMATCH" not in {i.code for i in validate_envelope(env, "seal")}


def test_admission_blocked_policy() -> None:
    from tools.sc_dataset.geo_envelope import check_admission, compute_handoff_hash
    env = handoff_env()
    env["handoff"]["allowed_to_proceed"] = "blocked"
    env["handoff"]["content_sha256"] = compute_handoff_hash(env)
    decision, _ = check_admission(env)
    assert decision == "BLOCKED_UPSTREAM"


def test_all_partial_derives_partial() -> None:
    rows = [{"query_id": "q1", "status": "partial", "attempt": 1}, {"query_id": "q2", "status": "partial", "attempt": 1}]
    assert derive_search_status(["q1", "q2"], rows) == "PARTIAL"


def test_search_status_rejects_blocked_values() -> None:
    env = search_env()
    env["search"]["status"] = "BLOCKED_UPSTREAM"
    assert "E_UNKNOWN_ENUM" in {i.code for i in validate_envelope(env, "search")}


def test_reseal_identical_is_noop() -> None:
    from tools.sc_dataset.geo_envelope import compute_fingerprint, criteria_payload
    env = handoff_env()
    env["qualification"] = valid_qual(["GSE1"])
    env["execution_identity"]["qualification_criteria"] = {"payload_version": "criteria-fp-v1", "sha256": compute_fingerprint(criteria_payload(env["qualification"]))}
    sealed = seal(env, now="2026-11-23T17:40:00Z")
    again = seal(sealed, now="2026-11-23T17:41:00Z")
    assert again["sealed_at_utc"] == "2026-11-23T17:40:00Z"
    assert any(w.get("code") == "W_ALREADY_SEALED" for w in again["search"]["warnings"])


def test_admit_cli_reports_blocked(tmp_path) -> None:
    import json

    from click.testing import CliRunner
    from tools.sc_dataset.geo_envelope import cli, write_envelope_atomic
    env = search_env()
    path = tmp_path / "envelope.json"
    write_envelope_atomic(path, env)
    result = CliRunner().invoke(cli, ["admit", str(path)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["decision"] == "BLOCKED_INVALID_ENVELOPE"
    assert any(i["code"] == "E_HANDOFF_MISSING" for i in payload["issues"])


def _seal_ready_env(report_path: Path | None = None) -> dict[str, Any]:
    from tools.sc_dataset.geo_envelope import compute_fingerprint, criteria_payload

    env = handoff_env()
    env["qualification"] = valid_qual(["GSE1"])
    env["execution_identity"]["qualification_criteria"] = {
        "payload_version": "criteria-fp-v1",
        "sha256": compute_fingerprint(criteria_payload(env["qualification"])),
    }
    if report_path is not None:
        env["qualification"]["persistence"] = {"media": "duckdb", "report": str(report_path)}
    return env


def test_persistence_report_is_required_only_at_seal_stage() -> None:
    env = _seal_ready_env()
    env["qualification"]["persistence"] = {"media": "duckdb"}
    assert "E_PERSISTENCE_REPORT_MISSING" not in {issue.code for issue in validate_envelope(env, "qualification")}
    assert "E_PERSISTENCE_REPORT_MISSING" in {issue.code for issue in validate_envelope(env, "seal")}
    env["qualification"]["persistence"] = {"media": "duckdb", "report": []}
    assert "E_UNKNOWN_FIELD" in {issue.code for issue in validate_envelope(env, "seal")}
    env["qualification"]["persistence"] = []
    assert "E_UNKNOWN_FIELD" in {issue.code for issue in validate_envelope(env, "seal")}


def test_write_qualification_section_rejects_search_only_envelope(tmp_path: Path) -> None:
    from tools.sc_dataset.geo_envelope import write_qualification_section

    skill = tmp_path / "skill.md"
    skill.write_text("criteria")
    with pytest.raises(EnvelopeError, match="E_HANDOFF_MISSING"):
        write_qualification_section(search_env(), valid_qual(["GSE1"]), skill)


def test_registered_precondition_blocks_missing_event_at_seal(tmp_path: Path) -> None:
    from tools.sc_dataset.geo_envelope import seal

    env = _seal_ready_env(tmp_path / "metadata_fetch.jsonl")
    with pytest.raises(EnvelopeError, match="E_REGISTERED_MISSING"):
        seal(env)


def test_registered_event_allows_seal_and_checks_run_and_expected_ids(tmp_path: Path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import seal

    report = tmp_path / "metadata_fetch.jsonl"
    env = _seal_ready_env(report)
    report.write_text(json.dumps({
        "gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"],
        "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True},
    }) + "\n")
    assert seal(env, now="2026-11-23T17:40:00Z")["sealed_at_utc"] == "2026-11-23T17:40:00Z"

    for event in (
        {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": "different", "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True}},
        {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"], "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1", "GSM2"], "samples": [{"gsm_id": "GSM2"}], "sample_count": 1, "verified": True}},
    ):
        report.write_text(json.dumps(event) + "\n")
        with pytest.raises(EnvelopeError, match="E_REGISTERED_MISMATCH"):
            seal(env)


def test_registered_event_with_invalid_sample_payload_is_rejected(tmp_path: Path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import seal

    report = tmp_path / "metadata_fetch.jsonl"
    env = _seal_ready_env(report)
    event = {
        "gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"],
        "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1", 7], "samples": [{"gsm_id": "GSM1"}], "sample_count": 2, "verified": True},
    }
    report.write_text(json.dumps(event) + "\n")
    with pytest.raises(EnvelopeError, match="E_REGISTERED_MISMATCH"):
        seal(env)


def test_registered_event_reader_tolerates_partial_last_line(tmp_path: Path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import seal

    report = tmp_path / "metadata_fetch.jsonl"
    env = _seal_ready_env(report)
    event = {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"], "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True}}
    report.write_text(json.dumps(event) + "\n{" )
    assert seal(env, now="2026-11-23T17:40:00Z")["sealed_at_utc"] == "2026-11-23T17:40:00Z"


def test_registered_event_log_rejects_duplicate_sequences(tmp_path: Path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import seal

    report = tmp_path / "metadata_fetch.jsonl"
    env = _seal_ready_env(report)
    event = {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"], "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True}}
    duplicate = {**event}
    report.write_text(json.dumps(event) + "\n" + json.dumps(duplicate) + "\n")
    with pytest.raises(EnvelopeError, match="E_REGISTERED_MISSING"):
        seal(env)


def test_failed_between_two_registration_events_still_blocks_seal(tmp_path: Path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import seal

    report = tmp_path / "metadata_fetch.jsonl"
    env = _seal_ready_env(report)
    valid_registration = {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True}
    events = [
        {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"], "registration": valid_registration},
        {"gse_id": "GSE1", "seq": 2, "status": "failed", "run_id": env["run_id"]},
        {"gse_id": "GSE1", "seq": 3, "status": "registered", "run_id": env["run_id"], "registration": valid_registration},
    ]
    report.write_text("".join(json.dumps(event) + "\n" for event in events))
    with pytest.raises(EnvelopeError, match="E_REGISTERED_ORDER"):
        seal(env)


def test_latest_nonregistered_event_blocks_seal(tmp_path: Path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import seal

    report = tmp_path / "metadata_fetch.jsonl"
    env = _seal_ready_env(report)
    events = [
        {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"], "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True}},
        {"gse_id": "GSE1", "seq": 2, "status": "ok", "run_id": env["run_id"]},
    ]
    report.write_text("".join(json.dumps(event) + "\n" for event in events))
    with pytest.raises(EnvelopeError, match="E_REGISTERED_MISMATCH"):
        seal(env)


def test_empty_handoff_with_persistence_needs_no_registration_event(tmp_path: Path) -> None:
    from tools.sc_dataset.geo_envelope import build_handoff, seal

    env = search_env()
    env["search"]["results"]["gse_ids"] = []
    env["search"]["results"]["unique_gse_count"] = 0
    env = build_handoff(env, [], [], now="2026-11-23T17:20:02Z")
    env["qualification"] = {"status": "NOT_APPLICABLE", "persistence": {"media": "duckdb", "report": str(tmp_path / "no-events.jsonl")}}
    assert seal(env, now="2026-11-23T17:40:00Z")["pipeline"]["outcome"] == "NO_CANDIDATES"


def test_no_persistence_seals_without_registration_log() -> None:
    from tools.sc_dataset.geo_envelope import seal

    assert seal(_seal_ready_env(), now="2026-11-23T17:40:00Z")["pipeline"]["outcome"] == "SUCCESS"


def test_failed_event_after_registered_blocks_seal(tmp_path: Path) -> None:
    import json

    from tools.sc_dataset.geo_envelope import seal

    report = tmp_path / "metadata_fetch.jsonl"
    env = _seal_ready_env(report)
    events = [
        {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"], "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True}},
        {"gse_id": "GSE1", "seq": 2, "status": "failed", "run_id": env["run_id"], "payload": {}},
    ]
    report.write_text("".join(json.dumps(event) + "\n" for event in events))
    with pytest.raises(EnvelopeError, match="E_REGISTERED_ORDER"):
        seal(env)


def test_inconsistent_search_status_is_rejected_at_admission() -> None:
    env = handoff_env()
    env["search"]["per_query"][0]["status"] = "partial"
    codes = {issue.code for issue in validate_envelope(env, "handoff")}
    assert "E_SEARCH_STATUS_INCONSISTENT" in codes
    assert check_admission(env)[0] == "BLOCKED_INVALID_ENVELOPE"
    consistent = handoff_env()
    assert check_admission(consistent)[0] == "ADMIT"


@pytest.mark.parametrize("bad_row", ["broken", {}, {"query_id": "q1"}])
def test_check_admission_malformed_per_query_fails_closed(bad_row):
    env = search_env()
    env["search"]["per_query"] = [bad_row]
    env["search"]["status"] = "PARTIAL"

    decision, issues = check_admission(env)

    assert decision == "BLOCKED_INVALID_ENVELOPE"
    assert issues


def test_seal_reports_one_canonical_status_error_for_inconsistent_search():
    from tools.sc_dataset.geo_envelope import seal

    env = handoff_env()
    env["search"]["per_query"][0]["status"] = "failed"
    env["search"]["status"] = "COMPLETE"

    with pytest.raises(EnvelopeError) as caught:
        seal(env)

    status_issues = [issue for issue in caught.value.issues if issue.path == "search.status"]
    assert len(status_issues) == 1
    assert status_issues[0].code == "E_SEARCH_STATUS_INCONSISTENT"



def test_seal_resolves_relative_persistence_report_from_envelope_directory(tmp_path):
    import json

    from click.testing import CliRunner
    from tools.sc_dataset.geo_envelope import cli, write_envelope_atomic

    envelope_dir = tmp_path / "run"
    envelope_dir.mkdir()
    report = envelope_dir / "metadata_fetch.jsonl"
    env = _seal_ready_env(Path("metadata_fetch.jsonl"))
    event = {"gse_id": "GSE1", "seq": 1, "status": "registered", "run_id": env["run_id"], "registration": {"schema": "registration-v1", "gse_id": "GSE1", "expected_gsm_ids": ["GSM1"], "samples": [{"gsm_id": "GSM1"}], "sample_count": 1, "verified": True}}
    report.write_text(json.dumps(event) + "\n")
    path = envelope_dir / "envelope.json"
    write_envelope_atomic(path, env)
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    previous = Path.cwd()
    os.chdir(other_cwd)
    try:
        validated = CliRunner().invoke(cli, ["validate", str(path)])
        result = CliRunner().invoke(cli, ["seal", str(path)])
    finally:
        os.chdir(previous)

    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output) == []
    assert result.exit_code == 0, result.output


def test_validate_seal_derive_report_json_on_non_dict_qualification(tmp_path) -> None:
    # ETAP-6 review P2-1 regression: malformed section types must produce the
    # machine-readable issue JSON, never an AttributeError traceback.
    import json as _json
    import subprocess
    import sys
    from pathlib import Path as _P
    tool = _P(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset" / "geo_envelope.py"
    bad = tmp_path / "bad.json"
    for bad_value in ("oops", None, 7, ["x"]):
        bad.write_text(_json.dumps({
            "schema_version": 1, "run_id": "run_x",
            "created_at_utc": "2026-11-24T00:00:00Z",
            "search": {"status": "COMPLETE", "results": [], "params": {}, "warnings": []},
            "qualification": bad_value,
        }), encoding="utf-8")
        for argv in (["validate", str(bad), "--stage", "seal"], ["seal", str(bad)], ["derive", str(bad)], ["report", str(bad)]):
            result = subprocess.run([sys.executable, str(tool), *argv], capture_output=True, text=True, check=False)
            assert result.returncode in (0, 1), (argv, bad_value, result.stderr)
            assert "Traceback" not in result.stderr, (argv, bad_value, result.stderr)
            if argv[0] in {"validate", "seal"}:
                assert result.returncode == 1 and result.stdout.strip(), (argv, bad_value, result.stdout, result.stderr)
                payload = _json.loads(result.stdout)
                issues = payload["issues"] if isinstance(payload, dict) else payload
                assert isinstance(issues, list) and issues
                assert any(isinstance(i, dict) and i.get("code") in {"E_UNKNOWN_FIELD", "E_STATUS_MISSING", "E_UNKNOWN_ENUM"} or (isinstance(i, dict) and "qualification" in str(i.get("path", i.get("field", "")))) for i in issues), payload



# ---- Mini-P2 stage (2026-11-24): non-dict handoff, refuse --stage, qualification.errors ----

_MP2_TOOL = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset" / "geo_envelope.py"
_MP2_FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "envelopes"


def _mp2_cli(argv: list[str]):
    import subprocess
    import sys
    return subprocess.run([sys.executable, str(_MP2_TOOL), *argv], capture_output=True, text=True, check=False)


def _mp2_doc(name: str) -> dict:
    import json
    return json.loads((_MP2_FIX / name).read_text(encoding="utf-8"))


def _mp2_write(tmp_path: Path, name: str, doc: dict) -> Path:
    import json
    path = tmp_path / name
    path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    return path


@pytest.mark.parametrize("bad", [None, "handoff-string", [1], 7], ids=["none", "str", "list", "int"])
def test_p21_non_dict_handoff_yields_controlled_refusal(tmp_path, bad) -> None:
    import json
    doc = _mp2_doc("valid_seal.json")
    doc["handoff"] = bad
    path = _mp2_write(tmp_path, f"bad_{type(bad).__name__}.json", doc)
    result = _mp2_cli(["validate", str(path), "--stage", "seal"])
    assert "Traceback" not in result.stderr, result.stderr[-400:]
    assert result.returncode == 1, (result.returncode, result.stdout[-200:], result.stderr[-400:])
    payload = json.loads(result.stdout)
    issues = payload["issues"] if isinstance(payload, dict) else payload
    assert any(i["code"] in {"E_HANDOFF_MISSING", "E_UNKNOWN_FIELD"} and "handoff" in str(i["path"]) for i in issues), issues
    derived = _mp2_cli(["derive", str(path)])
    assert "Traceback" not in derived.stderr, derived.stderr[-400:]
    reported = _mp2_cli(["report", str(path)])
    assert "Traceback" not in reported.stderr, reported.stderr[-400:]


def test_p21_incomplete_handoff_dict_and_valid_fixture_unchanged(tmp_path) -> None:
    import json
    doc = _mp2_doc("valid_seal.json")
    del doc["handoff"]["allowed_to_proceed"]
    del doc["handoff"]["transferred_gse_ids"]
    path = _mp2_write(tmp_path, "partial_handoff.json", doc)
    result = _mp2_cli(["validate", str(path), "--stage", "seal"])
    assert "Traceback" not in result.stderr, result.stderr[-400:]
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    issues = payload["issues"] if isinstance(payload, dict) else payload
    assert any("handoff" in str(i["path"]) for i in issues), issues
    valid_path = tmp_path / "valid_copy.json"
    valid_path.write_bytes((_MP2_FIX / "valid_seal.json").read_bytes())
    valid = _mp2_cli(["validate", str(valid_path), "--stage", "seal"])
    assert valid.returncode == 0, (valid.stdout, valid.stderr)


def test_p22_refuse_stage_choice_blocks_bad_stages_preserving_bytes(tmp_path) -> None:
    source = _MP2_FIX / "valid_handoff.json"
    for stage in ("search", "handoff", "seal", "bogus"):
        path = tmp_path / f"refuse_{stage}.json"
        path.write_bytes(source.read_bytes())
        before = path.read_bytes()
        result = _mp2_cli(["refuse", str(path), "--stage", stage, "--status", "BLOCKED_UPSTREAM", "--reason", "x"])
        assert result.returncode != 0, (stage, result.stdout, result.stderr)
        assert path.read_bytes() == before, stage
    missing = _mp2_cli(["refuse", str(_MP2_FIX / "valid_handoff.json"), "--status", "BLOCKED_UPSTREAM", "--reason", "x"])
    assert missing.returncode != 0
    ghost = tmp_path / "refuse_ok.json"
    ok = _mp2_cli(["refuse", str(ghost), "--stage", "qualification", "--status", "BLOCKED_UPSTREAM", "--reason", "x"])
    assert ok.returncode != 0 and not ghost.exists()  # missing file: refused, nothing created


def test_p22_refuse_qualification_still_works(tmp_path) -> None:
    import json
    path = tmp_path / "refuse_good.json"
    path.write_bytes((_MP2_FIX / "valid_handoff.json").read_bytes())
    result = _mp2_cli(["refuse", str(path), "--stage", "qualification", "--status", "BLOCKED_UPSTREAM", "--reason", "upstream"])
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["status"] == "BLOCKED_UPSTREAM"
    assert json.loads(path.read_text())["qualification"]["status"] == "BLOCKED_UPSTREAM"


@pytest.mark.parametrize("bad_errors", [["boom"], [{}], [{"gse_id": "GSE1"}], {"a": 1}, [42], None, 7, True],
                       ids=["str-row", "empty-dict", "no-detail", "dict-not-list", "int-row", "none", "int", "bool"])
def test_p23_qualification_errors_rows_rejected(tmp_path, bad_errors) -> None:
    import json
    doc = _mp2_doc("valid_seal.json")
    doc["qualification"]["errors"] = bad_errors
    path = _mp2_write(tmp_path, f"qerr_{abs(hash(str(bad_errors)))}.json", doc)
    result = _mp2_cli(["validate", str(path), "--stage", "qualification"])
    assert "Traceback" not in result.stderr, result.stderr[-400:]
    payload = json.loads(result.stdout)
    issues = payload["issues"] if isinstance(payload, dict) else payload
    assert any("qualification.errors" in str(i["path"]) for i in issues), (bad_errors, issues)
    assert result.returncode == 1


def test_p23_valid_error_rows_and_write_qualification_gate(tmp_path) -> None:
    doc = _mp2_doc("valid_seal.json")
    doc["qualification"]["errors"] = [{"gse_id": "GSE1", "code": "http_error", "detail": "503"}]
    doc["qualification"]["failed_gse_ids"] = []
    path = _mp2_write(tmp_path, "qerr_ok.json", doc)
    result = _mp2_cli(["validate", str(path), "--stage", "qualification"])
    assert result.returncode == 0, result.stdout
    # write-qualification must also refuse hostile error rows (pre-admission gate)
    qual = _mp2_doc("valid_seal.json")["qualification"]
    qual["errors"] = ["boom"]
    qpath = _mp2_write(tmp_path, "qual_bad.json", qual)
    env_path = tmp_path / "wh.json"
    env_path.write_bytes((_MP2_FIX / "valid_handoff.json").read_bytes())
    skill = tmp_path / "skill.md"
    skill.write_bytes(b"criteria")
    write = _mp2_cli(["write-qualification", str(env_path), str(qpath), "--skill-md", str(skill)])
    assert write.returncode != 0, write.stdout
    assert "Traceback" not in write.stderr, write.stderr[-400:]


@pytest.mark.parametrize("bad_skips", [None, 7, "x"], ids=["none", "int", "str"])
def test_p23_r1_skipped_reasons_scalars_rejected_without_traceback(tmp_path, bad_skips) -> None:
    import json
    doc = _mp2_doc("valid_seal.json")
    doc["qualification"]["skipped_reasons"] = bad_skips
    path = _mp2_write(tmp_path, f"serr_{type(bad_skips).__name__}.json", doc)
    result = _mp2_cli(["validate", str(path), "--stage", "seal"])
    assert "Traceback" not in result.stderr, result.stderr[-400:]
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    issues = payload["issues"] if isinstance(payload, dict) else payload
    assert any("skipped_reasons" in str(i["path"]) for i in issues), issues
