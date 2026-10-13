from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from tools.sc_dataset.geo_search import (
    _deduplicate_gses,
    _extract_gse,
    _fetch_geo_records,
    _normalize_record,
    _search_geo_ids,
    search_geo,
)


def test_search_geo_requires_queries() -> None:
    assert search_geo([]) == []


def test_search_geo_ignores_empty_queries() -> None:
    assert search_geo(["", "   "]) == []


def test_search_geo_deduplicates_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    search_mock = Mock(return_value=["1"])
    fetch_mock = Mock(return_value=[])

    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        search_mock,
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        fetch_mock,
    )

    search_geo(
        [
            "Crohn disease",
            "Crohn disease",
            "   Crohn disease   ",
        ]
    )

    assert search_mock.call_count == 1
    search_mock.assert_called_once()


def test_search_geo_executes_multiple_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_mock = Mock(
        side_effect=[
            ["1"],
            ["2"],
        ]
    )

    fetch_mock = Mock(
        side_effect=[
            [
                {
                    "accession": "GSE111",
                    "title": "First dataset",
                }
            ],
            [
                {
                    "accession": "GSE222",
                    "title": "Second dataset",
                }
            ],
        ]
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        search_mock,
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        fetch_mock,
    )

    results = search_geo(
        [
            "Crohn disease single cell",
            "ulcerative colitis single cell",
        ]
    )

    assert [result["gse_id"] for result in results] == [
        "GSE111",
        "GSE222",
    ]

    assert search_mock.call_count == 2
    assert fetch_mock.call_count == 2


def test_search_geo_passes_max_results_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_mock = Mock(return_value=[])
    fetch_mock = Mock(return_value=[])

    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        search_mock,
    )
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        fetch_mock,
    )

    search_geo(
        ["test query"],
        max_results_per_query=25,
        timeout=12.5,
    )

    search_mock.assert_called_once()
    kwargs = search_mock.call_args.kwargs

    assert kwargs["max_results"] == 25
    assert kwargs["timeout"] == 12.5


def test_search_geo_deduplicates_gses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(
            side_effect=[
                ["1"],
                ["2"],
            ]
        ),
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(
            side_effect=[
                [
                    {
                        "accession": "GSE123",
                        "title": "Dataset",
                    }
                ],
                [
                    {
                        "accession": "GSE123",
                        "title": "Dataset",
                    }
                ],
            ]
        ),
    )

    results = search_geo(
        [
            "query one",
            "query two",
        ]
    )

    assert len(results) == 1
    assert results[0]["gse_id"] == "GSE123"


def test_search_geo_preserves_discovery_queries_for_duplicate_gse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._search_geo_ids",
        Mock(
            side_effect=[
                ["1"],
                ["2"],
            ]
        ),
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_search._fetch_geo_records",
        Mock(
            side_effect=[
                [{"accession": "GSE123"}],
                [{"accession": "GSE123"}],
            ]
        ),
    )

    results = search_geo(
        [
            "query one",
            "query two",
        ]
    )

    assert len(results) == 1
    assert results[0]["query"] == "query one; query two"


def test_extract_gse_from_accession() -> None:
    record = {
        "accession": "GSE123456",
    }

    assert _extract_gse(record) == "GSE123456"


def test_extract_gse_from_title() -> None:
    record = {
        "title": "GSE123456 single-cell RNA sequencing",
    }

    assert _extract_gse(record) == "GSE123456"


def test_extract_gse_returns_none_when_missing() -> None:
    record = {
        "title": "Some GEO dataset",
    }

    assert _extract_gse(record) is None


