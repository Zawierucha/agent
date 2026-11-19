from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
import requests
from click.testing import CliRunner
from tools.sc_dataset.geo_search import (
    SearchRequestError,
    _deduplicate_gses,
    _extract_gse,
    _fetch_geo_records,
    _get_json,
    _normalize_record,
    _search_geo_ids,
    main,
    search_geo,
)


def make_response(status: int = 200, payload=None, headers=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = payload
    return response


def test_get_json_retries_429_then_returns_payload() -> None:
    session = Mock()
    session.get.side_effect = [
        make_response(429),
        make_response(429),
        make_response(200, {"ok": True}),
    ]
    sleep = Mock()

    assert _get_json(session, "https://example.test", {}, timeout=2, sleep=sleep) == {
        "ok": True
    }
    assert session.get.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]


def test_get_json_non_retryable_http_error_is_not_retried() -> None:
    session = Mock()
    session.get.return_value = make_response(400)
    sleep = Mock()

    with pytest.raises(requests.HTTPError, match="400"):
        _get_json(session, "https://example.test", {}, timeout=2, sleep=sleep)

    session.get.assert_called_once()
    sleep.assert_not_called()


def test_get_json_retryable_exhaustion_reports_attempts_and_status() -> None:
    session = Mock()
    session.get.return_value = make_response(503)
    sleep = Mock()

    with pytest.raises(SearchRequestError, match=r"4 attempts.*503"):
        _get_json(session, "https://example.test", {}, timeout=2, sleep=sleep)

    assert session.get.call_count == 4
    assert sleep.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]


def test_get_json_retry_after_seconds() -> None:
    session = Mock()
    session.get.side_effect = [
        make_response(429, headers={"Retry-After": "7"}),
        make_response(200, "ok"),
    ]
    sleep = Mock()

    assert (
        _get_json(session, "https://example.test", {}, timeout=2, sleep=sleep) == "ok"
    )
    sleep.assert_called_once_with(7)


def test_get_json_unparsable_retry_after_uses_exponential_fallback() -> None:
    session = Mock()
    session.get.side_effect = [
        make_response(503, headers={"Retry-After": "later"}),
        make_response(200, "ok"),
    ]
    sleep = Mock()

    assert (
        _get_json(session, "https://example.test", {}, timeout=2, sleep=sleep) == "ok"
    )
    sleep.assert_called_once_with(1)


def test_get_json_parses_retry_after_http_date() -> None:
    retry_date = datetime.now(UTC) + timedelta(seconds=45)
    session = Mock()
    session.get.side_effect = [
        make_response(503, headers={"Retry-After": format_datetime(retry_date)}),
        make_response(200, "ok"),
    ]
    sleep = Mock()

    _get_json(session, "https://example.test", {}, timeout=2, sleep=sleep)

    assert sleep.call_count == 1
    assert 0 <= sleep.call_args.args[0] <= 46


def test_get_json_clamps_past_retry_after_http_date() -> None:
    session = Mock()
    session.get.side_effect = [
        make_response(503, headers={"Retry-After": "Sun, 06 Nov 1994 08:49:37 GMT"}),
        make_response(200, "ok"),
    ]
    sleep = Mock()

    _get_json(session, "https://example.test", {}, timeout=2, sleep=sleep)

    sleep.assert_called_once_with(0)


def test_get_json_retries_connection_error_then_succeeds() -> None:
    session = Mock()
    session.get.side_effect = [
        requests.ConnectionError("offline"),
        make_response(200, {"ok": True}),
    ]
    sleep = Mock()

    assert _get_json(session, "url", {}, timeout=1, sleep=sleep) == {"ok": True}
    assert session.get.call_count == 2
    sleep.assert_called_once_with(1)


def test_get_json_timeout_exhaustion_names_exception_class() -> None:
    session = Mock()
    session.get.side_effect = requests.Timeout("slow")
    sleep = Mock()

    with pytest.raises(SearchRequestError, match=r"4 attempts.*Timeout"):
        _get_json(session, "url", {}, timeout=1, sleep=sleep)

    assert session.get.call_count == 4
    assert sleep.call_count == 3


def test_get_json_does_not_retry_invalid_json() -> None:
    session = Mock()
    response = make_response()
    error = ValueError("bad json")
    response.json.side_effect = error
    session.get.return_value = response
    sleep = Mock()

    with pytest.raises(ValueError) as exc_info:
        _get_json(session, "url", {}, timeout=1, sleep=sleep)

    assert exc_info.value is error
    session.get.assert_called_once()
    sleep.assert_not_called()


