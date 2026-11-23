from __future__ import annotations

from copy import deepcopy

import pytest
from tools.sc_dataset.geo_envelope import (
    EnvelopeError,
    apply_resume,
    build_handoff,
    check_admission,
    derive_pipeline_outcome,
    derive_search_status,
    envelope_skeleton,
    seal,
    validate_envelope,
    write_envelope_atomic,
    write_qualification_section,
)

RUN_ID = "20261123T170500Z-3f9a1c2e"
NOW = "2026-11-23T17:05:00Z"

def make_search(ids: list[str] | None = None, state: str = "success") -> dict:
    env = envelope_skeleton(RUN_ID, NOW, {"name": "sc-dataset-search", "version": "0.5.0"}, "geo_search.py", "1.1.0", {"queries": [{"query_id": "q1", "query": "alpha", "max_results": 10}], "species_constraint": "human"}, {"retstart_pagination": False})
    ids = ids or []
    env["search"].update({"status": {"success": "COMPLETE", "partial": "PARTIAL", "failed": "FAILED", "not_run": "FAILED"}[state], "per_query": [{"query_id": "q1", "status": state, "count": len(ids), "retrieved": len(ids), "error": None if state == "success" else "incomplete", "attempt": 1}], "results": {"unique_gse_count": len(ids), "gse_ids": ids, "dropped_no_gse": 0}, "finished_at_utc": "2026-11-23T17:12:41Z"})
    return env

def qualification(ids: list[str], status: str = "COMPLETE", includes: list[str] | None = None) -> dict:
    return {"skill": {"name": "sc-dataset-qualification", "version": "0.3.0"}, "criteria_profile": {"name": "default", "skill_version": "0.3.0", "skill_sha256": "unused"}, "status": status, "processed_gse_ids": ids, "failed_gse_ids": [], "skipped_gse_ids": [], "decisions": {"include": includes if includes is not None else ids, "exclude": [], "uncertain": []}, "sample_counts": {"include": len(includes or ids), "exclude": 0, "uncertain": 0}, "errors": [], "skipped_reasons": [], "finished_at_utc": "2026-11-23T17:30:00Z"}

def complete(env: dict, ids: list[str], tmp_path, status: str = "COMPLETE", includes: list[str] | None = None) -> dict:
    excluded = []
    env = build_handoff(env, ids, excluded, now="2026-11-23T17:20:02Z")
    skill = tmp_path / "skill.md"
    skill.write_bytes(b"qualification criteria v1")
    qual = qualification(ids, status, includes)
    env = write_qualification_section(env, qual, skill, now="2026-11-23T17:30:00Z")
    return env

def test_s1_happy_path_success(tmp_path) -> None:
    env = complete(make_search(["GSE1"]), ["GSE1"], tmp_path)
    assert derive_pipeline_outcome(env) == "SUCCESS"
    assert seal(env, NOW)["pipeline"]["outcome"] == "SUCCESS"

def test_s2_zero_candidates(tmp_path) -> None:
    env = complete(make_search(), [], tmp_path, "NOT_APPLICABLE")
    assert derive_pipeline_outcome(env) == "NO_CANDIDATES"

def test_s3_partial_accepted(tmp_path) -> None:
    env = make_search(["GSE1"], "partial")
    env = build_handoff(env, ["GSE1"], [], "partial_accepted", {"by": "user", "reason": "accept", "accepted_at_utc": NOW}, NOW)
    skill = tmp_path / "skill.md"
    skill.write_bytes(b"criteria")
    env = write_qualification_section(env, qualification(["GSE1"]), skill, NOW)
    assert derive_pipeline_outcome(env) == "PARTIAL_SUCCESS"

def test_s4_all_queries_failed() -> None:
    env = make_search([], "failed")
    assert env["search"]["status"] == "FAILED"
    assert derive_search_status(["q1"], env["search"]["per_query"]) == "FAILED"

def test_s5_invalid_envelope_blocks() -> None:
    status, issues = check_admission({"search": {}})
    assert status == "BLOCKED_INVALID_ENVELOPE" and issues

def test_s6_duplicate_handoff_is_idempotent() -> None:
    env = make_search(["GSE1"])
    first = build_handoff(env, ["GSE1"], [], now=NOW)
    again = build_handoff(first, ["GSE1"], [], now=NOW)
    assert any(w.get("code") == "W_REPEATED_HANDOFF" for w in again["search"]["warnings"])

def test_s7_partial_qualification_outcome(tmp_path) -> None:
    env = complete(make_search(["GSE1"]), ["GSE1"], tmp_path, "PARTIAL")
    env["qualification"]["status"] = "PARTIAL"
    assert derive_pipeline_outcome(env) == "PARTIAL_SUCCESS"

def test_s8_conflicting_handoff_rejected() -> None:
    env = build_handoff(make_search(["GSE1"]), ["GSE1"], [], now=NOW)
    with pytest.raises(EnvelopeError, match="E_ENVELOPE_CONFLICT"):
        build_handoff(env, [], [{"gse_id": "GSE1", "reason_code": "other", "reason_note": "x", "by": "u", "at_utc": NOW}], now=NOW)