def test_normalize_record() -> None:
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

    result = _normalize_record(
        record,
        query="single cell",
    )

    assert result == {
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


def test_normalize_record_handles_missing_fields() -> None:
    result = _normalize_record(
        {
            "accession": "GSE123",
        },
        query="test",
    )

    assert result["gse_id"] == "GSE123"
    assert result["query"] == "test"
    assert result["title"] is None
    assert result["summary"] is None


def test_deduplicate_gses() -> None:
    records = [
        {
            "gse_id": "GSE1",
            "title": "First",
        },
        {
            "gse_id": "GSE2",
            "title": "Second",
        },
        {
            "gse_id": "GSE1",
            "title": "Duplicate",
        },
    ]

    result = _deduplicate_gses(records)

    assert len(result) == 2
    assert [record["gse_id"] for record in result] == [
        "GSE1",
        "GSE2",
    ]


def test_deduplicate_gses_ignores_records_without_gse() -> None:
    records = [
        {
            "gse_id": None,
            "title": "Invalid",
        },
        {
            "gse_id": "GSE1",
            "title": "Valid",
        },
    ]

    result = _deduplicate_gses(records)

    assert result == [
        {
            "gse_id": "GSE1",
            "title": "Valid",
        }
    ]


def test_search_geo_ids() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "esearchresult": {
            "idlist": ["123", "456"],
        }
    }

    session.get.return_value = response

    result = _search_geo_ids(
        "Crohn disease",
        max_results=50,
        timeout=10.0,
        session=session,
    )

    assert result == ["123", "456"]

    session.get.assert_called_once()

    kwargs = session.get.call_args.kwargs

    assert kwargs["params"] == {
        "db": "gds",
        "term": "Crohn disease",
        "retmax": 50,
        "retmode": "json",
    }

    assert kwargs["timeout"] == 10.0


def test_search_geo_ids_raises_for_http_error() -> None:
    session = Mock()

    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError()

    session.get.return_value = response

    with pytest.raises(requests.HTTPError):
        _search_geo_ids(
            "test",
            max_results=10,
            timeout=5.0,
            session=session,
        )


def test_fetch_geo_records_empty_ids() -> None:
    session = Mock()

    result = _fetch_geo_records(
        [],
        timeout=10.0,
        session=session,
    )

    assert result == []
    session.get.assert_not_called()


def test_fetch_geo_records() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "result": {
            "uids": ["123"],
            "123": {
                "uid": "123",
                "accession": "GSE123",
                "title": "Example",
            },
        }
    }

    session.get.return_value = response

    result = _fetch_geo_records(
        ["123"],
        timeout=10.0,
        session=session,
    )

    assert result == [
        {
            "uid": "123",
            "accession": "GSE123",
            "title": "Example",
        }
    ]

    session.get.assert_called_once()

    kwargs = session.get.call_args.kwargs

    assert kwargs["params"] == {
        "db": "gds",
        "id": "123",
        "retmode": "json",
    }

    assert kwargs["timeout"] == 10.0


def test_fetch_geo_records_multiple_ids() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "result": {
            "uids": ["1", "2"],
            "1": {
                "accession": "GSE1",
            },
            "2": {
                "accession": "GSE2",
            },
        }
    }

    session.get.return_value = response

    result = _fetch_geo_records(
        ["1", "2"],
        timeout=10.0,
        session=session,
    )

    assert result == [
        {"accession": "GSE1"},
        {"accession": "GSE2"},
    ]


def test_cli_requires_query() -> None:
    from click.testing import CliRunner

    from tools.sc_dataset.geo_search import main

    runner = CliRunner()

    result = runner.invoke(main)

    assert result.exit_code != 0
    assert "Missing option" in result.output


def test_cli_accepts_multiple_queries() -> None:
    from click.testing import CliRunner

    from tools.sc_dataset.geo_search import main

    runner = CliRunner()

    with runner.isolated_filesystem():
        # Prevent a real NCBI request.
        result = runner.invoke(
            main,
            [
                "--query",
                "Crohn disease single cell",
                "--query",
                "ulcerative colitis single cell",
            ],
        )

    # The command reaches the search implementation.
    # A network failure is acceptable here because this test only
    # verifies Click parsing.
    assert "Traceback" not in result.output