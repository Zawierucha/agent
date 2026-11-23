from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

ENVELOPE_VERSION = "1.0"
HASH_VERSION = "handoff-payload-v1"
IDENTITY_VERSION = "exec-identity-v1"
SCOPE_FP_VERSION = "scope-fp-v1"
CRITERIA_FP_VERSION = "criteria-fp-v1"
QUERY_STATES = {"success", "partial", "failed", "not_run"}
SEARCH_STATES = {"COMPLETE", "PARTIAL", "FAILED"}
QUAL_STATES = {"COMPLETE", "PARTIAL", "NOT_APPLICABLE", "FAILED", "BLOCKED_INVALID_ENVELOPE", "BLOCKED_UPSTREAM"}
OUTCOME_STATES = {"SUCCESS", "EMPTY_COMPLETE", "NO_CANDIDATES", "ALL_EXCLUDED", "PARTIAL_SUCCESS", "FAILURE"}
EXCLUSION_REASONS = {"already_registered", "out_of_scope", "duplicate_superseries", "user_directive", "other"}
STAGES = ("search", "handoff", "qualification", "seal")


def canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def hash_payload(env: dict[str, Any]) -> dict[str, Any]:
    search = env["search"]
    handoff = env["handoff"]
    return {
        "hash_version": HASH_VERSION,
        "run_id": env["run_id"],
        "envelope_version": env["envelope_version"],
        "search_provenance": {
            "skill_version": search["skill"]["version"],
            "tool_version": search["tool_version"],
            "finished_at_utc": search["finished_at_utc"],
        },
        "found": sorted(search["results"]["gse_ids"]),
        "transferred": sorted(handoff["transferred_gse_ids"]),
        "excluded": sorted(
            [{"gse_id": entry["gse_id"], "reason_code": entry["reason_code"]} for entry in handoff["excluded"]],
            key=lambda entry: entry["gse_id"],
        ),
        "allowed_to_proceed": handoff["allowed_to_proceed"],
        "handoff_created_at_utc": handoff["created_at_utc"],
    }


def compute_handoff_hash(env: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(hash_payload(env))).hexdigest()


def scope_payload(search: dict[str, Any]) -> dict[str, Any]:
    return {
        "payload_version": SCOPE_FP_VERSION,
        "tool": search["tool"],
        "species_constraint": search["params"]["species_constraint"],
        "planned_queries": sorted(
            [{key: query[key] for key in ("query_id", "query", "max_results")} for query in search["params"]["queries"]],
            key=lambda query: query["query_id"],
        ),
        "retstart_pagination": search["limits"]["retstart_pagination"],
    }


def criteria_payload(qual: dict[str, Any]) -> dict[str, Any]:
    profile = qual["criteria_profile"]
    return {
        "payload_version": CRITERIA_FP_VERSION,
        "skill_name": qual["skill"]["name"],
        "skill_version": qual["skill"]["version"],
        "profile_name": profile["name"],
        "skill_md_sha256": profile["skill_sha256"],
    }


def compute_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_run_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def make_run_dir(workspace: str | Path, run_id: str) -> Path:
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", run_id):
        raise ValueError("invalid run_id")
    path = Path(workspace) / "data" / "reports" / f"run_{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def envelope_skeleton(run_id: str, created_at: str, skill: dict[str, str], tool: str, tool_version: str, params: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    search = {"skill": dict(skill), "tool": tool, "tool_version": tool_version, "status": None, "params": params, "per_query": [], "results": {"unique_gse_count": 0, "gse_ids": [], "dropped_no_gse": 0}, "limits": limits, "errors": [], "warnings": []}
    env: dict[str, Any] = {"envelope_version": ENVELOPE_VERSION, "run_id": run_id, "created_at_utc": created_at, "sealed_at_utc": None, "execution_identity": {"identity_version": IDENTITY_VERSION, "search_scope": {"payload_version": SCOPE_FP_VERSION, "sha256": compute_fingerprint(scope_payload(search))}, "qualification_criteria": None}, "search": search, "resume": {"attempt": 1, "resumed_from_run_id": None}}
    return env


