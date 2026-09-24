from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
import requests

from tools.sc_dataset.geo_search import (
    IBD_QUERIES,
    _fetch_geo_records,
    search_geo,
    search_ibd_sc_datasets,
)


def make_response(payload: dict) -> Mock:
    """Create a fake requests.Response."""
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None

    return response


def geo_record(
    accession: str,
    *,
    title: str = "Test dataset",
    summary: str = "Test summary",
    taxon: str = "Homo sapiens",
) -> dict:
    """Create a fake NCBI GEO esummary record."""
    return {
        "accession": accession,
        "title": title,
        "summary": summary,
        "taxon": taxon,
    }


def test_search_geo_returns_gse_candidates() -> None:
    """A GEO search should return GSE candidates from NCBI."""

    esearch_response = make_response(
        {
            "esearchresult": {
                "idlist": ["12345", "67890"],
            }
        }
    )

    esummary_response = make_response(
        {
            "result": {
                "12345": geo_record(
                    "GSE12345",
                    title="Crohn disease single-cell RNA-seq",
                ),
                "67890": geo_record(
                    "GSE67890",
                    title="Ulcerative colitis single-cell RNA-seq",
                ),
            }
        }
    )

    with patch(
        "tools.sc_dataset.geo_search.requests.get",
        side_effect=[
            esearch_response,
            esummary_response,
        ],
    ) as mock_get:
        results = search_geo(
            '"Crohn disease"',
            max_results=10,
        )

    assert len(results) == 2

    assert results[0]["gse_id"] == "GSE12345"
    assert results[0]["accession"] == "GSE12345"
    assert results[0]["title"] == "Crohn disease single-cell RNA-seq"

    assert results[1]["gse_id"] == "GSE67890"

    assert mock_get.call_count == 2


def test_search_geo_ignores_non_gse_records() -> None:
    """Records that are not GSE accessions should be ignored."""

    esearch_response = make_response(
        {
            "esearchresult": {
                "idlist": ["1", "2", "3"],
            }
        }
    )

    esummary_response = make_response(
        {
            "result": {
                "1": geo_record("GSE11111"),
                "2": geo_record("GSM22222"),
                "3": geo_record("GPL33333"),
            }
        }
    )

    with patch(
        "tools.sc_dataset.geo_search.requests.get",
        side_effect=[
            esearch_response,
            esummary_response,
        ],
    ):
        results = search_geo(
            "Crohn disease",
            max_results=10,
        )

    assert len(results) == 1
    assert results[0]["gse_id"] == "GSE11111"


def test_search_geo_returns_empty_when_no_results() -> None:
    """A GEO query with no matching records should return an empty list."""

    response = make_response(
        {
            "esearchresult": {
                "idlist": [],
            }
        }
    )

    with patch(
        "tools.sc_dataset.geo_search.requests.get",
        return_value=response,
    ) as mock_get:
        results = search_geo(
            "query with no results",
            max_results=10,
        )

    assert results == []
    assert mock_get.call_count == 1


def test_fetch_geo_records_skips_missing_records() -> None:
    """Missing records returned by esummary should be ignored."""

    response = make_response(
        {
            "result": {
                "1": geo_record("GSE12345"),
            }
        }
    )

    with patch(
        "tools.sc_dataset.geo_search.requests.get",
        return_value=response,
    ):
        results = _fetch_geo_records(
            ["1", "2"],
            timeout=10.0
        )

    assert len(results) == 1
    assert results[0]["gse_id"] == "GSE12345"


def test_search_ibd_sc_datasets_deduplicates_gses() -> None:
    """The complete IBD search should merge duplicate GSE accessions."""

    def fake_search_geo(
        query: str,
        *,
        max_results: int,
        timeout: float,
    ) -> list[dict]:
        if query == IBD_QUERIES[0]:
            return [
                {
                    "gse_id": "GSE10001",
                    "accession": "GSE10001",
                    "title": "IBD dataset",
                    "summary": "IBD summary",
                    "organism": "Homo sapiens",
                    "source": "NCBI GEO",
                },
                {
                    "gse_id": "GSE10002",
                    "accession": "GSE10002",
                    "title": "Crohn dataset",
                    "summary": "Crohn summary",
                    "organism": "Homo sapiens",
                    "source": "NCBI GEO",
                },
            ]

        return [
            {
                "gse_id": "GSE10001",
                "accession": "GSE10001",
                "title": "IBD dataset duplicate",
                "summary": "Duplicate",
                "organism": "Homo sapiens",
                "source": "NCBI GEO",
            }
        ]

    with patch(
        "tools.sc_dataset.geo_search.search_geo",
        side_effect=fake_search_geo,
    ) as mock_search:
        results = search_ibd_sc_datasets(
            max_results_per_query=10,
        )

    assert len(results) == 2

    gse_ids = [result["gse_id"] for result in results]

    assert gse_ids == [
        "GSE10001",
        "GSE10002",
    ]

    assert mock_search.call_count == len(IBD_QUERIES)


def test_search_ibd_sc_datasets_preserves_first_duplicate() -> None:
    """The first occurrence of a duplicate GSE should be retained."""

    first_result = {
        "gse_id": "GSE12345",
        "accession": "GSE12345",
        "title": "Original title",
        "summary": "Original summary",
        "organism": "Homo sapiens",
        "source": "NCBI GEO",
    }

    duplicate_result = {
        "gse_id": "GSE12345",
        "accession": "GSE12345",
        "title": "Duplicate title",
        "summary": "Duplicate summary",
        "organism": "Homo sapiens",
        "source": "NCBI GEO",
    }

    def fake_search_geo(
        query: str,
        *,
        max_results: int,
        timeout: float,
    ) -> list[dict]:
        if query == IBD_QUERIES[0]:
            return [first_result]

        return [duplicate_result]

    with patch(
        "tools.sc_dataset.geo_search.search_geo",
        side_effect=fake_search_geo,
    ):
        results = search_ibd_sc_datasets(
            max_results_per_query=10,
        )

    assert len(results) == 1
    assert results[0]["title"] == "Original title"
    assert results[0]["summary"] == "Original summary"


def test_search_ibd_sc_datasets_uses_all_ibd_queries() -> None:
    """Every configured IBD query should be executed."""

    with patch(
        "tools.sc_dataset.geo_search.search_geo",
        return_value=[],
    ) as mock_search:
        results = search_ibd_sc_datasets(
            max_results_per_query=5,
        )

    assert results == []
    assert mock_search.call_count == len(IBD_QUERIES)

    called_queries = [
        call.kwargs.get("query", call.args[0])
        for call in mock_search.call_args_list
    ]

    assert called_queries == IBD_QUERIES


def test_search_geo_propagates_http_errors() -> None:
    """HTTP failures should propagate as requests exceptions."""

    with patch(
        "tools.sc_dataset.geo_search.requests.get",
        side_effect=requests.RequestException("NCBI unavailable"),
    ):
        with pytest.raises(requests.RequestException, match="NCBI unavailable"):
            search_geo(
                "Crohn disease",
                max_results=10,
            )


def test_search_geo_passes_max_results() -> None:
    """The requested result limit should be sent to NCBI."""

    response = make_response(
        {
            "esearchresult": {
                "idlist": [],
            }
        }
    )

    with patch(
        "tools.sc_dataset.geo_search.requests.get",
        return_value=response,
    ) as mock_get:
        search_geo(
            "Crohn disease",
            max_results=25,
        )

    params = mock_get.call_args.kwargs["params"]

    assert params["retmax"] == 25
    assert params["db"] == "gds"
    assert params["retmode"] == "json"