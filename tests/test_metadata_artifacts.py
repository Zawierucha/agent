import gzip
import json
from unittest.mock import Mock

import pytest
import requests
from tools.sc_dataset import geo_metadata as metadata

get_geo_metadata = metadata.get_geo_metadata


def _response(content=b"^SERIES = GSE123456\n^SAMPLE = GSM1\n!Sample_characteristics_ch1 = tissue: colon\n"):
    response = Mock()
    response.content = gzip.compress(content)
    response.raise_for_status.return_value = None
    response.headers = {}
    response.status_code = 200
    return response


def test_timeout_retries_then_success_uses_equal_jitter(monkeypatch):
    good = _response()
    get = Mock(side_effect=[requests.Timeout("slow"), good])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    delays = []
    class RNG:
        def uniform(self, low, high):
            assert (low, high) == (0, 0.5)
            return 0.25
    result = get_geo_metadata("GSE123456", sleep=delays.append, rng=RNG())
    assert result["samples"][0]["gsm_id"] == "GSM1"
    assert get.call_count == 2
    assert delays == [0.75]


def test_retry_after_short_is_honored(monkeypatch):
    rate = Mock()
    rate.status_code = 429
    rate.headers = {"Retry-After": "2"}
    error = requests.HTTPError("429", response=rate)
    good = _response()
    get = Mock(side_effect=[error, good])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    delays = []
    get_geo_metadata("GSE123456", sleep=delays.append)
    assert delays == [2]


def test_retry_after_over_budget_defers(monkeypatch):
    rate = Mock()
    rate.status_code = 429
    rate.headers = {"Retry-After": "999"}
    error = requests.HTTPError("429", response=rate)
    get = Mock(side_effect=error)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(metadata.DeferredFetch) as caught:
        get_geo_metadata("GSE123456", sleep=lambda _: pytest.fail("must not sleep"))
    assert caught.value.retry_after_seconds == 999
    assert caught.value.attempts_so_far == 1


def test_403_budget_exhaustion_is_labeled(monkeypatch):
    response = Mock()
    response.status_code = 403
    response.headers = {}
    error = requests.HTTPError("403 forbidden", response=response)
    get = Mock(side_effect=error)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(requests.HTTPError, match="forbidden_exhausted"):
        get_geo_metadata("GSE123456", max_attempts=4, sleep=lambda _: None)
    assert get.call_count == 4


def test_404_is_not_retried(monkeypatch):
    response = Mock()
    response.status_code = 404
    error = requests.HTTPError("404 missing", response=response)
    get = Mock(side_effect=error)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(requests.HTTPError):
        get_geo_metadata("GSE123456", sleep=lambda _: None)
    assert get.call_count == 1


def test_bad_gzip_is_permanent_without_retry(monkeypatch):
    bad = Mock(content=b"bad gzip", status_code=200, headers={})
    bad.raise_for_status.return_value = None
    get = Mock(return_value=bad)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(ValueError, match="GEO SOFT response for GSE123456 was not a valid gzip file") as caught:
        get_geo_metadata("GSE123456", sleep=lambda _: pytest.fail("must not sleep"))
    assert caught.value.error_class == "bad_response"
    get.assert_called_once()


def test_library_success_shape_and_one_request(monkeypatch):
    get = Mock(return_value=_response())
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    assert set(get_geo_metadata("GSE123456")) == {"gse", "samples"}
    get.assert_called_once()


def test_run_dir_fetch_writes_raw_parsed_and_jsonl(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main

    get = Mock(return_value=_response())
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    run_dir = tmp_path / "run"
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(run_dir)])
    assert result.exit_code == 0, result.output
    raw = run_dir / "metadata_raw/GSE123456.soft.gz"
    parsed = run_dir / "metadata/GSE123456.json"
    assert gzip.decompress(raw.read_bytes()).startswith(b"^SERIES")
    assert json.loads(parsed.read_text())["parser_contract"] == "soft-parser-v1"
    events = [json.loads(line) for line in (run_dir / "metadata_fetch.jsonl").read_text().splitlines()]
    assert [event["status"] for event in events] == ["fetched", "ok"]
    assert events[-1]["raw_sha256"] and events[-1]["parsed_sha256"]