def test_s9_no_qualification_included_is_empty_complete(tmp_path) -> None:
    env = complete(make_search(["GSE1"]), ["GSE1"], tmp_path, includes=[])
    assert derive_pipeline_outcome(env) == "EMPTY_COMPLETE"

def test_s10_all_found_excluded(tmp_path) -> None:
    env = make_search(["GSE1"])
    env = build_handoff(env, [], [{"gse_id": "GSE1", "reason_code": "other", "reason_note": "skip", "by": "user", "at_utc": NOW}], now=NOW)
    env["qualification"] = {"status": "NOT_APPLICABLE"}
    assert derive_pipeline_outcome(env) == "ALL_EXCLUDED"

def test_s11_unaccounted_found_is_invalid() -> None:
    env = make_search(["GSE1", "GSE2"])
    bad = {**env, "handoff": {"found_count": 2, "transferred_gse_ids": ["GSE1"], "excluded": [], "allowed_to_proceed": "complete", "created_at_utc": NOW, "content_sha256": "bad"}}
    assert {i.code for i in validate_envelope(bad, "handoff")} & {"E_HANDOFF_V4", "E_HANDOFF_V7"}

def test_s12_no_query_attempted_is_failed() -> None:
    env = make_search([], "not_run")
    assert derive_search_status(["q1"], env["search"]["per_query"]) == "FAILED"

def test_s13_scope_drift_refused() -> None:
    env = make_search(["GSE1"])
    changed = deepcopy(env)
    changed["search"]["params"]["queries"][0]["query"] = "other"
    with pytest.raises(EnvelopeError, match="E_SCOPE_FINGERPRINT_MISMATCH"):
        apply_resume(env, changed)

def test_s14_criteria_drift_refused(tmp_path) -> None:
    env = complete(make_search(["GSE1"]), ["GSE1"], tmp_path)
    skill = tmp_path / "skill.md"
    skill.write_bytes(b"changed criteria")
    with pytest.raises(EnvelopeError, match="E_CRITERIA_FINGERPRINT_MISMATCH"):
        write_qualification_section(env, qualification(["GSE1"]), skill, NOW)

def test_s15_post_seal_mutation_refused_bytes_unchanged(tmp_path) -> None:
    from tools.sc_dataset.geo_envelope import _mutate_path
    env = complete(make_search(["GSE1"]), ["GSE1"], tmp_path)
    sealed = seal(env, NOW)
    path = tmp_path / "envelope.json"
    write_envelope_atomic(path, sealed)
    before = path.read_bytes()
    with pytest.raises(EnvelopeError, match="E_ENVELOPE_SEALED"):
        _mutate_path(path, lambda current: current.update({"x": 1}))
    assert path.read_bytes() == before

def test_s16_concurrent_writers_serialize(tmp_path) -> None:
    import subprocess
    import sys
    path = tmp_path / "envelope.json"
    write_envelope_atomic(path, make_search(["GSE1"]))
    worker = 'from tools.sc_dataset.geo_envelope import _mutate_path; import sys; p=sys.argv[1]; exec("def update(e):\\n e[\\"search\\"][\\"warnings\\"].append({\\"pid\\": 1}); return e"); [ _mutate_path(p, update) for _ in range(5) ]'
    import os
    from pathlib import Path
    procs = [subprocess.Popen([sys.executable, "-c", worker, str(path)], env={**os.environ, "PYTHONPATH": str(Path.cwd() / ".agents")}) for _ in range(2)]
    assert [proc.wait(timeout=10) for proc in procs] == [0, 0]
    import json
    final = json.loads(path.read_text())
    assert len(final["search"]["warnings"]) == 10


def test_skipped_previously_recorded_is_unknown_qualification_enum(tmp_path):
    from tools.sc_dataset.geo_envelope import QUAL_STATES

    assert "skipped_previously_recorded" not in QUAL_STATES
    env = make_search(["GSE1"])
    env = build_handoff(env, ["GSE1"], [], now=NOW)
    env["qualification"] = qualification(["GSE1"])
    env["qualification"]["status"] = "skipped_previously_recorded"
    issues = validate_envelope(env, "qualification")
    assert any(issue.code == "E_UNKNOWN_ENUM" and issue.path == "qualification.status" for issue in issues)


def test_error_detail_convention_is_a_seal_stage_string(tmp_path):
    from tools.sc_dataset.geo_envelope import seal

    env = complete(make_search(["GSE1"]), ["GSE1"], tmp_path)
    qual = env["qualification"]
    qual["processed_gse_ids"] = []
    qual["failed_gse_ids"] = ["GSE1"]
    qual["decisions"] = {"include": [], "exclude": [], "uncertain": []}
    qual["sample_counts"] = {"include": 0, "exclude": 0, "uncertain": 0}
    qual["errors"] = [{"gse_id": "GSE1", "detail": "error_class=bad_response; attempts=4; last=EOFError; report_seq=2"}]
    qual["status"] = "FAILED"
    assert not [issue for issue in validate_envelope(env, "seal") if issue.severity == "ERROR"]
    assert seal(env, NOW)["sealed_at_utc"] == NOW

    negative = deepcopy(env)
    negative["qualification"]["errors"] = []
    assert "E_QUERY_UNACCOUNTED" in {issue.code for issue in validate_envelope(negative, "seal")}
