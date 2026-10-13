from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from tools.sc_dataset.geo_metadata import (
    _extract_gsm_ids,
    _fetch_sample_summaries,
    _fetch_summary,
    _find_geo_id,
    _normalize_sample,
    _normalize_series,
    get_geo_metadata,
)


def test_find_geo_id() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "esearchresult": {
            "idlist": ["123456"],
        }
    }

    session.get.return_value = response

    result = _find_geo_id(
        "GSE123456",
        timeout=10.0,
        session=session,
    )

    assert result == "123456"

    session.get.assert_called_once()

    kwargs = session.get.call_args.kwargs

    assert kwargs["params"] == {
        "db": "gds",
        "term": "GSE123456[Accession]",
        "retmax": 1,
        "retmode": "json",
    }

    assert kwargs["timeout"] == 10.0


def test_find_geo_id_raises_when_accession_not_found() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "esearchresult": {
            "idlist": [],
        }
    }

    session.get.return_value = response

    with pytest.raises(
        ValueError,
        match="GEO accession not found",
    ):
        _find_geo_id(
            "GSE999999",
            timeout=10.0,
            session=session,
        )


def test_find_geo_id_propagates_http_error() -> None:
    session = Mock()

    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError()

    session.get.return_value = response

    with pytest.raises(requests.HTTPError):
        _find_geo_id(
            "GSE123456",
            timeout=10.0,
            session=session,
        )


def test_fetch_summary() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "result": {
            "123456": {
                "uid": "123456",
                "accession": "GSE123456",
                "title": "Example dataset",
            }
        }
    }

    session.get.return_value = response

    result = _fetch_summary(
        "123456",
        timeout=10.0,
        session=session,
    )

    assert result == {
        "uid": "123456",
        "accession": "GSE123456",
        "title": "Example dataset",
    }

    session.get.assert_called_once()

    kwargs = session.get.call_args.kwargs

    assert kwargs["params"] == {
        "db": "gds",
        "id": "123456",
        "retmode": "json",
    }

    assert kwargs["timeout"] == 10.0


def test_fetch_summary_raises_when_record_missing() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "result": {
            "uids": [],
        }
    }

    session.get.return_value = response

    with pytest.raises(
        ValueError,
        match="No GEO summary returned",
    ):
        _fetch_summary(
            "123456",
            timeout=10.0,
            session=session,
        )


def test_extract_gsm_ids() -> None:
    record = {
        "samples": [
            "GSM100",
            "GSM200",
            "GPL300",
            "GSE400",
        ]
    }

    assert _extract_gsm_ids(record) == [
        "GSM100",
        "GSM200",
    ]


def test_extract_gsm_ids_returns_empty_for_missing_samples() -> None:
    assert _extract_gsm_ids({}) == []


def test_extract_gsm_ids_returns_empty_for_invalid_samples() -> None:
    assert _extract_gsm_ids(
        {
            "samples": "GSM100",
        }
    ) == []


def test_normalize_series() -> None:
    record = {
        "title": "Human single-cell dataset",
        "summary": "Example summary",
        "overall_design": "Single-cell RNA sequencing",
        "organism": "Homo sapiens",
        "taxon": "9606",
        "gpl": "GPL24676",
        "n_samples": 5,
        "samples": [
            "GSM100",
            "GSM200",
        ],
    }

    result = _normalize_series(
        record,
        "GSE123456",
    )

    assert result == {
        "gse_id": "GSE123456",
        "title": "Human single-cell dataset",
        "summary": "Example summary",
        "overall_design": "Single-cell RNA sequencing",
        "organism": "Homo sapiens",
        "taxon": "9606",
        "platform": "GPL24676",
        "n_samples": 5,
        "samples": [
            "GSM100",
            "GSM200",
        ],
    }


def test_normalize_series_handles_missing_fields() -> None:
    result = _normalize_series(
        {
            "title": "Example",
        },
        "GSE123456",
    )

    assert result["gse_id"] == "GSE123456"
    assert result["title"] == "Example"
    assert result["summary"] is None
    assert result["overall_design"] is None
    assert result["organism"] is None
    assert result["platform"] is None
    assert result["n_samples"] is None
    assert result["samples"] == []


def test_normalize_sample() -> None:
    record = {
        "title": "Sample 1",
        "summary": "Sample summary",
        "source_name": "Colon tissue",
        "organism": "Homo sapiens",
        "characteristics": [
            "disease: Crohn disease",
            "tissue: colon",
        ],
        "gpl": "GPL24676",
    }

    result = _normalize_sample(
        record,
        "GSM123456",
    )

    assert result == {
        "gsm_id": "GSM123456",
        "title": "Sample 1",
        "summary": "Sample summary",
        "source_name": "Colon tissue",
        "organism": "Homo sapiens",
        "characteristics": [
            "disease: Crohn disease",
            "tissue: colon",
        ],
        "platform": "GPL24676",
    }