def test_recover_reparses_valid_raw_without_network(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import recover

    run_dir = tmp_path / "run"
    raw_path = run_dir / "metadata_raw/GSE123456.soft.gz"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(_response().content)
    get = Mock(side_effect=AssertionError("network forbidden during recover"))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    assert (run_dir / "metadata/GSE123456.json").exists()
    events = [json.loads(line) for line in (run_dir / "metadata_fetch.jsonl").read_text().splitlines()]
    assert [event["status"] for event in events] == ["fetched", "ok"]
    assert all(event["origin"] == "recovered_orphan" for event in events)
    get.assert_not_called()


def test_event_log_tolerates_torn_last_line_but_not_middle(tmp_path):
    from tools.sc_dataset.geo_metadata import _read_events

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log = run_dir / "metadata_fetch.jsonl"
    log.write_text('{\"gse_id\":\"GSE1\",\"seq\":1,\"status\":\"ok\"}\n{bad')
    assert _read_events(run_dir) == [{"gse_id": "GSE1", "seq": 1, "status": "ok"}]
    log.write_text('{bad\n{\"gse_id\":\"GSE1\",\"seq\":2,\"status\":\"ok\"}\n')
    with pytest.raises(ValueError, match="corrupt metadata event"):
        _read_events(run_dir)


def _write_parsed_run(monkeypatch, run_dir):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=_response()))
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(run_dir)])
    assert result.exit_code == 0, result.output


