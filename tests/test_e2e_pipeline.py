from __future__ import annotations

import json

import pytest
from e2e_harness import (
    RUN_ID,
    FakeNCBI,
    FakeResponse,
    PipelineHarness,
    full_chain,
    make_qualification,
    soft_gzip,
)
from tools.sc_dataset import geo_envelope, geo_metadata


@pytest.fixture(autouse=True)
def forbid_real_sockets(monkeypatch):
    import socket

    def blocked(*_args, **_kwargs):
        raise AssertionError("socket guard: real network connections are forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def test_unregistered_requests_get_hits_socket_guard(monkeypatch):

    import requests

    with pytest.raises(AssertionError, match="socket guard"):
        requests.get("https://unregistered.example/escape", timeout=0.1)

@pytest.fixture

def pipeline(tmp_path, monkeypatch):
    harness = PipelineHarness(tmp_path, monkeypatch)
    yield harness
    harness.assert_production_db_unchanged()


def test_network_guard_rejects_unregistered_url():
    fake = FakeNCBI()
    with pytest.raises(AssertionError, match="unregistered URL"):
        fake.request("https://unregistered.example/escape")


def test_success_full_chain_registers_and_seals(pipeline):
    result = full_chain(pipeline, include=["GSE123456"])
    envelope = json.loads(pipeline.envelope.read_text())
    assert result["seal"][0] == 0
    assert envelope["pipeline"]["outcome"] == "SUCCESS"
    assert [row[0] for row in pipeline.rows("geo_datasets")] == ["GSE123456"]
    assert [row[0] for row in pipeline.rows("geo_samples")] == ["GSM100001"]
    assert pipeline.rows("geo_qualification") == [("GSE123456", "include")]
    assert (pipeline.run_dir / "metadata_raw/GSE123456.soft.gz").is_file()
    assert (pipeline.run_dir / "metadata_parsed/GSE123456.json").is_file()


def test_empty_complete_zero_includes_still_registers(pipeline):
    result = full_chain(pipeline, include=[])
    envelope = json.loads(pipeline.envelope.read_text())
    assert result["seal"][0] == 0
    assert envelope["pipeline"]["outcome"] == "EMPTY_COMPLETE"
    assert [row[0] for row in pipeline.rows("geo_datasets")] == ["GSE123456"]
    assert pipeline.rows("geo_qualification") == []


def test_no_candidates_search_complete_seals_without_db_writes(pipeline):
    result = full_chain(pipeline, found=())
    envelope = json.loads(pipeline.envelope.read_text())
    assert result["search"][0] == 0
    assert envelope["search"]["status"] == "COMPLETE"
    assert envelope["pipeline"]["outcome"] == "NO_CANDIDATES"
    assert all(pipeline.rows(table) == [] for table in ("geo_datasets", "geo_samples", "geo_qualification"))


def test_all_excluded_has_reasons_and_zero_db_writes(pipeline):
    excluded = [{"gse_id": "GSE123456", "reason_code": "already_registered", "reason_note": "fixture existing", "by": "test", "at_utc": "2026-11-23T12:00:00Z"}]
    result = full_chain(pipeline, transferred=[], excluded=excluded)
    envelope = json.loads(pipeline.envelope.read_text())
    assert result["handoff"][0] == 0
    assert result["seal"][0] == 0
    assert envelope["pipeline"]["outcome"] == "ALL_EXCLUDED"
    assert envelope["handoff"]["excluded"] == excluded
    assert all(pipeline.rows(table) == [] for table in ("geo_datasets", "geo_samples", "geo_qualification"))


def test_partial_success_acceptance_seals_registered_subset(pipeline):
    query = "fixture query"
    partial = FakeResponse(payload={"esearchresult": {"count": "2", "idlist": ["0"]}})
    accepted = {"by": "test-user", "reason": "fixture coverage review", "accepted_at_utc": "2026-11-23T12:00:00Z"}
    result = full_chain(pipeline, found=("GSE123456",), include=["GSE123456"], search_scripts={query: (partial,)}, allowed="partial_accepted", acceptance=accepted)
    envelope = json.loads(pipeline.envelope.read_text())
    assert result["search"][0] == 0
    assert result["admit"][0] == 0
    assert envelope["search"]["status"] == "PARTIAL"
    assert envelope["pipeline"]["outcome"] == "PARTIAL_SUCCESS"
    assert [row[0] for row in pipeline.rows("geo_datasets")] == ["GSE123456"]


def test_failure_all_queries_failed_blocks_and_preserves_empty_db(pipeline):
    failures = tuple(FakeResponse(status_code=503) for _ in range(4))
    result = full_chain(pipeline, found=(), search_scripts={"fixture query": failures})
    envelope = json.loads(pipeline.envelope.read_text())
    assert result["search"][0] == 0
    assert result["admit"][0] == 1
    assert result["refuse"][0] == 0
    assert result["seal"][0] == 1
    assert envelope["search"]["status"] == "FAILED"
    assert envelope["qualification"]["status"] == "BLOCKED_UPSTREAM"
    assert envelope.get("sealed_at_utc") is None
    assert result["seal"][1]["issues"][0]["code"] == "E_HANDOFF_MISSING"
    assert result["derive"][1]["outcome"] == "FAILURE"
    assert geo_envelope.derive_pipeline_outcome(envelope) == "FAILURE"
    assert all(pipeline.rows(table) == [] for table in ("geo_datasets", "geo_samples", "geo_qualification"))


def test_garbage_envelope_admission_refuses_without_database_writes(pipeline):
    pipeline.envelope.write_text("{not json")
    result = pipeline.run_admit()
    assert result[0] == 1
    assert all(pipeline.rows(table) == [] for table in ("geo_datasets", "geo_samples"))


def test_missing_handoff_refuses_qualification_d3(pipeline):
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456",))
    pipeline.run_search()
    result = pipeline.run_write_qual(make_qualification(["GSE123456"]))
    assert result[0] == 1
    assert any(issue.code == "E_HANDOFF_MISSING" for issue in geo_envelope.validate_envelope(json.loads(pipeline.envelope.read_text()), "handoff"))


def test_resume_scope_fingerprint_drift_refuses_and_new_run_path_is_available(pipeline):
    pipeline.run_init()
    pipeline.register_query("fixture query", ())
    pipeline.run_search()
    drift = pipeline.run_resume(query="changed query")
    assert drift[0] == 1
    assert "E_SCOPE_FINGERPRINT_MISMATCH" in str(drift[1])
    fresh = pipeline.root / "new-run.json"
    created = pipeline.command(geo_envelope.cli, ["init-run", str(fresh), "--run-id", "20261123T120001Z-abcdef02", "--query", "changed query"])
    assert created[0] == 0
    assert json.loads(fresh.read_text())["run_id"] == "20261123T120001Z-abcdef02"


def test_incomplete_handoff_accounting_fails_v_code(pipeline):
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456", "GSE123457"))
    pipeline.run_search()
    result = pipeline.run_handoff(["GSE123456"])
    assert result[0] == 1
    assert not json.loads(pipeline.envelope.read_text()).get("handoff")


def test_repeated_handoff_is_idempotent(pipeline):
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456",))
    pipeline.run_search()
    assert pipeline.run_handoff(["GSE123456"])[0] == 0
    first = json.loads(pipeline.envelope.read_text())["handoff"]
    assert pipeline.run_handoff(["GSE123456"])[0] == 0
    second = json.loads(pipeline.envelope.read_text())["handoff"]
    assert first["content_sha256"] == second["content_sha256"]
    assert any(w["code"] == "W_REPEATED_HANDOFF" for w in json.loads(pipeline.envelope.read_text())["search"]["warnings"])


def test_retry_then_success_is_logged(pipeline):
    fail = FakeResponse(status_code=503)
    pipeline.register_soft("GSE123456", script=(fail, FakeResponse(content=soft_gzip("GSE123456"))))
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456",))
    pipeline.run_search()
    pipeline.run_handoff(["GSE123456"])
    result = pipeline.run_fetch("GSE123456")
    events = [json.loads(line) for line in (pipeline.run_dir / "metadata_fetch.jsonl").read_text().splitlines()]
    assert result[0] == 0
    assert len([call for call in pipeline.fake.calls if call[0] == geo_metadata._get_soft_url("GSE123456")]) == 2
    assert events[-1]["status"] == "ok"
    assert events[-1]["attempts"] == 2


def test_persistent_5xx_is_failed_and_accounted_downstream(pipeline):
    pipeline.register_soft("GSE123456", script=tuple(FakeResponse(status_code=503) for _ in range(4)))
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456",))
    pipeline.run_search()
    pipeline.run_handoff(["GSE123456"])
    result = pipeline.run_fetch("GSE123456")
    event = json.loads((pipeline.run_dir / "metadata_fetch.jsonl").read_text().splitlines()[-1])
    assert result[0] == 1
    assert event["status"] == "failed"
    assert event["attempts"] == 4
    assert event["error_class"] == "transient_http"


def test_retry_after_75_defers_then_same_run_resume_avoids_completed_gse(pipeline):
    pipeline.register_soft("GSE123456", script=(FakeResponse(status_code=429, headers={"Retry-After": "75"}), FakeResponse(content=soft_gzip("GSE123456"))))
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456",))
    pipeline.run_search()
    pipeline.run_handoff(["GSE123456"])
    deferred = pipeline.run_fetch("GSE123456")
    assert deferred[0] == 75
    event_count = len(pipeline.fake.calls)
    resumed = pipeline.run_fetch("GSE123456")
    assert resumed[0] == 0
    assert len([call for call in pipeline.fake.calls if call[0] == geo_metadata._get_soft_url("GSE123456")]) == 2
    assert len(pipeline.fake.calls) >= event_count


def test_same_run_id_resume_happy_path_increments_attempt(pipeline):
    pipeline.run_init()
    pipeline.register_query("fixture query", ())
    pipeline.run_search()
    result = pipeline.run_resume()
    assert result[0] == 0
    assert json.loads(pipeline.envelope.read_text())["resume"]["attempt"] == 2


def test_recover_raw_before_parsed_is_offline_then_preflight_and_register(pipeline):
    pipeline.register_soft("GSE123456")
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456",))
    pipeline.run_search()
    pipeline.run_handoff(["GSE123456"])
    assert pipeline.run_fetch("GSE123456")[0] == 0
    (pipeline.run_dir / "metadata/GSE123456.json").unlink()
    (pipeline.run_dir / "metadata_parsed/GSE123456.json").unlink()
    calls = len(pipeline.fake.calls)
    recovered = pipeline.run_recover("GSE123456")
    assert recovered[0] == 0
    assert len(pipeline.fake.calls) == calls
    assert pipeline.run_preflight("GSE123456")[0] == 0
    assert pipeline.run_register("GSE123456")[0] == 0


def test_partial_registration_resumes_same_gse(pipeline):
    pipeline.register_soft("GSE123456")
    pipeline.run_init()
    pipeline.register_query("fixture query", ("GSE123456",))
    pipeline.run_search()
    pipeline.run_handoff(["GSE123456"])
    pipeline.run_fetch("GSE123456")
    pipeline.run_preflight("GSE123456")
    artifact = json.loads((pipeline.run_dir / "metadata_parsed/GSE123456.json").read_text())
    grant = str(pipeline.run_dir / "preflight/GSE123456.grant.json")
    first = pipeline.runner.invoke(__import__("tools.sc_dataset.geo_database", fromlist=["cli"]).cli, ["--db", str(pipeline.db), "save-dataset", "GSE123456", "--preflight", grant])
    assert first.exit_code == 0
    resumed = pipeline.run_register("GSE123456")
    assert resumed[0] == 0
    assert len(pipeline.rows("geo_samples")) == len(artifact["samples"])


def test_crash_between_verify_and_write_qualification_seal_still_works(pipeline):
    result = full_chain(pipeline, hooks={"qualification": lambda _h, _r: "skip"})
    assert result["verify:GSE123456"][0] == 0
    assert result["seal"][0] == 1


def test_crash_between_verify_and_qualification_write_can_resume(pipeline):
    result = full_chain(pipeline, hooks={"qualification": lambda _h, _r: "skip"})
    assert result["verify:GSE123456"][0] == 0
    assert pipeline.run_write_qual(make_qualification(["GSE123456"]))[0] == 0
    assert pipeline.run_seal()[0] == 0


def test_no_persistence_run_seals_without_registration_events(pipeline):
    result = full_chain(pipeline, persistence=False)
    envelope = json.loads(pipeline.envelope.read_text())
    assert result["seal"][0] == 0
    assert envelope["pipeline"]["outcome"] == "EMPTY_COMPLETE"
    assert "persistence" not in envelope["qualification"]


def test_registered_missing_blocks_seal(pipeline):
    result = full_chain(pipeline, hooks={"verify:GSE123456": lambda _h, _r: "skip"})
    assert result["seal"][0] == 1
    assert "E_REGISTERED_MISSING" in str(result["seal"])


def test_mutation_after_seal_is_refused_and_bytes_unchanged(pipeline):
    full_chain(pipeline, include=["GSE123456"])
    before = pipeline.envelope.read_bytes()
    attempted = pipeline.run_refuse()
    after = pipeline.envelope.read_bytes()
    assert attempted[0] == 1
    assert "E_ENVELOPE_SEALED" in str(attempted)
    assert after == before
@pytest.mark.parametrize("gse_id", ["GSE123456", "GSE123457"])
def test_deferred_fetch_replays_same_gse_and_preserves_run_identity(pipeline, gse_id):
    pipeline.register_soft(gse_id, script=(FakeResponse(status_code=429, headers={"Retry-After": "75"}), FakeResponse(content=soft_gzip(gse_id))))
    pipeline.run_init()
    pipeline.register_query("fixture query", (gse_id,))
    pipeline.run_search()
    pipeline.run_handoff([gse_id])
    assert pipeline.run_fetch(gse_id)[0] == 75
    assert pipeline.run_fetch(gse_id)[0] == 0
    events = [json.loads(line) for line in (pipeline.run_dir / "metadata_fetch.jsonl").read_text().splitlines()]
    assert [event["status"] for event in events] == ["deferred", "fetched", "ok"]
    assert len([call for call in pipeline.fake.calls if call[0] == geo_metadata._get_soft_url(gse_id)]) == 2


def test_404_is_permanent_and_does_not_retry(pipeline):
    pipeline.register_soft("GSE123456", script=(FakeResponse(status_code=404),))
    pipeline.run_init(); pipeline.register_query("fixture query", ("GSE123456",)); pipeline.run_search(); pipeline.run_handoff(["GSE123456"])
    result = pipeline.run_fetch("GSE123456")
    event = json.loads((pipeline.run_dir / "metadata_fetch.jsonl").read_text().splitlines()[-1])
    assert result[0] == 1 and event["error_class"] == "not_found"
    assert len([call for call in pipeline.fake.calls if call[0] == geo_metadata._get_soft_url("GSE123456")]) == 1
    assert not (pipeline.run_dir / "metadata_raw/GSE123456.soft.gz").exists()


def test_midstream_abort_retries_without_partial_artifacts(pipeline):
    pipeline.register_soft("GSE123456", script=(FakeResponse(content=soft_gzip("GSE123456"), stream_error_after=1), FakeResponse(content=soft_gzip("GSE123456"))))
    pipeline.run_init(); pipeline.register_query("fixture query", ("GSE123456",)); pipeline.run_search(); pipeline.run_handoff(["GSE123456"])
    result = pipeline.run_fetch("GSE123456")
    assert result[0] == 0
    assert not list((pipeline.run_dir / "metadata_raw").glob("*.part"))
    assert (pipeline.run_dir / "metadata_raw/GSE123456.soft.gz").exists()


def test_invalid_gzip_reports_failure_and_recovery_retains_raw_evidence(pipeline):
    pipeline.register_soft("GSE123456", script=(FakeResponse(content=b"not-gzip"),))
    pipeline.run_init(); pipeline.register_query("fixture query", ("GSE123456",)); pipeline.run_search(); pipeline.run_handoff(["GSE123456"])
    result = pipeline.run_fetch("GSE123456", max_mb=1)
    assert result[0] == 1 and result[1]["error_class"] == "bad_response"
    assert (pipeline.run_dir / "metadata_raw/GSE123456.soft.gz").exists()
    assert not (pipeline.run_dir / "metadata_parsed/GSE123456.json").exists()


def test_features_mismatch_blocks_register_event(pipeline):
    pipeline.register_soft("GSE123456"); pipeline.run_init(); pipeline.register_query("fixture query", ("GSE123456",)); pipeline.run_search(); pipeline.run_handoff(["GSE123456"])
    pipeline.run_fetch("GSE123456"); pipeline.run_preflight("GSE123456"); pipeline.run_register("GSE123456")
    connection = __import__("duckdb").connect(str(pipeline.db))
    try:
        connection.execute("DELETE FROM geo_sample_characteristics WHERE gsm_id='GSM100001'")
    finally:
        connection.close()
    result = pipeline.run_verify("GSE123456")
    assert result[0] == 1
    assert result[1]["error_class"] == "registration_mismatch"
    assert "features_missing" in result[1]["subcodes"]
    events = [json.loads(line) for line in (pipeline.run_dir / "metadata_fetch.jsonl").read_text().splitlines()]
    assert not any(event["status"] == "registered" for event in events)


def test_registered_event_seal_rejects_foreign_run_and_gsm_mismatch(pipeline):
    full_chain(pipeline)
    log = pipeline.run_dir / "metadata_fetch.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    registered = next(event for event in events if event["status"] == "registered")
    registered["registration"]["expected_gsm_ids"] = ["GSM999999"]
    log.write_text("".join(json.dumps(event) + "\n" for event in events))
    # The event mismatch is validated at seal and remains isolated from DuckDB.
    assert pipeline.run_seal()[0] == 1


def test_mutation_commands_are_all_rejected_after_seal(pipeline):
    full_chain(pipeline, include=["GSE123456"])
    commands = [
        ["build-handoff", str(pipeline.envelope), str(pipeline.run_dir / "handoff.json")],
        ["refuse", str(pipeline.envelope), "--stage", "qualification", "--status", "BLOCKED_UPSTREAM", "--reason", "late"],
        ["resume", str(pipeline.envelope), str(pipeline.run_dir / "invocation.json")],
        ["write-qualification", str(pipeline.envelope), str(pipeline.run_dir / "qualification.json"), "--skill-md", str(__import__("pathlib").Path(__file__).resolve().parents[1] / ".agents/skills/sc-dataset-qualification/SKILL.md")],
        ["seal", str(pipeline.envelope)],
    ]
    for command in commands:
        before = pipeline.envelope.read_bytes()
        result = pipeline.command(geo_envelope.cli, command)
        if command[0] == "seal":
            assert result[0] == 0  # identical re-seal is a no-op
        else:
            if command[0] == "resume":
                command[2] = str(pipeline.run_dir / "invocation.json")
                (pipeline.run_dir / "invocation.json").write_text(json.dumps({"search": json.loads(pipeline.envelope.read_text())["search"]}))
                result = pipeline.command(geo_envelope.cli, command)
            assert result[0] == 1
            assert "E_ENVELOPE_SEALED" in str(result[1])
        assert pipeline.envelope.read_bytes() == before
def test_compressed_limit_breach_is_not_retried_and_publishes_no_artifacts(pipeline):
    pipeline.register_soft("GSE123456", script=(FakeResponse(content=b"x" * (1024 * 1024 + 1)),))
    pipeline.run_init(); pipeline.register_query("fixture query", ("GSE123456",)); pipeline.run_search(); pipeline.run_handoff(["GSE123456"])
    result = pipeline.run_fetch("GSE123456", max_mb=1)
    assert result[0] == 1 and result[1]["error_class"] == "oversized_response"
    event = json.loads((pipeline.run_dir / "metadata_fetch.jsonl").read_text().splitlines()[-1])
    assert event["status"] == "failed" and event["error_class"] == "oversized_response"
    assert len([call for call in pipeline.fake.calls if call[0] == geo_metadata._get_soft_url("GSE123456")]) == 1
    assert not (pipeline.run_dir / "metadata_raw/GSE123456.soft.gz").exists()
    assert not (pipeline.run_dir / "metadata_parsed/GSE123456.json").exists()


def test_foreign_gsm_grant_refuses_cross_gse_registration(pipeline):
    pipeline.register_soft("GSE123456"); pipeline.run_init(); pipeline.register_query("fixture query", ("GSE123456",)); pipeline.run_search(); pipeline.run_handoff(["GSE123456"])
    pipeline.run_fetch("GSE123456"); pipeline.run_preflight("GSE123456")
    grant = str(pipeline.run_dir / "preflight/GSE123456.grant.json")
    before = {table: pipeline.rows(table) for table in ("geo_datasets", "geo_samples", "geo_sample_characteristics")}
    result = pipeline.runner.invoke(__import__("tools.sc_dataset.geo_database", fromlist=["cli"]).cli, ["--db", str(pipeline.db), "save-sample", "GSM100001", "--preflight", grant, "--gse-id", "GSE999999"])
    assert result.exit_code == 1
    assert "grant_gse_mismatch" in result.output
    assert {table: pipeline.rows(table) for table in before} == before


def test_owner_injected_after_preflight_rolls_back_sample_write(pipeline):
    pipeline.register_soft("GSE123456"); pipeline.run_init(); pipeline.register_query("fixture query", ("GSE123456",)); pipeline.run_search(); pipeline.run_handoff(["GSE123456"])
    pipeline.run_fetch("GSE123456"); pipeline.run_preflight("GSE123456")
    conn=__import__("duckdb").connect(str(pipeline.db)); conn.execute("INSERT INTO geo_datasets(gse_id,title) VALUES ('GSE999999','foreign')"); conn.execute("INSERT INTO geo_samples(gsm_id,gse_id) VALUES ('GSM100001','GSE999999')"); conn.close()
    before={table:pipeline.rows(table) for table in ("geo_datasets","geo_samples","geo_sample_characteristics")}
    grant=str(pipeline.run_dir/"preflight/GSE123456.grant.json")
    result=pipeline.runner.invoke(__import__("tools.sc_dataset.geo_database",fromlist=["cli"]).cli,["--db",str(pipeline.db),"save-sample","GSM100001","--preflight",grant,"--gse-id","GSE123456"])
    after={table:pipeline.rows(table) for table in before}
    assert result.exit_code==2 and "ownership_conflict" in result.output
    assert after==before


def test_registered_event_with_foreign_run_id_is_rejected_at_seal(pipeline):
    result=full_chain(pipeline)
    env=json.loads(pipeline.envelope.read_text()); env["sealed_at_utc"]=None; env.pop("pipeline",None); pipeline.envelope.write_text(json.dumps(env,sort_keys=True,separators=(",",":")))
    log=pipeline.run_dir/"metadata_fetch.jsonl"
    events=[json.loads(line) for line in log.read_text().splitlines()]
    for event in events:
        if event["status"]=="registered": event["run_id"]="20261123T120001Z-abcdef02"
    log.write_text("".join(json.dumps(event)+"\n" for event in events))
    sealed=pipeline.run_seal()
    assert sealed[0]==1 and "E_REGISTERED_MISMATCH" in str(sealed[1])
    assert result["verify:GSE123456"][0]==0


def test_registered_event_missing_path_fails_specific_code(pipeline):
    full_chain(pipeline)
    env=json.loads(pipeline.envelope.read_text()); env["sealed_at_utc"]=None; env.pop("pipeline",None); pipeline.envelope.write_text(json.dumps(env,sort_keys=True,separators=(",",":")))
    log=pipeline.run_dir/"metadata_fetch.jsonl"
    events=[json.loads(line) for line in log.read_text().splitlines() if json.loads(line).get("status")!="registered"]
    log.write_text("".join(json.dumps(event)+"\n" for event in events))
    result=pipeline.run_seal()
    assert result[0]==1 and "E_REGISTERED_MISSING" in str(result[1])


def test_criteria_hash_matches_admitted_qualification(pipeline):
    full_chain(pipeline,include=["GSE123456"])
    envelope=json.loads(pipeline.envelope.read_text())
    expected=geo_envelope.compute_fingerprint(geo_envelope.criteria_payload(envelope["qualification"]))
    assert envelope["execution_identity"]["qualification_criteria"]["sha256"]==expected


def test_retry_after_short_is_honored_without_wall_clock_sleep(pipeline):
    pipeline.register_soft("GSE123456",script=(FakeResponse(status_code=429,headers={"Retry-After":"2"}),FakeResponse(content=soft_gzip("GSE123456"))))
    pipeline.run_init();pipeline.register_query("fixture query",("GSE123456",));pipeline.run_search();pipeline.run_handoff(["GSE123456"])
    result=pipeline.run_fetch("GSE123456")
    assert result[0]==0
    assert len([call for call in pipeline.fake.calls if call[0]==geo_metadata._get_soft_url("GSE123456")])==2
def test_streaming_fixture_supports_chunk_abort():
    response = FakeResponse(content=b"abcdef", stream_error_after=2)
    chunks = response.iter_content(2)
    assert next(chunks) == b"ab"
    with pytest.raises(__import__("requests").ConnectionError, match="mid-stream abort"):
        next(chunks)
def test_search_retry_after_short_is_honored(pipeline):
    retry = FakeResponse(status_code=429, headers={"Retry-After": "5"})
    payload = FakeResponse(payload={"esearchresult": {"count": "0", "idlist": []}})
    pipeline.register_query("fixture query", (), search_script=(retry, payload))
    pipeline.run_init()
    result = pipeline.run_search()
    search_calls = [call for call in pipeline.fake.calls if "esearch.fcgi" in call[0]]
    assert result[0] == 0
    assert len(search_calls) == 2
    assert pipeline.run_dir.joinpath("envelope.json").exists()
def test_admission_garbage_blob_is_refused_exit_one_and_db_empty(pipeline):
    pipeline.envelope.write_bytes(b"garbage")
    result = pipeline.run_admit()
    assert result[0] == 1
    assert all(not pipeline.rows(table) for table in ("geo_datasets", "geo_samples", "geo_sample_characteristics"))
def test_partial_success_fetch_failure_is_accounted_and_sealed(pipeline):
    result=full_chain(pipeline,found=("GSE123456","GSE123457"),failed_fetch=("GSE123457",),include=["GSE123456"])
    envelope=json.loads(pipeline.envelope.read_text())
    assert result["fetch:GSE123457"][0]==1
    assert result["seal"][0]==0
    assert envelope["qualification"]["status"]=="PARTIAL"
    assert envelope["qualification"]["failed_gse_ids"]==["GSE123457"]
    assert envelope["pipeline"]["outcome"]=="PARTIAL_SUCCESS"
    assert [row[0] for row in pipeline.rows("geo_datasets")]==["GSE123456"]


def test_persistence_seal_rejects_missing_registration_event(pipeline):
    full_chain(pipeline)
    env=json.loads(pipeline.envelope.read_text()); env["sealed_at_utc"]=None; env.pop("pipeline",None); pipeline.envelope.write_text(json.dumps(env,sort_keys=True,separators=(",",":")))
    (pipeline.run_dir/"metadata_fetch.jsonl").unlink()
    result=pipeline.run_seal()
    assert result[0]==1 and "E_REGISTERED_MISSING" in str(result[1])
def test_retry_after_over_60_defers_metadata_fetch(pipeline):
    pipeline.register_soft("GSE123456",script=(FakeResponse(status_code=429,headers={"Retry-After":"75"}),))
    pipeline.run_init();pipeline.register_query("fixture query",("GSE123456",));pipeline.run_search();pipeline.run_handoff(["GSE123456"])
    result=pipeline.run_fetch("GSE123456")
    event=json.loads((pipeline.run_dir/"metadata_fetch.jsonl").read_text().splitlines()[-1])
    assert result[0]==75 and event["status"]=="deferred"
    assert event["retry_after_seconds"]==75
    assert len([call for call in pipeline.fake.calls if call[0]==geo_metadata._get_soft_url("GSE123456")])==1


def test_search_retry_5xx_then_200_preserves_two_attempt_transport(pipeline):
    pipeline.run_init(); pipeline.register_query("fixture query",(),search_script=(FakeResponse(status_code=503),FakeResponse(payload={"esearchresult":{"count":"0","idlist":[]}})))
    result=pipeline.run_search()
    calls=[call for call in pipeline.fake.calls if "esearch.fcgi" in call[0]]
    assert result[0]==0 and len(calls)==2
    envelope=json.loads(pipeline.envelope.read_text())
    assert envelope["search"]["status"]=="COMPLETE"
    assert envelope["search"]["per_query"][0]["attempt"]==1



def test_refuse_cli_records_failed_search_blocked_upstream_and_seal_fails_closed(tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_envelope import cli, write_envelope_atomic

    env = geo_envelope.envelope_skeleton(
        "20261123T120000Z-abcdef01", "2026-11-23T12:00:00Z",
        {"name": "sc-dataset-search", "version": "0.5.0"}, "geo_search.py", "1.1.0",
        {"queries": [{"query_id": "q1", "query": "broken", "max_results": 10}], "species_constraint": "human"},
        {"retstart_pagination": False},
    )
    env["search"].update({"status": "FAILED", "per_query": [{"query_id": "q1", "status": "failed", "count": None, "retrieved": 0, "attempt": 1, "error": "upstream failed"}], "results": {"unique_gse_count": 0, "gse_ids": [], "dropped_no_gse": 0}, "finished_at_utc": "2026-11-23T12:01:00Z"})
    path = tmp_path / "failed-envelope.json"
    write_envelope_atomic(path, env)
    runner = CliRunner()

    admitted = runner.invoke(cli, ["admit", str(path)])
    assert admitted.exit_code == 1
    refused = runner.invoke(cli, ["refuse", str(path), "--stage", "qualification", "--status", "BLOCKED_UPSTREAM", "--reason", "search failed"])
    assert refused.exit_code == 0, refused.output
    updated = json.loads(path.read_text())
    assert updated["qualification"]["status"] == "BLOCKED_UPSTREAM"
    assert updated.get("sealed_at_utc") is None
    assert geo_envelope.derive_pipeline_outcome(updated) == "FAILURE"
    sealed = runner.invoke(cli, ["seal", str(path)])
    assert sealed.exit_code == 1
    sealed_issues = json.loads(sealed.stdout)["issues"]
    assert any(issue["code"] == "E_HANDOFF_MISSING" for issue in sealed_issues)
    assert json.loads(path.read_text()).get("sealed_at_utc") is None



def test_corrected_skill_step_commands_and_flags_match_real_cli_help():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    envelope = root / ".agents/tools/sc_dataset/geo_envelope.py"
    search = root / ".agents/tools/sc_dataset/geo_search.py"
    metadata = root / ".agents/tools/sc_dataset/geo_metadata.py"
    database = root / ".agents/tools/sc_dataset/geo_database.py"
    steps = [
        ("1 init-run", "geo_envelope.py init-run <envelope.json> --run-id <id> --query <query>", [sys.executable, str(envelope), "init-run", "--help"], ["--run-id", "--query"]),
        ("2 search", "geo_search.py --query <query> --output-envelope <dir>/envelope.json --run-id <id>", [sys.executable, str(search), "--help"], ["--query", "--output-envelope", "--run-id"]),
        ("3 handoff", "geo_envelope.py build-handoff <envelope.json> <handoff.json>", [sys.executable, str(envelope), "build-handoff", "--help"], []),
        ("4 validate", "geo_envelope.py validate <envelope.json> --stage handoff", [sys.executable, str(envelope), "validate", "--help"], ["--stage"]),
        ("4 admit", "geo_envelope.py admit <envelope.json>", [sys.executable, str(envelope), "admit", "--help"], []),
        ("5 qualification", "geo_envelope.py write-qualification <envelope.json> <qualification.json> --skill-md <skill.md>", [sys.executable, str(envelope), "write-qualification", "--help"], ["--skill-md"]),
        ("6 fetch", "geo_metadata.py <GSE> --run-dir <dir> --run-id <id>", [sys.executable, str(metadata), "--help"], ["--run-dir", "--run-id"]),
        ("7 preflight", "geo_metadata.py preflight-registration <dir> <GSE> --db <db> --run-id <id>", [sys.executable, str(metadata), "preflight-registration", "--help"], ["--db", "--run-id"]),
        ("8a dataset", "geo_database.py --db <db> save-dataset <GSE> --preflight <grant>", [sys.executable, str(database), "save-dataset", "--help"], ["--preflight"]),
        ("8b sample", "geo_database.py --db <db> save-sample <GSM> --gse-id <GSE> --preflight <grant>", [sys.executable, str(database), "save-sample", "--help"], ["--gse-id", "--preflight"]),
        ("8c characteristics", "geo_database.py --db <db> save-characteristics <GSM> --gse-id <GSE> --characteristics <json> --preflight <grant>", [sys.executable, str(database), "save-characteristics", "--help"], ["--gse-id", "--characteristics", "--preflight"]),
        ("9 verify", "geo_metadata.py verify-registration <dir> <GSE> --db <db> --run-id <id>", [sys.executable, str(metadata), "verify-registration", "--help"], ["--db", "--run-id"]),
        ("10 registered event check", "geo_envelope.py validate <envelope.json> --stage seal", [sys.executable, str(envelope), "validate", "--help"], ["--stage"]),
        ("11 derive", "geo_envelope.py derive <envelope.json>", [sys.executable, str(envelope), "derive", "--help"], []),
        ("12 seal", "geo_envelope.py seal <envelope.json>", [sys.executable, str(envelope), "seal", "--help"], []),
    ]
    for label, command_line, help_command, flags in steps:
        help_result = subprocess.run(help_command, check=False, capture_output=True, text=True)
        assert help_result.returncode == 0, (label, help_result.stderr)
        for flag in flags:
            assert flag in help_result.stdout, (label, command_line, flag, help_result.stdout)
    validate_help = subprocess.run(
        [sys.executable, str(envelope), "validate", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validate_help.returncode == 0
    assert "seal" in validate_help.stdout
    root_help = subprocess.run([sys.executable, str(database), "--help"], check=False, capture_output=True, text=True)
    assert root_help.returncode == 0
    assert "--db PATH" in root_help.stdout
    assert len({step[0].split()[0] for step in steps}) >= 12


def test_twelve_step_cli_smoke_run_uses_real_commands_and_temporary_state(pipeline):
    from tools.sc_dataset import geo_database

    gse_id = "GSE123456"
    gsm_id = "GSM100001"
    grant = pipeline.run_dir / "preflight" / f"{gse_id}.grant.json"

    assert pipeline.run_init()[0] == 0
    pipeline.register_query("fixture query", (gse_id,))
    assert pipeline.run_search()[0] == 0
    assert pipeline.run_handoff([gse_id])[0] == 0
    assert pipeline.command(geo_envelope.cli, ["validate", str(pipeline.envelope), "--stage", "handoff"])[0] == 0
    assert pipeline.run_admit()[0] == 0

    pipeline.register_soft(gse_id)
    assert pipeline.run_fetch(gse_id)[0] == 0
    qual = make_qualification(
        [gse_id], include=[gse_id], persistence=True,
        report=str(pipeline.run_dir / "metadata_fetch.jsonl"),
    )
    assert pipeline.run_write_qual(qual)[0] == 0

    preflight = pipeline.runner.invoke(
        geo_metadata.preflight_registration,
        [str(pipeline.run_dir), gse_id, "--db", str(pipeline.db), "--run-id", RUN_ID],
    )
    assert preflight.exit_code == 0, preflight.output
    assert json.loads(grant.read_text())["run_id"] == RUN_ID

    dataset = pipeline.runner.invoke(
        geo_database.cli,
        ["--db", str(pipeline.db), "save-dataset", gse_id, "--preflight", str(grant), "--title", "Fixture", "--organism", "Homo sapiens", "--n-samples", "1"],
    )
    sample = pipeline.runner.invoke(
        geo_database.cli,
        ["--db", str(pipeline.db), "save-sample", gsm_id, "--gse-id", gse_id, "--preflight", str(grant), "--title", "Fixture sample", "--organism", "Homo sapiens"],
    )
    characteristics = pipeline.runner.invoke(
        geo_database.cli,
        ["--db", str(pipeline.db), "save-characteristics", gsm_id, "--gse-id", gse_id, "--preflight", str(grant), "--characteristics", '{"disease":"Crohn","tissue":"colon"}'],
    )
    assert dataset.exit_code == 0, dataset.output
    assert sample.exit_code == 0, sample.output
    assert characteristics.exit_code == 0, characteristics.output

    verify = pipeline.runner.invoke(
        geo_metadata.verify_registration,
        [str(pipeline.run_dir), gse_id, "--db", str(pipeline.db), "--run-id", RUN_ID],
    )
    assert verify.exit_code == 0, verify.output
    assert pipeline.command(geo_envelope.cli, ["validate", str(pipeline.envelope), "--stage", "seal"])[0] == 0
    assert pipeline.run_derive()[0] == 0
    sealed = pipeline.run_seal()
    assert sealed[0] == 0, sealed[1]
    assert json.loads(pipeline.envelope.read_text())["pipeline"]["outcome"] == "SUCCESS"