def test_search_geo_ids_returns_ids_count_and_request_params() -> None:
    session = Mock()
    session.get.return_value = make_response(
        payload={"esearchresult": {"idlist": ["123", "456"], "count": "250"}}
    )

    result = _search_geo_ids(
        "Crohn disease", max_results=50, timeout=10, session=session, sleep=Mock()
    )

    assert result == (["123", "456"], 250)
    assert session.get.call_args.kwargs["params"] == {
        "db": "gds",
        "term": "Crohn disease",
        "retmax": 50,
        "retmode": "json",
    }
    assert session.get.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("count", [None, "bad", []])
def test_search_geo_ids_unparseable_count_is_none(count) -> None:
    session = Mock()
    session.get.return_value = make_response(
        payload={
            "esearchresult": {
                "idlist": [],
                **({} if count is None else {"count": count}),
            }
        }
    )

    assert _search_geo_ids(
        "test", max_results=10, timeout=1, session=session, sleep=Mock()
    ) == ([], None)


def test_fetch_geo_records_empty_ids_does_not_request() -> None:
    session = Mock()
    assert _fetch_geo_records([], timeout=1, session=session, sleep=Mock()) == []
    session.get.assert_not_called()


def test_fetch_geo_records_uses_uids_and_skips_non_dict_entries() -> None:
    session = Mock()
    session.get.return_value = make_response(
        payload={
            "result": {
                "uids": ["1", "2", "3", "4"],
                "1": {"accession": "GSE1"},
                "3": "not-a-dict",
                "4": None,
                "9": {"accession": "GSE9"},
            }
        }
    )

    result = _fetch_geo_records(["1", "2", "3", "4"], timeout=1, session=session)

    assert result == [{"accession": "GSE1"}]
    assert session.get.call_args.kwargs["params"] == {
        "db": "gds",
        "id": "1,2,3,4",
        "retmode": "json",
    }


def test_extract_gse_accession_and_title_fallback() -> None:
    assert _extract_gse({"accession": "GSE123456"}) == "GSE123456"
    assert (
        _extract_gse({"title": "GSE123456 single-cell RNA sequencing"}) == "GSE123456"
    )
    assert _extract_gse({"accession": 123, "title": "GSE999 dataset"}) == "GSE999"
    assert (
        _extract_gse({"accession": "GSM123", "title": "GSE456 parent series"})
        == "GSE456"
    )


def test_extract_gse_invalid_title_tokens() -> None:
    for title in (
        "Some GEO dataset",
        "GSE",
        "GSE.",
        "GS123",
        "GSE123(Extra)",
        "(GSE456)",
        "GSE١٢٣",
    ):
        assert _extract_gse({"title": title}) is None


def test_extract_gse_accepts_punctuation_suffixes() -> None:
    assert _extract_gse({"title": "data GSE123:"}) == "GSE123"
    assert _extract_gse({"title": "data GSE456,"}) == "GSE456"
    assert _extract_gse({"title": "data GSE789)"}) == "GSE789"


def test_normalize_record_preserves_schema_and_missing_fields() -> None:
    record = {
        "accession": "GSE123",
        "title": "Example dataset",
        "summary": "Example summary",
        "taxon": "Homo sapiens",
        "gdstype": "Expression profiling by high throughput sequencing",
        "organism": "Homo sapiens",
        "gpl": "GPL24676",
        "n_samples": 10,
    }
    assert _normalize_record(record, query="single cell") == {
        "gse_id": "GSE123",
        "title": "Example dataset",
        "summary": "Example summary",
        "taxon": "Homo sapiens",
        "gds_type": "Expression profiling by high throughput sequencing",
        "organism": "Homo sapiens",
        "platform": "GPL24676",
        "n_samples": 10,
        "query": "single cell",
    }
    missing = _normalize_record({"accession": "GSE1", "title": 123}, query="test")
    assert missing["gse_id"] == "GSE1"
    assert missing["title"] == 123
    assert missing["summary"] is None


def test_deduplicate_gses_keeps_first_and_ignores_missing_gse() -> None:
    records = [
        {"gse_id": None, "title": "Invalid"},
        {"gse_id": "GSE1", "title": "First"},
        {"gse_id": "GSE2", "title": "Second"},
        {"gse_id": "GSE1", "title": "Duplicate"},
    ]
    result = _deduplicate_gses(records)
    assert [record["gse_id"] for record in result] == ["GSE1", "GSE2"]
    assert result[0]["title"] == "First"


def test_search_geo_cleans_and_deduplicates_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_mock = Mock(return_value=([], 0))
    fetch_mock = Mock(return_value=[])
    monkeypatch.setattr("tools.sc_dataset.geo_search._search_geo_ids", search_mock)
    monkeypatch.setattr("tools.sc_dataset.geo_search._fetch_geo_records", fetch_mock)

    result = search_geo([None, 42, " Crohn disease ", "Crohn disease", "   "])

    assert result["queries"] == [
        {
            "query": "Crohn disease",
            "status": "success",
            "count": 0,
            "retrieved": 0,
            "error": None,
        }
    ]
    search_mock.assert_called_once()
    assert search_mock.call_args.args == ("Crohn disease",)