def test_preflight_rejects_foreign_gsm_without_database_writes(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import save_dataset, save_sample
    from tools.sc_dataset.geo_metadata import preflight_registration

    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE999999", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE999999", None, None, None, None, None, {}, None, None, None, None, None)
    before = connection.execute("SELECT gsm_id,gse_id FROM geo_samples").fetchall()
    connection.close()
    result = CliRunner().invoke(preflight_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 2
    assert json.loads(result.output)["error_class"] == "ownership_conflict"
    connection = duckdb.connect(str(geo_db), read_only=True)
    assert connection.execute("SELECT gsm_id,gse_id FROM geo_samples").fetchall() == before
    connection.close()


def test_preflight_allows_absent_samples(monkeypatch, geo_db, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import preflight_registration
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    result = CliRunner().invoke(preflight_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 0, result.output


def test_verify_registration_full_sample_and_characteristics_equality(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import (
        save_characteristics,
        save_dataset,
        save_sample,
    )
    from tools.sc_dataset.geo_metadata import verify_registration

    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
    save_characteristics(connection, "GSM1", {})
    connection.close()
    result = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 1
    assert json.loads(result.output)["error_class"] == "registration_mismatch"
    assert "features_missing" in json.loads(result.output)["subcodes"]


def test_cli_retry_after_long_exits_75_and_records_deferred(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    response = Mock(status_code=429, headers={"Retry-After": "999"})
    response.raise_for_status.side_effect = requests.HTTPError("429", response=response)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=response))
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code == 75, (result.output, repr(result.exception))
    assert json.loads(result.output)["error_class"] == "deferred_fetch"
    event = json.loads((tmp_path / "run/metadata_fetch.jsonl").read_text().splitlines()[-1])
    assert event["status"] == "deferred"
    assert event["retry_after_seconds"] == 999


def test_verify_reports_characteristic_value_mismatch(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import (
        save_characteristics,
        save_dataset,
        save_sample,
    )
    from tools.sc_dataset.geo_metadata import verify_registration
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
    save_characteristics(connection, "GSM1", {"tissue": "rectum"})
    connection.close()
    result = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 1
    subcodes = json.loads(result.output)["subcodes"]
    assert subcodes["features_value_mismatch"] == [["GSM1", "tissue", "colon", "rectum"]]
    assert "features_missing" not in subcodes and "features_extra" not in subcodes


def test_verify_registers_only_after_full_match(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import (
        save_characteristics,
        save_dataset,
        save_sample,
    )
    from tools.sc_dataset.geo_metadata import verify_registration
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
    save_characteristics(connection, "GSM1", {"tissue": "colon"})
    connection.close()
    result = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in (run_dir / "metadata_fetch.jsonl").read_text().splitlines()]
    assert events[-1]["status"] == "registered"


def test_event_duplicate_sequence_is_rejected(tmp_path):
    from tools.sc_dataset.geo_metadata import _read_events
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "metadata_fetch.jsonl").write_text(
        '{\"gse_id\":\"GSE1\",\"seq\":1,\"status\":\"ok\"}\n{\"gse_id\":\"GSE1\",\"seq\":1,\"status\":\"ok\"}\n'
    )
    with pytest.raises(ValueError, match="duplicate seq"):
        _read_events(run_dir)


def test_recover_repairs_corrupt_parsed_from_valid_raw_without_http(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import recover
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    parsed = run_dir / "metadata/GSE123456.json"
    parsed.write_text("not json")
    network = Mock(side_effect=AssertionError("recovery must not issue HTTP"))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", network)
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    assert json.loads(parsed.read_text())["parser_contract"] == "soft-parser-v1"
    network.assert_not_called()


def test_recover_reports_corrupt_raw_and_retains_file(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import recover
    run_dir = tmp_path / "run"
    raw = run_dir / "metadata_raw/GSE123456.soft.gz"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"corrupt gzip")
    network = Mock(side_effect=AssertionError("network forbidden during recover"))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", network)
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    assert raw.exists()
    events = [json.loads(line) for line in (run_dir / "metadata_fetch.jsonl").read_text().splitlines()]
    assert events[-1]["status"] == "failed"
    assert events[-1]["error_class"] == "corrupt_raw"
    network.assert_not_called()


def test_parser_contract_upgrade_reparses_without_http(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset import geo_metadata
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    parsed = run_dir / "metadata/GSE123456.json"
    document = json.loads(parsed.read_text())
    document["parser_contract"] = "old-contract"
    parsed.write_text(json.dumps(document))
    network = Mock(side_effect=AssertionError("recovery must not issue HTTP"))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", network)
    result = CliRunner().invoke(geo_metadata.recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    assert json.loads(parsed.read_text())["parser_contract"] == geo_metadata.PARSER_CONTRACT
    log = run_dir / "metadata_fetch.jsonl"
    assert any(event.get("origin") == "parser_upgrade" for event in map(json.loads, log.read_text().splitlines()))
    network.assert_not_called()


def test_verify_reports_extra_sample_and_extra_characteristic(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import (
        save_characteristics,
        save_dataset,
        save_sample,
    )
    from tools.sc_dataset.geo_metadata import verify_registration
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 2)
    for gsm in ("GSM1", "GSM2"):
        save_sample(connection, gsm, "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
        save_characteristics(connection, gsm, {"tissue": "colon"} if gsm == "GSM1" else {"extra": "x"})
    connection.close()
    result = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 1
    subcodes = json.loads(result.output)["subcodes"]
    assert "samples_extra" in subcodes and "features_extra" in subcodes
    assert "registered" not in [event.get("status") for event in map(json.loads, (run_dir / "metadata_fetch.jsonl").read_text().splitlines())]


def test_run_id_fallback_writes_artifacts(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=_response()))
    run_dir = tmp_path / "fallback"
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(run_dir)])
    assert result.exit_code == 0, result.output
    event = json.loads((run_dir / "metadata_fetch.jsonl").read_text().splitlines()[0])
    assert event["run_id"] == "fallback"


def test_recover_respects_highest_event_seq_and_skips_consistent_artifacts(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import recover
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    log = run_dir / "metadata_fetch.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    latest = dict(events[-1])
    latest.update(seq=events[-1]["seq"] + 1, status="failed", error_class="test")
    with log.open("a") as stream:
        stream.write(json.dumps(latest) + "\n")
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(side_effect=AssertionError("no network")))
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["GSE123456"] == "recovered"
    assert len(log.read_text().splitlines()) == 4
    again = CliRunner().invoke(recover, [str(run_dir)])
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["GSE123456"] == "consistent"
    assert len(log.read_text().splitlines()) == 4


def test_verify_detects_foreign_owner_after_write_attempt(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import save_dataset, save_sample
    from tools.sc_dataset.geo_metadata import verify_registration
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSEOTHER", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSEOTHER", None, None, None, None, None, {}, None, None, None, None, None)
    connection.close()
    result = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["error_class"] == "registration_mismatch"
    assert "ownership_conflict" in report["subcodes"]


def test_preflight_allows_same_gse_partial_rows(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import save_dataset, save_sample
    from tools.sc_dataset.geo_metadata import preflight_registration
    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
    connection.close()
    result = CliRunner().invoke(preflight_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 0, result.output


def test_cli_default_mode_stdout_remains_plain_metadata_json(monkeypatch):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=_response()))
    result = CliRunner().invoke(main, ["GSE123456"])
    assert result.exit_code == 0
    assert set(json.loads(result.output)) == {"gse", "samples"}
    assert "parser_contract" not in result.output


def test_truncated_soft_retries_but_unexpected_empty_is_not_skipped(monkeypatch):
    truncated = _response(b"^SERIES = GSE123456\n!Series_sample_id = GSM1\n")
    good = _response()
    get = Mock(side_effect=[truncated, good])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    assert get_geo_metadata("GSE123456", sleep=lambda _: None)["samples"]
    assert get.call_count == 2


def test_unexpected_empty_soft_is_permanent_and_not_auto_skipped(monkeypatch):
    response = _response(b"^SERIES = GSE123456\n")
    get = Mock(return_value=response)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(ValueError) as caught:
        get_geo_metadata("GSE123456", sleep=lambda _: None)
    assert caught.value.error_class == "no_samples_unexpected"
    get.assert_called_once()


def test_retry_after_over_60_on_true_403_still_defers(monkeypatch):
    response = Mock(status_code=403, headers={"Retry-After": "90"})
    error = requests.HTTPError("403 forbidden", response=response)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(side_effect=error))
    with pytest.raises(metadata.DeferredFetch):
        get_geo_metadata("GSE123456", max_attempts=1, sleep=lambda _: None)


def test_run_id_without_run_dir_keeps_default_stdout_and_no_artifacts(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=_response()))
    result = CliRunner().invoke(main, ["GSE123456", "--run-id", str(tmp_path / "id-only")])
    assert result.exit_code == 0
    assert set(json.loads(result.output)) == {"gse", "samples"}
    assert not (tmp_path / "id-only/metadata_fetch.jsonl").exists()


def test_403_long_retry_after_defers_before_forbidden_exhaustion(monkeypatch):
    response = Mock(status_code=403, headers={"Retry-After": "90"})
    error = requests.HTTPError("403", response=response)
    get = Mock(side_effect=error)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(metadata.DeferredFetch):
        get_geo_metadata("GSE123456", max_attempts=1, sleep=lambda _: None)


def test_run_dir_403_exhaustion_records_attempt_count_and_class(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    response = Mock(status_code=403, headers={})
    error = requests.HTTPError("forbidden", response=response)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(side_effect=error))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", lambda _: None)
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code != 0
    event = json.loads((tmp_path / "run/metadata_fetch.jsonl").read_text().splitlines()[-1])
    assert event["error_class"] == "forbidden_exhausted"
    assert event["attempts"] == 4


def test_run_dir_403_retry_after_long_defers(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    response = Mock(status_code=403, headers={"Retry-After": "999"})
    error = requests.HTTPError("forbidden", response=response)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(side_effect=error))
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code == 75


def test_recover_appends_highest_sequence_after_valid_reparse(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import recover
    run_dir = tmp_path / "run"
    raw = run_dir / "metadata_raw/GSE123456.soft.gz"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(_response().content)
    network = Mock(side_effect=AssertionError("network forbidden during recover"))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", network)
    parsed = run_dir / "metadata/GSE123456.json"
    parsed.parent.mkdir(parents=True)
    parsed.write_text("bad json")
    log = run_dir / "metadata_fetch.jsonl"
    log.write_text(json.dumps({"run_id": "r", "gse_id": "GSE123456", "seq": 7, "status": "fetched"}) + "\n")
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [event["seq"] for event in events] == [7, 8]
    assert events[-1]["origin"] == "legacy_contract_unknown"
    assert events[-1]["status"] == "ok"
    assert json.loads(parsed.read_text())["parser_contract"] == "soft-parser-v1"
    network.assert_not_called()


def test_final_403_with_retry_after_over_60_defers_in_run_mode(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    errors = []
    for status, retry_after in [(429, None), (429, None), (429, None), (403, "999")]:
        response = Mock(status_code=status, headers={"Retry-After": retry_after} if retry_after else {})
        error = requests.HTTPError(str(status), response=response)
        errors.append(error)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(side_effect=errors))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", lambda _: None)
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code == 75


def test_registration_uses_logged_parsed_artifact_after_hash_verification(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import (
        save_characteristics,
        save_dataset,
        save_sample,
    )
    from tools.sc_dataset.geo_metadata import verify_registration

    runner = CliRunner()
    run_dir = tmp_path / "parsed-tamper"
    _write_parsed_run(monkeypatch, run_dir)
    parsed_path = run_dir / "metadata/GSE123456.json"
    artifact = json.loads(parsed_path.read_text())
    artifact["samples"][0]["characteristics"]["tissue"] = "rectum"
    parsed_path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
    save_characteristics(connection, "GSM1", {"tissue": "colon"})
    connection.close()

    parsed_result = runner.invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert parsed_result.exit_code == 0, parsed_result.output
    assert json.loads(parsed_path.read_text())["samples"][0]["characteristics"]["tissue"] == "colon"

    raw_tamper_run = tmp_path / "raw-tamper"
    _write_parsed_run(monkeypatch, raw_tamper_run)
    raw_path = raw_tamper_run / "metadata_raw/GSE123456.soft.gz"
    raw_path.write_bytes(_response(b"corrupt raw bytes").content)
    raw_result = runner.invoke(verify_registration, [str(raw_tamper_run), "GSE123456", "--db", str(geo_db)])
    assert raw_result.exit_code == 1
    report = json.loads(raw_result.output)
    assert report["subcodes"]["verification_error"] == "raw_sha_mismatch"


def test_run_dir_truncated_soft_exhaustion_records_failed_event(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main
    response = _response(b"^SERIES = GSE123456\n!Series_sample_id = GSM1\n")
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=response))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", lambda _: None)
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code != 0
    events = [json.loads(line) for line in (tmp_path / "run/metadata_fetch.jsonl").read_text().splitlines()]
    assert events[-1]["status"] == "failed"
    assert events[-1]["error_class"] == "truncated_soft"



def test_registration_commands_return_json_for_invalid_event_identity(monkeypatch, geo_db, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import (
        preflight_registration,
        recover,
        verify_registration,
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    raw = run_dir / "metadata_raw/GSE123456.soft.gz"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(_response().content)
    (run_dir / "metadata_fetch.jsonl").write_text(
        json.dumps({"gse_id": "GSE123456", "seq": "bad", "status": "ok"}) + "\n"
    )
    runner = CliRunner()
    for command, args in (
        (recover, [str(run_dir)]),
        (preflight_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)]),
        (verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)]),
    ):
        result = runner.invoke(command, args)
        assert result.exit_code == 1, result.output
        assert "invalid metadata event identity" in result.output
        assert "Traceback" not in result.output


def test_fetch_error_is_not_masked_by_failed_event_append(monkeypatch, tmp_path):
    from click.testing import CliRunner
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", lambda _: None)
    from tools.sc_dataset.geo_metadata import main

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get",
        Mock(side_effect=requests.ConnectionError("original fetch failure")),
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata._append_event",
        Mock(side_effect=OSError("event log unavailable")),
    )
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code == 1
    assert "original fetch failure" in result.output
    assert "event log unavailable" not in result.output
    assert "Traceback" not in result.output