def final_attempts(per_query: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finals: dict[str, dict[str, Any]] = {}
    for row in per_query:
        qid = row.get("query_id")
        if isinstance(qid, str) and (qid not in finals or row.get("attempt", 0) > finals[qid].get("attempt", 0)):
            finals[qid] = row
    return finals


def derive_search_status(planned: list[str], per_query: list[dict[str, Any]]) -> str:
    if not planned:
        return "FAILED"
    finals = final_attempts(per_query)
    states = [finals[qid]["status"] for qid in planned if qid in finals]
    if len(states) != len(planned):
        return "PARTIAL" if states else "FAILED"
    successes = sum(state == "success" for state in states)
    if successes == len(states):
        return "COMPLETE"
    if successes:
        return "PARTIAL"
    if all(state == "not_run" for state in states):
        return "FAILED"
    if any(state == "failed" for state in states):
        return "FAILED"
    return "PARTIAL"


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    path: str
    message: str


class EnvelopeError(ValueError):
    def __init__(self, issues: list[ValidationIssue] | str):
        self.issues = issues if isinstance(issues, list) else []
        super().__init__(issues if isinstance(issues, str) else "; ".join(i.code for i in issues))


def _issue(code: str, path: str, message: str, severity: str = "ERROR") -> ValidationIssue:
    return ValidationIssue(severity, code, path, message)


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _unknown_fields(obj: Any, allowed: set[str], path: str) -> list[ValidationIssue]:
    if not isinstance(obj, dict):
        return [_issue("E_UNKNOWN_FIELD", path, "expected object")]
    return [_issue("E_UNKNOWN_FIELD", f"{path}.{key}", "unknown field") for key in obj if key not in allowed]


def validate_envelope(env: dict[str, Any], stage: str = "seal") -> list[ValidationIssue]:
    if stage not in STAGES:
        return [_issue("E_UNKNOWN_ENUM", "stage", f"unknown stage {stage}")]
    issues: list[ValidationIssue] = []
    if not isinstance(env, dict):
        return [_issue("E_UNKNOWN_FIELD", "$", "envelope must be an object")]
    if env.get("envelope_version") != ENVELOPE_VERSION:
        issues.append(_issue("E_UNKNOWN_ENUM", "envelope_version", "unsupported envelope version"))
    if not isinstance(env.get("run_id"), str) or not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", env["run_id"]):
        issues.append(_issue("E_UNKNOWN_FIELD", "run_id", "run_id format is invalid"))
    issues.extend(_unknown_fields(env, {"envelope_version", "run_id", "created_at_utc", "sealed_at_utc", "execution_identity", "search", "handoff", "qualification", "pipeline", "resume"}, "$"))
    issues.extend(_unknown_fields(env.get("execution_identity"), {"identity_version", "search_scope", "qualification_criteria"}, "execution_identity"))
    if "search" not in env or not isinstance(env.get("search"), dict):
        return [_issue("E_STATUS_MISSING", "search", "search section or status missing")]
    search = env["search"]
    issues.extend(_unknown_fields(search, {"skill", "tool", "tool_version", "status", "params", "per_query", "results", "limits", "errors", "warnings", "started_at_utc", "finished_at_utc"}, "search"))
    params = search.get("params", {})
    planned_rows = params.get("queries", []) if isinstance(params, dict) else []
    planned = [q.get("query_id") for q in planned_rows if isinstance(q, dict)]
    if len(planned) != len(set(planned)):
        issues.append(_issue("E_QUERY_ID_UNKNOWN", "search.params.queries", "planned query_id values must be unique"))
    per_query = search.get("per_query", [])
    if "status" not in search or search["status"] is None:
        issues.append(_issue("E_STATUS_MISSING", "search.status", "search status is required"))
    elif search["status"] not in SEARCH_STATES:
        issues.append(_issue("E_UNKNOWN_ENUM", "search.status", "unknown search status"))
    if not isinstance(per_query, list):
        per_query = []
        issues.append(_issue("E_QUERY_UNACCOUNTED", "search.per_query", "must be a list"))
    attempts: dict[str, list[int]] = {}
    known_qids = set(planned)
    for idx, row in enumerate(per_query):
        path = f"search.per_query[{idx}]"
        if not isinstance(row, dict):
            issues.append(_issue("E_UNKNOWN_FIELD", path, "expected object"))
            continue
        qid = row.get("query_id")
        if qid not in known_qids:
            issues.append(_issue("E_QUERY_ID_UNKNOWN", f"{path}.query_id", "query_id is not planned"))
        issues.extend(_unknown_fields(row, {"query_id", "status", "count", "retrieved", "error", "substituted_query", "attempt"}, path))
        status = row.get("status")
        if status not in QUERY_STATES:
            issues.append(_issue("E_UNKNOWN_ENUM", f"{path}.status", "unknown query status"))
        attempt = row.get("attempt")
        if not isinstance(attempt, int) or attempt < 1:
            issues.append(_issue("E_ATTEMPT_SEQUENCE", f"{path}.attempt", "attempts begin at one"))
        else:
            attempts.setdefault(str(qid), []).append(attempt)
    for qid in planned:
        if qid not in {r.get("query_id") for r in per_query if isinstance(r, dict)}:
            issues.append(_issue("E_QUERY_UNACCOUNTED", f"search.per_query.{qid}", "planned query has no record"))
    for qid, nums in attempts.items():
        if sorted(nums) != list(range(1, len(nums) + 1)) or len(nums) != len(set(nums)):
            issues.append(_issue("E_ATTEMPT_SEQUENCE", f"search.per_query.{qid}", "attempt numbers must be unique and contiguous from one"))
    if "params" in search and isinstance(params, dict):
        for idx, query in enumerate(planned_rows):
            if not isinstance(query, dict) or not {"query_id", "query", "max_results"} <= query.keys():
                issues.append(_issue("E_UNKNOWN_FIELD", f"search.params.queries[{idx}]", "planned query fields are required"))
    try:
        identity_hash = env["execution_identity"]["search_scope"]["sha256"]
        if compute_fingerprint(scope_payload(search)) != identity_hash:
            issues.append(_issue("E_IDENTITY_TAMPER", "execution_identity.search_scope.sha256", "search scope fingerprint mismatch"))
    except (KeyError, TypeError, ValueError):
        issues.append(_issue("E_IDENTITY_TAMPER", "execution_identity.search_scope", "search scope identity missing or invalid"))
    for key in ("created_at_utc",):
        if key in env and not _is_timestamp(env[key]):
            issues.append(_issue("E_TIMESTAMP_FORMAT", key, "timestamp must be ISO-8601 UTC Z"))
    if "finished_at_utc" in search and not _is_timestamp(search["finished_at_utc"]):
        issues.append(_issue("E_TIMESTAMP_FORMAT", "search.finished_at_utc", "timestamp must be ISO-8601 UTC Z"))
    if stage in STAGES[1:]:
        handoff = env.get("handoff")
        if not isinstance(handoff, dict):
            issues.append(_issue("E_HANDOFF_MISSING", "handoff", "handoff required at this stage"))
        else:
            issues.extend(_unknown_fields(handoff, {"from_stage", "to_stage", "found_count", "transferred_gse_ids", "excluded", "allowed_to_proceed", "acceptance", "hash_version", "content_sha256", "created_at_utc"}, "handoff"))
            if handoff.get("allowed_to_proceed") not in {"complete", "partial_accepted", "blocked"}:
                issues.append(_issue("E_UNKNOWN_ENUM", "handoff.allowed_to_proceed", "unknown handoff policy"))
            _validate_handoff(env, handoff, issues)
    if stage in STAGES[2:]:
        qual = env.get("qualification")
        if not isinstance(qual, dict):
            issues.append(_issue("E_STATUS_MISSING", "qualification", "qualification section required"))
        else:
            if qual.get("status") not in QUAL_STATES:
                issues.append(_issue("E_UNKNOWN_ENUM", "qualification.status", "unknown qualification status"))
            issues.extend(_validate_qualification(qual, env.get("handoff", {}).get("transferred_gse_ids", [])))
            if qual.get("status") != "NOT_APPLICABLE":
                try:
                    expected = compute_fingerprint(criteria_payload(qual))
                    stored = env["execution_identity"]["qualification_criteria"]["sha256"]
                    if expected != stored:
                        issues.append(_issue("E_CRITERIA_FINGERPRINT_MISMATCH", "execution_identity.qualification_criteria.sha256", "criteria fingerprint mismatch"))
                except (KeyError, TypeError):
                    issues.append(_issue("E_CRITERIA_FINGERPRINT_MISMATCH", "execution_identity.qualification_criteria", "criteria identity missing"))
    if stage == "seal":
        if env.get("sealed_at_utc") is not None and not _is_timestamp(env.get("sealed_at_utc")):
            issues.append(_issue("E_TIMESTAMP_FORMAT", "sealed_at_utc", "timestamp must be ISO-8601 UTC Z"))
        if not isinstance(search.get("errors"), list) or not isinstance(search.get("warnings"), list):
            issues.append(_issue("E_UNKNOWN_FIELD", "search.errors", "errors and warnings must be lists"))
        derived = derive_search_status([str(q) for q in planned], per_query)
        if search.get("status") != derived:
            issues.append(_issue("E_STATUS_MISSING", "search.status", f"declared status must equal derived {derived}"))
        if isinstance(env.get("qualification"), dict):
            qual = env["qualification"]
            issues.extend(_unknown_fields(qual, {"skill", "status", "criteria_profile", "processed_gse_ids", "failed_gse_ids", "skipped_gse_ids", "decisions", "sample_counts", "persistence", "finished_at_utc", "started_at_utc", "errors", "skipped_reasons"}, "qualification"))
            try:
                qderived = derive_qualification_status(qual, env["handoff"]["transferred_gse_ids"])
                if qual.get("status") != qderived:
                    issues.append(_issue("E_STATUS_MISSING", "qualification.status", f"declared status must equal derived {qderived}"))
            except (ValueError, KeyError, TypeError):
                pass
        if "pipeline" in env:
            pipeline = env["pipeline"]
            if pipeline.get("outcome") not in OUTCOME_STATES:
                issues.append(_issue("E_UNKNOWN_ENUM", "pipeline.outcome", "unknown pipeline outcome"))
            try:
                if pipeline.get("outcome") != derive_pipeline_outcome(env):
                    issues.append(_issue("E_STATUS_MISSING", "pipeline.outcome", "declared outcome does not match derivation"))
            except (ValueError, KeyError, TypeError):
                pass
    return issues


def _validate_handoff(env: dict[str, Any], handoff: dict[str, Any], issues: list[ValidationIssue]) -> None:
    found = env.get("search", {}).get("results", {}).get("gse_ids", [])
    transferred = handoff.get("transferred_gse_ids", [])
    excluded = handoff.get("excluded", [])
    excluded_ids = [e.get("gse_id") for e in excluded if isinstance(e, dict)] if isinstance(excluded, list) else []
    def fail(n: int, message: str) -> None:
        issues.append(_issue(f"E_HANDOFF_V{n}", "handoff", message))
    if not isinstance(found, list) or not all(isinstance(item, str) for item in found):
        fail(5, "found accessions must be strings")
        found = []
    if not isinstance(transferred, list) or not all(isinstance(item, str) for item in transferred):
        fail(5, "transferred accessions must be strings")
        transferred = []
    if handoff.get("hash_version") != HASH_VERSION:
        fail(7, "unsupported handoff hash version")
    if handoff.get("found_count") != len(found):
        fail(1, "found_count differs from search result count")
    if len(transferred) != len(set(transferred)) or len(excluded_ids) != len(set(excluded_ids)):
        fail(2, "duplicates in transferred or excluded")
    if set(transferred) & set(excluded_ids):
        fail(3, "transferred and excluded overlap")
    if set(transferred) | set(excluded_ids) != set(found):
        fail(4, "found candidates are not fully accounted for")
    if not set(transferred) <= set(found) or not set(excluded_ids) <= set(found):
        fail(5, "handoff contains candidate absent from search results")
    if isinstance(excluded, list):
        for index, entry in enumerate(excluded):
            if not isinstance(entry, dict) or entry.get("reason_code") not in EXCLUSION_REASONS or not all(entry.get(k) for k in ("reason_note", "by", "at_utc")):
                fail(6, f"excluded[{index}] requires valid reason_code, reason_note, by, at_utc")
    try:
        recomputed_hash = compute_handoff_hash(env)
    except (KeyError, TypeError):
        recomputed_hash = ""
    if handoff.get("content_sha256") != recomputed_hash:
        fail(7, "handoff hash mismatch")
        issues.append(_issue("E_HASH_MISMATCH", "handoff.content_sha256", "recomputed handoff hash differs"))
    for key in ("created_at_utc",):
        if key in handoff and not _is_timestamp(handoff[key]):
            issues.append(_issue("E_TIMESTAMP_FORMAT", f"handoff.{key}", "timestamp must be ISO-8601 UTC Z"))


def _validate_qualification(qual: dict[str, Any], transferred: list[str]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    processed, failed, skipped = [qual.get(k, []) for k in ("processed_gse_ids", "failed_gse_ids", "skipped_gse_ids")]
    decisions = qual.get("decisions", {})
    decision_lists = [decisions.get(k, []) for k in ("include", "exclude", "uncertain")] if isinstance(decisions, dict) else [[], [], []]
    if any(not isinstance(items, list) or not all(isinstance(item, str) for item in items) for items in (processed, failed, skipped)):
        return [_issue("E_UNKNOWN_FIELD", "qualification", "accounting categories must be GSE id lists")]
    if not isinstance(transferred, list) or not all(isinstance(item, str) for item in transferred):
        return [_issue("E_QUERY_UNACCOUNTED", "handoff.transferred_gse_ids", "transferred must be GSE id list")]
    all_categories = [set(x) for x in (processed, failed, skipped)]
    if set.union(*all_categories) != set(transferred):
        issues.append(_issue("E_QUERY_UNACCOUNTED", "qualification", "transferred ids must be fully accounted for"))
    if any(all_categories[a] & all_categories[b] for a in range(3) for b in range(a + 1, 3)):
        issues.append(_issue("E_UNKNOWN_FIELD", "qualification", "processed/failed/skipped must be disjoint"))
    if any(not isinstance(items, list) or not all(isinstance(item, str) for item in items) for items in decision_lists):
        return issues + [_issue("E_UNKNOWN_FIELD", "qualification.decisions", "decision lists must contain GSE ids")]
    sets = [set(x) for x in decision_lists]
    if set.union(*sets) != set(processed) or any(sets[a] & sets[b] for a in range(3) for b in range(a + 1, 3)):
        issues.append(_issue("E_UNKNOWN_FIELD", "qualification.decisions", "decisions must partition processed ids"))
    mentioned = set.union(*all_categories, *sets)
    if not mentioned <= set(transferred):
        issues.append(_issue("E_QUERY_ID_UNKNOWN", "qualification", "untransferred id appears in qualification"))
    errors = qual.get("errors", [])
    error_ids = {e.get("gse_id") for e in errors if isinstance(e, dict)}
    if any(not isinstance(error, dict) or not error.get("detail") for error in errors if isinstance(error, dict)) or not isinstance(errors, list):
        issues.append(_issue("E_UNKNOWN_FIELD", "qualification.errors", "errors entries require detail"))
    if not set(failed) <= error_ids:
        issues.append(_issue("E_QUERY_UNACCOUNTED", "qualification.errors", "failed ids require errors details"))
    skip_rows = qual.get("skipped_reasons", [])
    if not isinstance(skip_rows, list) or any(not isinstance(row, dict) or not row.get("reason") for row in skip_rows):
        issues.append(_issue("E_UNKNOWN_FIELD", "qualification.skipped_reasons", "skipped reasons require detail"))
    skip_ids = {e.get("gse_id") for e in skip_rows if isinstance(e, dict)}
    if not set(skipped) <= skip_ids:
        issues.append(_issue("E_QUERY_UNACCOUNTED", "qualification.skipped_reasons", "skipped ids require reasons"))
    return issues


def derive_qualification_status(qual: dict[str, Any], transferred_gse_ids: list[str]) -> str:
    if not transferred_gse_ids:
        return "NOT_APPLICABLE"
    issues = _validate_qualification(qual, transferred_gse_ids)
    if issues:
        raise ValueError("qualification accounting invalid: " + ",".join(i.code for i in issues))
    processed, failed, skipped = [qual.get(k, []) for k in ("processed_gse_ids", "failed_gse_ids", "skipped_gse_ids")]
    if not failed and not skipped:
        return "COMPLETE"
    if processed:
        return "PARTIAL"
    return "FAILED"


def check_admission(env: dict[str, Any] | None) -> tuple[str, list[ValidationIssue]]:
    if not isinstance(env, dict):
        return "BLOCKED_INVALID_ENVELOPE", [_issue("E_STATUS_MISSING", "$", "envelope unavailable")]
    issues = validate_envelope(env, "handoff")
    if any(i.severity == "ERROR" for i in issues):
        return "BLOCKED_INVALID_ENVELOPE", issues
    status = env["search"]["status"]
    if status == "FAILED":
        return "BLOCKED_UPSTREAM", [_issue("E_UNKNOWN_ENUM", "search.status", "search failed upstream")]
    if status not in {"COMPLETE", "PARTIAL"}:
        return "BLOCKED_INVALID_ENVELOPE", [_issue("E_STATUS_MISSING", "search.status", "search status is absent or unsupported")]
    handoff = env["handoff"]
    if handoff.get("allowed_to_proceed") == "blocked":
        return "BLOCKED_UPSTREAM", [_issue("E_UNKNOWN_ENUM", "handoff.allowed_to_proceed", "handoff explicitly blocked")]
    if status == "PARTIAL" and not (handoff.get("allowed_to_proceed") == "partial_accepted" and isinstance(handoff.get("acceptance"), dict) and all(handoff["acceptance"].get(k) for k in ("by", "reason", "accepted_at_utc"))):
        return "BLOCKED_UPSTREAM", [_issue("E_UNKNOWN_ENUM", "handoff.acceptance", "partial search requires explicit acceptance")]
    return "ADMIT", issues


def derive_pipeline_outcome(env: dict[str, Any]) -> str | None:
    if "handoff" not in env:
        return None
    search_status = env.get("search", {}).get("status")
    handoff = env["handoff"]
    qual = env.get("qualification")
    found = env.get("search", {}).get("results", {}).get("gse_ids", [])
    transferred = handoff.get("transferred_gse_ids", [])
    excluded = handoff.get("excluded", [])
    if search_status == "FAILED" or search_status in {"BLOCKED_INVALID_ENVELOPE", "BLOCKED_UPSTREAM"}:
        return "FAILURE"
    if search_status == "PARTIAL" and handoff.get("allowed_to_proceed") != "partial_accepted":
        return "FAILURE"
    all_excluded = (search_status == "COMPLETE" and len(found) > 0 and handoff.get("found_count") == len(found) and not transferred and len(excluded) == len({e.get("gse_id") for e in excluded}) and {e.get("gse_id") for e in excluded} == set(found) and all(e.get("reason_code") in EXCLUSION_REASONS and all(e.get(k) for k in ("reason_note", "by", "at_utc")) for e in excluded) and isinstance(qual, dict) and qual.get("status") == "NOT_APPLICABLE")
    if all_excluded:
        return "ALL_EXCLUDED"
    if not qual:
        return None
    qstatus = qual.get("status")
    if qstatus in {"FAILED", "BLOCKED_INVALID_ENVELOPE", "BLOCKED_UPSTREAM"}:
        return "FAILURE"
    if search_status == "PARTIAL" or qstatus == "PARTIAL" or handoff.get("allowed_to_proceed") == "partial_accepted":
        return "PARTIAL_SUCCESS"
    if search_status == "COMPLETE" and not found and qstatus == "NOT_APPLICABLE":
        return "NO_CANDIDATES"
    if qstatus == "COMPLETE":
        return "SUCCESS" if qual.get("decisions", {}).get("include") else "EMPTY_COMPLETE"
    if qstatus == "NOT_APPLICABLE" and found and transferred:
        return "FAILURE"
    return "FAILURE"


def read_envelope_locked(path: str | Path) -> tuple[dict[str, Any], Any]:
    envelope_path = Path(path)
    lock_fh = open(envelope_path.with_name(envelope_path.name + ".lock"), "a+")  # noqa: SIM115 -- caller releases after snapshot use
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
    try:
        env = json.loads(envelope_path.read_text(encoding="utf-8"))
        return env, lock_fh
    except Exception:
        lock_fh.close()
        raise


def write_envelope_atomic(path: str | Path, env: dict[str, Any]) -> None:
    envelope_path = Path(path)
    envelope_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = envelope_path.with_name(envelope_path.name + ".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if envelope_path.exists():
            existing = json.loads(envelope_path.read_text(encoding="utf-8"))
            if existing.get("sealed_at_utc") is not None:
                if canonical_json_bytes(existing) == canonical_json_bytes(env):
                    return
                raise EnvelopeError([_issue("E_ENVELOPE_SEALED", "sealed_at_utc", "sealed envelope is immutable")])
        fd, tmp_name = tempfile.mkstemp(prefix=envelope_path.name + ".tmp.", dir=envelope_path.parent)
        try:
            with os.fdopen(fd, "wb") as tmp_fh:
                tmp_fh.write(canonical_json_bytes(env))
                tmp_fh.flush()
                os.fsync(tmp_fh.fileno())
            os.replace(tmp_name, envelope_path)
            dir_fd = os.open(envelope_path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def _assert_unsealed(env: dict[str, Any]) -> None:
    if env.get("sealed_at_utc") is not None:
        raise EnvelopeError([_issue("E_ENVELOPE_SEALED", "sealed_at_utc", "sealed envelope is immutable")])


def _mutate_path(path: str | Path, mutation: Any) -> dict[str, Any]:
    envelope_path = Path(path)
    with open(envelope_path.with_name(envelope_path.name + ".lock"), "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        env = json.loads(envelope_path.read_text(encoding="utf-8"))
        _assert_unsealed(env)
        updated = mutation(env)
        if updated is None:
            updated = env
        fd, tmp_name = tempfile.mkstemp(prefix=envelope_path.name + ".tmp.", dir=envelope_path.parent)
        try:
            with os.fdopen(fd, "wb") as tmp_fh:
                tmp_fh.write(canonical_json_bytes(updated))
                tmp_fh.flush()
                os.fsync(tmp_fh.fileno())
            os.replace(tmp_name, envelope_path)
            dir_fd = os.open(envelope_path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return updated


def build_handoff(env: dict[str, Any], transferred: list[str], excluded: list[dict[str, Any]], allowed: str = "complete", acceptance: dict[str, Any] | None = None, now: str | None = None) -> dict[str, Any]:
    candidate = json.loads(canonical_json_bytes(env))
    _assert_unsealed(candidate)
    if "handoff" in candidate:
        old_hash = candidate["handoff"].get("content_sha256")
        trial = dict(candidate)
        trial["handoff"] = {"from_stage": "search", "to_stage": "qualification", "found_count": len(candidate["search"]["results"]["gse_ids"]), "transferred_gse_ids": transferred, "excluded": excluded, "allowed_to_proceed": allowed, "acceptance": acceptance, "hash_version": HASH_VERSION, "content_sha256": "", "created_at_utc": candidate["handoff"].get("created_at_utc", now or utc_now())}
        trial["handoff"]["content_sha256"] = compute_handoff_hash(trial)
        if old_hash == trial["handoff"]["content_sha256"]:
            if candidate["handoff"].get("allowed_to_proceed") == "partial_accepted" and isinstance(candidate["handoff"].get("acceptance"), dict):
                trial["handoff"]["allowed_to_proceed"] = "partial_accepted"
                trial["handoff"]["acceptance"] = candidate["handoff"]["acceptance"]
                trial["handoff"]["content_sha256"] = compute_handoff_hash(trial)
                if trial["handoff"]["content_sha256"] != old_hash:
                    raise EnvelopeError([_issue("E_ENVELOPE_CONFLICT", "handoff", "acceptance policy differs")])
            if candidate.get("qualification"):
                candidate.setdefault("search", {}).setdefault("warnings", []).append({"code": "W_REPEATED_HANDOFF", "at_utc": now or utc_now()})
                return candidate
            candidate.setdefault("search", {}).setdefault("warnings", []).append({"code": "W_REPEATED_HANDOFF", "at_utc": now or utc_now()})
            return candidate
        raise EnvelopeError([_issue("E_ENVELOPE_CONFLICT", "handoff", "different handoff already exists for this run_id")])
    candidate["handoff"] = {"from_stage": "search", "to_stage": "qualification", "found_count": len(candidate["search"]["results"]["gse_ids"]), "transferred_gse_ids": transferred, "excluded": excluded, "allowed_to_proceed": allowed, "acceptance": acceptance, "hash_version": HASH_VERSION, "content_sha256": "", "created_at_utc": now or utc_now()}
    candidate["handoff"]["content_sha256"] = compute_handoff_hash(candidate)
    errors = [i for i in validate_envelope(candidate, "handoff") if i.severity == "ERROR"]
    if errors:
        raise EnvelopeError(errors)
    return candidate


def write_qualification_section(env: dict[str, Any], qual: dict[str, Any], skill_md_path: str | Path, now: str | None = None) -> dict[str, Any]:
    candidate = json.loads(canonical_json_bytes(env))
    _assert_unsealed(candidate)
    if candidate.get("handoff"):
        admission, issues = check_admission(candidate)
        if admission != "ADMIT":
            raise EnvelopeError(issues)
    skill_hash = hashlib.sha256(Path(skill_md_path).read_bytes()).hexdigest()
    qual = json.loads(canonical_json_bytes(qual))
    qual.setdefault("criteria_profile", {})["skill_sha256"] = skill_hash
    fp = compute_fingerprint(criteria_payload(qual))
    current = candidate["execution_identity"].get("qualification_criteria")
    identity = {"payload_version": CRITERIA_FP_VERSION, "sha256": fp}
    if current is not None and current != identity:
        raise EnvelopeError([_issue("E_CRITERIA_FINGERPRINT_MISMATCH", "execution_identity.qualification_criteria", "criteria changed during run")])
    candidate["execution_identity"]["qualification_criteria"] = identity
    qual["criteria_profile"]["skill_sha256"] = skill_hash
    qual.setdefault("finished_at_utc", now or utc_now())
    candidate["qualification"] = qual
    return candidate


def refuse(env: dict[str, Any], stage: str, status: str, reason: str, now: str | None = None) -> dict[str, Any]:
    candidate = json.loads(canonical_json_bytes(env))
    _assert_unsealed(candidate)
    candidate[stage] = {"status": status, "errors": [{"code": "BLOCKED", "message": reason}], "finished_at_utc": now or utc_now()}
    return candidate


def apply_resume(env: dict[str, Any], new_invocation: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    candidate = json.loads(canonical_json_bytes(env))
    _assert_unsealed(candidate)
    try:
        new_scope = compute_fingerprint(scope_payload(new_invocation["search"]))
    except (KeyError, TypeError) as exc:
        raise EnvelopeError([_issue("E_SCOPE_FINGERPRINT_MISMATCH", "search", "new invocation scope unavailable")]) from exc
    old_scope = candidate["execution_identity"]["search_scope"]["sha256"]
    if new_scope != old_scope:
        raise EnvelopeError([_issue("E_SCOPE_FINGERPRINT_MISMATCH", "execution_identity.search_scope", "scope changed; use a new run_id")])
    new_qual = new_invocation.get("qualification")
    if isinstance(new_qual, dict) and candidate["execution_identity"].get("qualification_criteria"):
        stored = candidate["execution_identity"]["qualification_criteria"]["sha256"]
        try:
            proposed = compute_fingerprint(criteria_payload(new_qual))
        except (KeyError, TypeError):
            proposed = ""
        if proposed != stored:
            raise EnvelopeError([_issue("E_CRITERIA_FINGERPRINT_MISMATCH", "execution_identity.qualification_criteria", "criteria changed; use a new run_id")])
    candidate.setdefault("resume", {"attempt": 1, "resumed_from_run_id": None})["attempt"] += 1
    return candidate


def seal(env: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    candidate = json.loads(canonical_json_bytes(env))
    if candidate.get("sealed_at_utc") is not None:
        errors = validate_envelope(candidate, "seal")
        if not any(i.severity == "ERROR" for i in errors):
            candidate.setdefault("search", {}).setdefault("warnings", []).append({"code": "W_ALREADY_SEALED", "at_utc": now or utc_now()})
            return candidate
        raise EnvelopeError([_issue("E_ENVELOPE_SEALED", "sealed_at_utc", "sealed envelope is immutable")])
    errors = [i for i in validate_envelope(candidate, "seal") if i.severity == "ERROR"]
    if errors:
        raise EnvelopeError(errors)
    candidate["sealed_at_utc"] = now or utc_now()
    candidate.setdefault("pipeline", {})["outcome"] = derive_pipeline_outcome(candidate)
    return candidate


def report(env: dict[str, Any]) -> str:
    search = env.get("search", {})
    handoff = env.get("handoff", {})
    failed = [r.get("query_id") for r in final_attempts(search.get("per_query", [])).values() if r.get("status") != "success"]
    rationale = derive_pipeline_outcome(env) or "OPEN: outcome not yet derivable"
    return "\n".join(["Run: " + str(env.get("run_id", "unknown")), "Search status: " + str(search.get("status", "MISSING")), "Non-coverage: queries not fully successful: " + str(failed), "Handoff: found=" + str(handoff.get("found_count", 0)) + " transferred=" + str(len(handoff.get("transferred_gse_ids", []))) + " excluded=" + str(len(handoff.get("excluded", []))), "Outcome: " + str(rationale), "Coverage statement: this report describes recorded execution only; it does not claim upstream absence."])

def criteria_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load(path: str) -> dict[str, Any]:
    env, lock_fh = read_envelope_locked(path)
    lock_fh.close()
    return env


@click.group()
def cli() -> None:
    pass


@cli.command("validate")
@click.argument("path", type=click.Path(exists=True))
@click.option("--stage", type=click.Choice(STAGES), default="seal", show_default=True)
def cli_validate(path: str, stage: str) -> None:
    issues = validate_envelope(_load(path), stage)
    click.echo(json.dumps([issue.__dict__ for issue in issues], ensure_ascii=False))
    if any(i.severity == "ERROR" for i in issues):
        raise click.exceptions.Exit(1)


@cli.command("derive")
@click.argument("path", type=click.Path(exists=True))
def cli_derive(path: str) -> None:
    env = _load(path)
    click.echo(json.dumps({"search_status": env.get("search", {}).get("status"), "qualification_status": env.get("qualification", {}).get("status"), "outcome": derive_pipeline_outcome(env)}, ensure_ascii=False))


@cli.command("admit")
@click.argument("path", type=click.Path(exists=True))
def cli_admit(path: str) -> None:
    decision, issues = check_admission(_load(path))
    click.echo(json.dumps({"decision": decision, "issues": [i.__dict__ for i in issues]}, ensure_ascii=False))
    if decision != "ADMIT":
        raise click.exceptions.Exit(1)


@cli.command("report")
@click.argument("path", type=click.Path(exists=True))
def cli_report(path: str) -> None:
    click.echo(report(_load(path)))


@cli.command("criteria-hash")
@click.argument("path", type=click.Path(exists=True))
def cli_criteria_hash(path: str) -> None:
    click.echo(criteria_hash(path))


@cli.command("init-run")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--run-id", default=None)
@click.option("--skill-version", default="unknown")
@click.option("--tool-version", default="1.1.0")
@click.option("--query", "queries", multiple=True, required=True)
@click.option("--max-results", default=100, type=click.IntRange(1, 10000))
@click.option("--species", default="Homo sapiens [Organism]")
@click.option("--retstart-pagination/--no-retstart-pagination", default=False)
def cli_init(path: Path, run_id: str | None, skill_version: str, tool_version: str, queries: tuple[str, ...], max_results: int, species: str, retstart_pagination: bool) -> None:
    actual_run_id = run_id or new_run_id()
    params = {"queries": [{"query_id": f"q{index}", "query": query, "max_results": max_results} for index, query in enumerate(queries, 1)], "species_constraint": species}
    env = envelope_skeleton(actual_run_id, utc_now(), {"name": "sc-dataset-search", "version": skill_version}, "geo_search.py", tool_version, params, {"retstart_pagination": retstart_pagination, "max_results_per_query": max_results})
    write_envelope_atomic(path, env)
    click.echo(json.dumps({"run_id": actual_run_id, "path": str(path)}, ensure_ascii=False))


@cli.command("attach-search")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.argument("search_json", type=click.Path(exists=True, path_type=Path))
def cli_attach_search(path: Path, search_json: Path) -> None:
    search_section = json.loads(search_json.read_text(encoding="utf-8"))
    def update(env: dict[str, Any]) -> dict[str, Any]:
        env["search"].update(search_section)
        errors = [issue for issue in validate_envelope(env, "search") if issue.severity == "ERROR"]
        if errors:
            raise EnvelopeError(errors)
        return env
    updated = _mutate_path(path, update)
    click.echo(json.dumps({"status": updated["search"]["status"]}, ensure_ascii=False))


@cli.command("build-handoff")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.argument("handoff_json", type=click.Path(exists=True, path_type=Path))
def cli_handoff(path: Path, handoff_json: Path) -> None:
    data = json.loads(handoff_json.read_text(encoding="utf-8"))
    def update(env: dict[str, Any]) -> dict[str, Any]:
        return build_handoff(env, data["transferred_gse_ids"], data.get("excluded", []), data.get("allowed_to_proceed", "complete"), data.get("acceptance"), data.get("created_at_utc"))
    env = _mutate_path(path, update)
    click.echo(json.dumps({"content_sha256": env["handoff"]["content_sha256"]}))


@cli.command("write-qualification")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.argument("qualification_json", type=click.Path(exists=True, path_type=Path))
@click.option("--skill-md", required=True, type=click.Path(exists=True, path_type=Path))
def cli_qualification(path: Path, qualification_json: Path, skill_md: Path) -> None:
    data = json.loads(qualification_json.read_text(encoding="utf-8"))
    env = _mutate_path(path, lambda current: write_qualification_section(current, data, skill_md))
    click.echo(json.dumps({"status": env["qualification"].get("status")}))


@cli.command("refuse")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--stage", required=True)
@click.option("--status", required=True)
@click.option("--reason", required=True)
def cli_refuse(path: Path, stage: str, status: str, reason: str) -> None:
    env = _mutate_path(path, lambda current: refuse(current, stage, status, reason))
    click.echo(json.dumps({"status": env[stage]["status"]}))


@cli.command("resume")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.argument("invocation_json", type=click.Path(exists=True, path_type=Path))
def cli_resume(path: Path, invocation_json: Path) -> None:
    invocation = json.loads(invocation_json.read_text(encoding="utf-8"))
    env = _mutate_path(path, lambda current: apply_resume(current, invocation))
    click.echo(json.dumps({"attempt": env["resume"]["attempt"]}))


@cli.command("seal")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def cli_seal(path: Path) -> None:
    current = _load(str(path))
    if current.get("sealed_at_utc") is not None:
        updated = seal(current)
    else:
        updated = _mutate_path(path, lambda env: seal(env))
    click.echo(json.dumps({"sealed_at_utc": updated["sealed_at_utc"], "outcome": updated.get("pipeline", {}).get("outcome"), "warnings": [warning["code"] for warning in updated.get("search", {}).get("warnings", []) if warning.get("code") == "W_ALREADY_SEALED"]}))


if __name__ == "__main__":
    cli()