def test_normalize_sample_handles_missing_fields() -> None:
    result = _normalize_sample(
        {},
        "GSM123456",
    )

    assert result == {
        "gsm_id": "GSM123456",
        "title": None,
        "summary": None,
        "source_name": None,
        "organism": None,
        "characteristics": None,
        "platform": None,
    }


def test_fetch_sample_summaries_empty_ids() -> None:
    session = Mock()

    result = _fetch_sample_summaries(
        [],
        timeout=10.0,
        session=session,
    )

    assert result == []
    session.get.assert_not_called()


def test_fetch_sample_summaries() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "result": {
            "uids": [
                "100",
                "200",
            ],
            "100": {
                "title": "Sample 1",
                "source_name": "Colon",
                "organism": "Homo sapiens",
                "characteristics": [
                    "disease: Crohn disease",
                ],
                "gpl": "GPL24676",
            },
            "200": {
                "title": "Sample 2",
                "source_name": "Ileum",
                "organism": "Homo sapiens",
                "characteristics": [
                    "disease: ulcerative colitis",
                ],
                "gpl": "GPL24676",
            },
        }
    }

    session.get.return_value = response

    result = _fetch_sample_summaries(
        [
            "100",
            "200",
        ],
        timeout=10.0,
        session=session,
    )

    assert result == [
        {
            "gsm_id": "100",
            "title": "Sample 1",
            "summary": None,
            "source_name": "Colon",
            "organism": "Homo sapiens",
            "characteristics": [
                "disease: Crohn disease",
            ],
            "platform": "GPL24676",
        },
        {
            "gsm_id": "200",
            "title": "Sample 2",
            "summary": None,
            "source_name": "Ileum",
            "organism": "Homo sapiens",
            "characteristics": [
                "disease: ulcerative colitis",
            ],
            "platform": "GPL24676",
        },
    ]

    session.get.assert_called_once()

    kwargs = session.get.call_args.kwargs

    assert kwargs["params"] == {
        "db": "gds",
        "id": "100,200",
        "retmode": "json",
    }

    assert kwargs["timeout"] == 10.0


def test_fetch_sample_summaries_skips_missing_records() -> None:
    session = Mock()

    response = Mock()
    response.json.return_value = {
        "result": {
            "uids": ["100"],
            "100": {
                "title": "Sample 1",
            },
        }
    }

    session.get.return_value = response

    result = _fetch_sample_summaries(
        [
            "100",
            "200",
        ],
        timeout=10.0,
        session=session,
    )

    assert result == [
        {
            "gsm_id": "100",
            "title": "Sample 1",
            "summary": None,
            "source_name": None,
            "organism": None,
            "characteristics": None,
            "platform": None,
        }
    ]


def test_get_geo_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    find_mock = Mock(return_value="123456")

    summary_mock = Mock(
        return_value={
            "title": "Human scRNA-seq",
            "summary": "IBD study",
            "overall_design": "Single-cell RNA sequencing",
            "organism": "Homo sapiens",
            "taxon": "9606",
            "gpl": "GPL24676",
            "n_samples": 2,
            "samples": [
                "GSM100",
                "GSM200",
            ],
        }
    )

    samples_mock = Mock(
        return_value=[
            {
                "gsm_id": "GSM100",
                "title": "Sample 1",
            },
            {
                "gsm_id": "GSM200",
                "title": "Sample 2",
            },
        ]
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata._find_geo_id",
        find_mock,
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata._fetch_summary",
        summary_mock,
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata._fetch_sample_summaries",
        samples_mock,
    )

    result = get_geo_metadata(
        "gse123456",
        timeout=15.0,
    )

    assert result == {
        "gse": {
            "gse_id": "GSE123456",
            "title": "Human scRNA-seq",
            "summary": "IBD study",
            "overall_design": "Single-cell RNA sequencing",
            "organism": "Homo sapiens",
            "taxon": "9606",
            "platform": "GPL24676",
            "n_samples": 2,
        },
        "samples": [
            {
                "gsm_id": "GSM100",
                "title": "Sample 1",
            },
            {
                "gsm_id": "GSM200",
                "title": "Sample 2",
            },
        ],
    }

    find_mock.assert_called_once()
    summary_mock.assert_called_once()
    samples_mock.assert_called_once()


@pytest.mark.parametrize(
    "accession",
    [
        "",
        "GSM123456",
        "GPL123",
        "123456",
    ],
)
def test_get_geo_metadata_rejects_invalid_accession(
    accession: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="Expected a GSE accession",
    ):
        get_geo_metadata(accession)


def test_get_geo_metadata_normalizes_accession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    find_mock = Mock(return_value="123456")

    summary_mock = Mock(
        return_value={
            "title": "Example",
            "samples": [],
        }
    )

    samples_mock = Mock(return_value=[])

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata._find_geo_id",
        find_mock,
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata._fetch_summary",
        summary_mock,
    )

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata._fetch_sample_summaries",
        samples_mock,
    )

    result = get_geo_metadata("  gse123456  ")

    assert result["gse"]["gse_id"] == "GSE123456"

    find_mock.assert_called_once()

    assert find_mock.call_args.args[0] == "GSE123456"