def test_empty_soft_error_classes_survive_retry_and_recovery(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import recover

    truncated = _response(b"^SERIES = GSE123456\n!Series_sample_id = GSM1\n")
    get = Mock(return_value=truncated)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(EOFError) as caught:
        get_geo_metadata("GSE123456", max_attempts=1, sleep=lambda _: None)
    assert caught.value.error_class == "truncated_soft"

    run_dir = tmp_path / "run"
    raw = run_dir / "metadata_raw/GSE123456.soft.gz"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(truncated.content)
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    event = json.loads((run_dir / "metadata_fetch.jsonl").read_text().splitlines()[-1])
    assert event["error_class"] == "truncated_soft"
    assert json.loads(result.output)["GSE123456"] == "truncated_soft"


def test_registration_commands_reject_invalid_gse_without_path_interpolation(geo_db, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import (
        preflight_registration,
        recover,
        verify_registration,
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runner = CliRunner()
    for command, args in (
        (recover, [str(run_dir), "--gse", "../GSE123456"]),
        (recover, [str(run_dir), "--gse", "gse123456"]),
        (preflight_registration, [str(run_dir), "../GSE123456", "--db", str(geo_db)]),
        (preflight_registration, [str(run_dir), "gse123456", "--db", str(geo_db)]),
        (verify_registration, [str(run_dir), "../GSE123456", "--db", str(geo_db)]),
        (verify_registration, [str(run_dir), "gse123456", "--db", str(geo_db)]),
    ):
        result = runner.invoke(command, args)
        assert result.exit_code == 1, result.output
        assert "Invalid GEO Series accession" in result.output
        assert "Traceback" not in result.output
    assert list(run_dir.iterdir()) == []


def test_main_rejects_bad_accession_without_event_log(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main

    result = CliRunner().invoke(main, ["../GSE1", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code == 1
    assert not (tmp_path / "run" / "metadata_fetch.jsonl").exists()
    assert "Traceback" not in result.output


def test_preflight_artifact_check_is_read_only_and_advises_recovery(monkeypatch, geo_db, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import preflight_registration

    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    parsed = run_dir / "metadata/GSE123456.json"
    parsed.write_text("not json")
    before = (run_dir / "metadata_fetch.jsonl").read_bytes()
    result = CliRunner().invoke(
        preflight_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)]
    )
    assert result.exit_code == 1
    assert "recover" in result.output
    assert parsed.read_text() == "not json"
    assert (run_dir / "metadata_fetch.jsonl").read_bytes() == before


def test_registration_commands_report_missing_paths_as_json(geo_db, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import (
        preflight_registration,
        recover,
        verify_registration,
    )

    runner = CliRunner()
    absent_run = tmp_path / "absent-run"
    for command, args in (
        (recover, [str(absent_run)]),
        (preflight_registration, [str(absent_run), "GSE123456", "--db", str(geo_db)]),
        (verify_registration, [str(absent_run), "GSE123456", "--db", str(geo_db)]),
        (preflight_registration, [str(tmp_path), "GSE123456", "--db", str(tmp_path / "absent.db")]),
        (verify_registration, [str(tmp_path), "GSE123456", "--db", str(tmp_path / "absent.db")]),
    ):
        result = runner.invoke(command, args)
        assert result.exit_code == 1, result.output
        assert result.output.lstrip().startswith("{")
        assert "Traceback" not in result.output


def test_run_dir_parse_retry_uses_injected_equal_jitter(monkeypatch, tmp_path):
    from tools.sc_dataset.geo_metadata import _fetch_and_record

    truncated = Mock(content=gzip.compress(b"^SERIES = GSE123456\n^SAMPLE = GSM1\n")[:-4], status_code=200, headers={})
    truncated.raise_for_status.return_value = None
    good = _response()
    get = Mock(side_effect=[truncated, good])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    delays = []

    class RNG:
        def uniform(self, low, high):
            assert (low, high) == (0, 0.5)
            return 0.25

    parsed = _fetch_and_record(tmp_path / "run", "GSE123456", 1.0, sleep=delays.append, rng=RNG())
    assert parsed["samples"][0]["gsm_id"] == "GSM1"
    assert delays == [0.75]


def test_ok_events_record_parser_contract_and_recover_origins(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import PARSER_CONTRACT, recover

    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    log = run_dir / "metadata_fetch.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert events[-1]["parser_contract"] == PARSER_CONTRACT
    (run_dir / "metadata/GSE123456.json").unlink()
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["GSE123456"] == "recovered"
    recovered = json.loads(log.read_text().splitlines()[-1])
    assert recovered["origin"] == "recovered"
    assert recovered["parser_contract"] == PARSER_CONTRACT


def test_recover_classifies_missing_parser_contract_as_legacy(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import recover

    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    log = run_dir / "metadata_fetch.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    for event in events:
        event.pop("parser_contract", None)
    log.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    (run_dir / "metadata/GSE123456.json").unlink()
    result = CliRunner().invoke(recover, [str(run_dir)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["GSE123456"] == "legacy_contract_unknown"
    assert json.loads(log.read_text().splitlines()[-1])["origin"] == "legacy_contract_unknown"


def test_preflight_rejects_parsed_hash_mismatch_without_mutation(monkeypatch, geo_db, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import preflight_registration

    run_dir = tmp_path / "run"
    _write_parsed_run(monkeypatch, run_dir)
    parsed = run_dir / "metadata/GSE123456.json"
    doc = json.loads(parsed.read_text())
    doc["samples"][0]["characteristics"]["tissue"] = "rectum"
    parsed.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    log = run_dir / "metadata_fetch.jsonl"
    before_log = log.read_bytes()
    result = CliRunner().invoke(
        preflight_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)]
    )
    assert result.exit_code == 1
    assert "recover" in result.output
    assert json.loads(parsed.read_text())["samples"][0]["characteristics"]["tissue"] == "rectum"
    assert log.read_bytes() == before_log


def test_list_characteristic_round_trips_through_registration(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import (
        save_characteristics,
        save_dataset,
        save_sample,
    )
    from tools.sc_dataset.geo_metadata import verify_registration

    run_dir = tmp_path / "run"
    soft = (
        b"^SERIES = GSE123456\n^SAMPLE = GSM1\n"
        b"!Sample_characteristics_ch1 = tissue: colon\n"
        b"!Sample_characteristics_ch1 = tissue: rectum\n"
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=_response(soft))
    )
    from tools.sc_dataset.geo_metadata import main
    fetched = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(run_dir)])
    assert fetched.exit_code == 0, fetched.output
    artifact = json.loads((run_dir / "metadata/GSE123456.json").read_text())
    assert artifact["samples"][0]["characteristics"]["tissue"] == ["colon", "rectum"]

    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
    save_characteristics(connection, "GSM1", {"tissue": ["colon", "rectum"]})
    row = connection.execute(
        "SELECT value FROM geo_sample_characteristics WHERE gsm_id='GSM1' AND characteristic='tissue'"
    ).fetchone()
    assert row == (json.dumps(["colon", "rectum"], ensure_ascii=False),)
    connection.close()

    verified = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert verified.exit_code == 0, verified.output


def test_registration_detects_serialization_difference_for_list_characteristic(monkeypatch, geo_db, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_database import (
        save_characteristics,
        save_dataset,
        save_sample,
    )
    from tools.sc_dataset.geo_metadata import main, verify_registration

    run_dir = tmp_path / "run"
    soft = (
        b"^SERIES = GSE123456\n^SAMPLE = GSM1\n"
        b"!Sample_characteristics_ch1 = tissue: colon\n"
        b"!Sample_characteristics_ch1 = tissue: rectum\n"
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=_response(soft))
    )
    fetched = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(run_dir)])
    assert fetched.exit_code == 0, fetched.output
    connection = duckdb.connect(str(geo_db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {}, None, None, None, None, None)
    save_characteristics(connection, "GSM1", {"tissue": ' ["colon", "rectum"]'})
    connection.close()
    result = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(geo_db)])
    assert result.exit_code == 1, result.output
    assert "features_value_mismatch" in json.loads(result.output)["subcodes"]


def test_verify_reports_orphaned_database_characteristics_as_extra(monkeypatch, tmp_path):
    import duckdb
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main, verify_registration

    run_dir = tmp_path / "run"
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=_response()))
    fetched = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(run_dir)])
    assert fetched.exit_code == 0, fetched.output

    db_path = tmp_path / "orphaned.duckdb"
    connection = duckdb.connect(str(db_path))
    connection.execute("CREATE TABLE geo_samples (gsm_id VARCHAR, gse_id VARCHAR)")
    connection.execute(
        "CREATE TABLE geo_sample_characteristics (gsm_id VARCHAR, characteristic VARCHAR, value VARCHAR)"
    )
    connection.execute("CREATE TABLE geo_sample_metadata (gsm_id VARCHAR)")
    connection.execute(
        "INSERT INTO geo_sample_characteristics VALUES ('GSM1', 'tissue', 'colon'), ('GSM1', 'extra', 'ghost')"
    )
    connection.close()

    result = CliRunner().invoke(verify_registration, [str(run_dir), "GSE123456", "--db", str(db_path)])
    assert result.exit_code == 1, result.output
    subcodes = json.loads(result.output)["subcodes"]
    assert ["GSM1", "extra", "ghost"] in subcodes["features_extra"]