def test_search_geo_multiple_query_records_and_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_mock = Mock(side_effect=[(["1"], 1), (["2"], 1)])
    fetch_mock = Mock(
        side_effect=[
            [{"accession": "GSE111", "title": "First", "taxon": "human"}],
            [{"accession": "GSE222", "title": "Second", "taxon": "mouse"}],
        ]
    )
    monkeypatch.setattr("tools.sc_dataset.geo_search._search_geo_ids", search_mock)
    monkeypatch.setattr("tools.sc_dataset.geo_search._fetch_geo_records", fetch_mock)

    result = search_geo(
        ["crohn query", "mouse query"], max_results_per_query=25, timeout=12.5
    )

    assert [row["gse_id"] for row in result["records"]] == ["GSE111", "GSE222"]
    assert [row["query"] for row in result["records"]] == ["crohn query", "mouse query"]
    assert [r["status"] for r in result["queries"]] == ["success", "success"]
    assert search_mock.call_count == fetch_mock.call_count == 2
    assert search_mock.call_args.kwargs["max_results"] == 25
    assert search_mock.call_args.kwargs["timeout"] == 12.5


def test_search_geo_envelope_reports_partial_and_success_and_merges_queries(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(side_effect=[(["1", "2"], 250), (["3", "4"], 2)]),
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(
            side_effect=[
                [{"accession": "GSE123", "title": "First"}, {"accession": "GSE456"}],
                [
                    {"accession": "GSE123", "title": "Duplicate"},
                    {"accession": "GSE789"},
                ],
            ]
        ),
    )

    result = search_geo(["query one", "query two"])

    assert result["queries"] == [
        {
            "query": "query one",
            "status": "partial",
            "count": 250,
            "retrieved": 2,
            "error": None,
        },
        {
            "query": "query two",
            "status": "success",
            "count": 2,
            "retrieved": 2,
            "error": None,
        },
    ]
    assert [row["gse_id"] for row in result["records"]] == [
        "GSE123",
        "GSE456",
        "GSE789",
    ]
    assert result["records"][0]["query"] == "query one; query two"
    assert result["dropped_no_gse"] == 0


def test_search_geo_zero_results_does_not_fetch_summary() -> None:
    session = Mock()
    session.get.return_value = make_response(
        payload={"esearchresult": {"idlist": [], "count": "0"}}
    )
    session_context = Mock()
    session_context.__enter__ = Mock(return_value=session)
    session_context.__exit__ = Mock(return_value=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search.requests.Session",
        Mock(return_value=session_context),
    )
    try:
        result = search_geo(["empty query"], sleep=Mock())
    finally:
        monkeypatch.undo()

    assert result["queries"] == [
        {
            "query": "empty query",
            "status": "success",
            "count": 0,
            "retrieved": 0,
            "error": None,
        }
    ]
    session.get.assert_called_once()
    assert session.get.call_args.args[0].endswith("esearch.fcgi")


def test_search_geo_missing_count_is_none_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids", Mock(return_value=([], None))
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records", Mock(return_value=[])
    )

    result = search_geo(["query"])

    assert result["queries"][0]["count"] is None
    assert result["queries"][0]["status"] == "success"


def test_search_geo_failed_query_does_not_block_later_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(side_effect=[SearchRequestError("unavailable"), (["2"], 1)]),
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(return_value=[{"accession": "GSE22"}]),
    )

    result = search_geo(["bad query", "good query"])

    assert result["queries"][0]["status"] == "failed"
    assert result["queries"][0]["error"].startswith("SearchRequestError:")
    assert result["queries"][0]["retrieved"] == 0
    assert result["queries"][1]["status"] == "success"
    assert [row["gse_id"] for row in result["records"]] == ["GSE22"]


def test_search_geo_failed_summary_reports_esearch_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(return_value=(["1", "2"], 8)),
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(side_effect=requests.ConnectionError("summary unavailable")),
    )

    result = search_geo(["query"])

    assert result["queries"][0] == {
        "query": "query",
        "status": "failed",
        "count": 8,
        "retrieved": 2,
        "error": "ConnectionError: summary unavailable",
    }
    assert result["records"] == []


def test_search_geo_counts_dropped_unlocatable_records_before_dedup(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(return_value=(["1", "2", "3"], 3)),
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(
            return_value=[
                {"title": "No accession one"},
                {"accession": "GSM123", "title": "No series two"},
                {"accession": "GSE3", "title": "Valid"},
            ]
        ),
    )

    result = search_geo(["query"])

    assert result["dropped_no_gse"] == 2
    assert [row["gse_id"] for row in result["records"]] == ["GSE3"]