def test_run_dir_bad_gzip_is_permanent_without_retry(monkeypatch, tmp_path):
    from click.testing import CliRunner
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", lambda _: None)
    from tools.sc_dataset.geo_metadata import main

    bad = Mock(content=b"bad gzip", status_code=200, headers={})
    bad.raise_for_status.return_value = None
    get = Mock(return_value=bad)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / "run")])
    assert result.exit_code == 1
    assert "GEO SOFT response for GSE123456 was not a valid gzip file" in result.output
    events = [json.loads(line) for line in (tmp_path / "run/metadata_fetch.jsonl").read_text().splitlines()]
    assert events[-1]["error_class"] == "bad_response"
    assert events[-1]["attempts"] == 1
    get.assert_called_once()


def test_http_400_and_401_are_permanent_in_both_fetch_paths(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from tools.sc_dataset.geo_metadata import main

    for status in (400, 401):
        response = Mock(status_code=status, headers={})
        error = requests.HTTPError(str(status), response=response)
        get = Mock(side_effect=error)
        monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
        with pytest.raises(requests.HTTPError) as caught:
            get_geo_metadata("GSE123456", sleep=lambda _: pytest.fail("must not sleep"))
        assert caught.value.error_class == "permanent_http"
        get.assert_called_once()

        get.reset_mock()
        result = CliRunner().invoke(main, ["GSE123456", "--run-dir", str(tmp_path / str(status))])
        assert result.exit_code == 1
        events = [json.loads(line) for line in (tmp_path / str(status) / "metadata_fetch.jsonl").read_text().splitlines()]
        assert events[-1]["error_class"] == "permanent_http"
        assert events[-1]["attempts"] == 1
        get.assert_called_once()


def test_retry_after_http_date_is_honored(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    retry_at = datetime.now(UTC) + timedelta(seconds=12)
    header = format_datetime(retry_at, usegmt=True)
    expected = metadata._retry_after_seconds(header)
    response = Mock(status_code=429, headers={"Retry-After": header})
    error = requests.HTTPError("429", response=response)
    get = Mock(side_effect=[error, _response()])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    delays = []
    get_geo_metadata("GSE123456", sleep=delays.append)
    assert delays == pytest.approx([expected], abs=0.1)


def test_run_dir_http_date_retry_after_is_honored(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from tools.sc_dataset.geo_metadata import _fetch_and_record

    retry_at = datetime.now(UTC) + timedelta(seconds=12)
    header = format_datetime(retry_at, usegmt=True)
    expected = metadata._retry_after_seconds(header)
    response = Mock(status_code=429, headers={"Retry-After": header})
    error = requests.HTTPError("429", response=response)
    get = Mock(side_effect=[error, _response()])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    delays = []
    _fetch_and_record(tmp_path / "run", "GSE123456", 1, sleep=delays.append)
    assert delays == pytest.approx([expected], abs=0.1)


def test_fetch_defaults_resolve_sleep_after_monkeypatch(monkeypatch):
    delays = []
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", delays.append)
    response = Mock(status_code=429, headers={})
    error = requests.HTTPError("429", response=response)
    get = Mock(side_effect=[error, _response()])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    get_geo_metadata("GSE123456")
    assert len(delays) == 1
    assert 0.5 <= delays[0] <= 1


def test_run_dir_respects_configured_max_attempts(monkeypatch, tmp_path):
    from tools.sc_dataset.geo_metadata import _fetch_and_record

    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", lambda _: None)
    get = Mock(side_effect=requests.Timeout("slow"))
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    with pytest.raises(requests.Timeout):
        _fetch_and_record(tmp_path / "run", "GSE123456", 1, max_attempts=2, sleep=lambda _: None)
    assert get.call_count == 2


def test_event_log_repair_uses_atomic_write(monkeypatch, tmp_path):
    from tools.sc_dataset.geo_metadata import _append_event

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log = run_dir / "metadata_fetch.jsonl"
    log.write_bytes(b"{\"gse_id\":\"GSE123456\",\"seq\":1,\"status\":\"ok\"}\n{torn")
    actual = __import__("tools.sc_dataset.geo_metadata", fromlist=["_atomic_write"])._atomic_write
    writes = []
    def capture(path, data):
        writes.append((path, data))
        actual(path, data)
    monkeypatch.setattr("tools.sc_dataset.geo_metadata._atomic_write", capture)
    _append_event(run_dir, "GSE123456", "ok")
    assert writes and writes[0][0] == log
    assert writes[0][1].endswith(b"\n")
    assert len(log.read_bytes().splitlines()) == 2


def test_run_dir_late_binding_sleep_default_uses_patched_time_sleep(monkeypatch, tmp_path):
    delays = []
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.time.sleep", delays.append)
    response = Mock(status_code=429, headers={})
    error = requests.HTTPError("429", response=response)
    get = Mock(side_effect=[error, _response()])
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", get)
    from tools.sc_dataset.geo_metadata import _fetch_and_record
    _fetch_and_record(tmp_path / "run", "GSE123456", 1)
    assert len(delays) == 1
    assert 0.5 <= delays[0] <= 1