def test_search_geo_empty_queries_do_not_create_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = Mock(side_effect=AssertionError("session should not be created"))
    monkeypatch.setattr("tools.sc_dataset.geo_search.requests.Session", session_factory)

    assert search_geo([]) == {"queries": [], "records": [], "dropped_no_gse": 0}
    assert search_geo([None, "", "  "]) == {
        "queries": [],
        "records": [],
        "dropped_no_gse": 0,
    }
    session_factory.assert_not_called()


def test_search_geo_catches_malformed_or_missing_data_and_continues(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(side_effect=[KeyError("esearchresult"), (["2"], None)]),
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(return_value=[{"accession": "GSE2"}]),
    )

    result = search_geo(["bad", "good"])

    assert result["queries"][0]["error"] == "KeyError: 'esearchresult'"
    assert result["queries"][1]["status"] == "success"
    assert result["records"][0]["gse_id"] == "GSE2"


def test_cli_requires_query_and_prints_envelope_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert CliRunner().invoke(main).exit_code != 0
    expected = {"queries": [], "records": [], "dropped_no_gse": 0}
    search_mock = Mock(return_value=expected)
    monkeypatch.setattr("tools.sc_dataset.geo_search.search_geo", search_mock)

    result = CliRunner().invoke(main, ["--query", "example"])

    assert result.exit_code == 0
    import json

    output = json.loads(result.output)
    assert set(output) == {"queries", "records", "dropped_no_gse"}
    search_mock.assert_called_once_with(
        ["example"], max_results_per_query=100, timeout=30.0
    )


def test_cli_rejects_max_results_above_ncbi_limit() -> None:
    result = CliRunner().invoke(main, ["--query", "example", "--max-results", "20000"])
    assert result.exit_code != 0
    assert "10000" in result.output


def test_search_geo_missing_idlist_fails_query_without_blocking_next(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(side_effect=[KeyError("idlist"), (["2"], 1)]),
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(return_value=[{"accession": "GSE2"}]),
    )

    result = search_geo(["missing id list", "valid"])

    assert result["queries"][0]["status"] == "failed"
    assert result["queries"][0]["error"] == "KeyError: 'idlist'"
    assert result["queries"][0]["retrieved"] == 0
    assert result["records"][0]["gse_id"] == "GSE2"


def test_fetch_geo_records_malformed_json_propagates_without_retry() -> None:
    session = Mock()
    response = make_response()
    error = ValueError("Expecting value")
    response.json.side_effect = error
    session.get.return_value = response
    sleep = Mock()

    with pytest.raises(ValueError) as exc_info:
        _fetch_geo_records(["1"], timeout=1, session=session, sleep=sleep)

    assert exc_info.value is error
    session.get.assert_called_once()
    sleep.assert_not_called()


def test_fetch_geo_records_missing_result_propagates_keyerror() -> None:
    session = Mock()
    session.get.return_value = make_response(payload={})

    with pytest.raises(KeyError, match="result"):
        _fetch_geo_records(["1"], timeout=1, session=session, sleep=Mock())


def test_search_geo_zero_ids_is_success_and_skips_esummary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids", Mock(return_value=([], 0))
    )
    fetch_mock = Mock()
    monkeypatch.setattr("tools.sc_dataset.geo_search._fetch_geo_records", fetch_mock)

    result = search_geo(["empty"])

    assert result["queries"] == [
        {
            "query": "empty",
            "status": "success",
            "count": 0,
            "retrieved": 0,
            "error": None,
        }
    ]
    fetch_mock.assert_not_called()


def test_search_geo_pretty_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result_envelope = {"queries": [], "records": [], "dropped_no_gse": 0}
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search.search_geo", Mock(return_value=result_envelope)
    )

    result = CliRunner().invoke(main, ["--query", "example", "--pretty"])

    assert result.exit_code == 0
    import json

    assert json.loads(result.output) == result_envelope
    assert '\n  "queries"' in result.output


def test_cli_accepts_multiple_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    result_envelope = {"queries": [], "records": [], "dropped_no_gse": 0}
    search_mock = Mock(return_value=result_envelope)
    monkeypatch.setattr("tools.sc_dataset.geo_search.search_geo", search_mock)

    result = CliRunner().invoke(
        main,
        ["--query", "Crohn disease", "--query", "ulcerative colitis"],
    )

    assert result.exit_code == 0
    search_mock.assert_called_once_with(
        ["Crohn disease", "ulcerative colitis"],
        max_results_per_query=100,
        timeout=30.0,
    )
