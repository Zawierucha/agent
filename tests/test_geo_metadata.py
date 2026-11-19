import gzip
import json
from unittest.mock import Mock

import pytest
import requests
from click.testing import CliRunner

from tools.sc_dataset.geo_metadata import (
    GEO_FTP_URL,
    _get_soft_url,
    _normalize_sample,
    _parse_characteristics,
    _parse_soft,
    _parse_soft_value,
    get_geo_metadata,
    main,
)


def test_get_soft_url_builds_expected_url():
    assert _get_soft_url("GSE346721") == (
        f"{GEO_FTP_URL}/GSE346nnn/GSE346721/soft/" "GSE346721_family.soft.gz"
    )


@pytest.mark.parametrize(
    "accession",
    ["GSE12", "GSE", "GSM123", "abc", "GSE12x"],
)
def test_get_soft_url_rejects_invalid_accession(accession):
    with pytest.raises(ValueError, match="Invalid GEO Series accession"):
        _get_soft_url(accession)


def test_parse_soft_value_strips_whitespace_and_quotes():
    assert _parse_soft_value('  "Colon tissue"  ') == "Colon tissue"


def test_parse_characteristics_handles_repeated_keys_and_unparsed_values():
    result = _parse_characteristics(
        [
            "disease: Crohn disease",
            "tissue: colon",
            "disease: ulcerative colitis",
            "sample note without colon",
            "",
        ]
    )

    assert result == {
        "disease": ["Crohn disease", "ulcerative colitis"],
        "tissue": "colon",
        "unparsed": ["sample note without colon"],
    }


def test_parse_characteristics_handles_empty_values():
    assert _parse_characteristics([]) == {}
    assert _parse_characteristics(["", "  "]) == {}


def test_normalize_sample_parses_fields_and_characteristics():
    result = _normalize_sample(
        {
            "gsm_id": "GSM123456",
            "title": "Sample 1",
            "summary": ["First line", "Second line"],
            "source_name": ["Colon tissue"],
            "organism": ["Homo sapiens"],
            "characteristics": [
                "disease: Crohn disease",
                "tissue: colon",
            ],
            "platform": "GPL24676",
        }
    )

    assert result == {
        "gsm_id": "GSM123456",
        "title": "Sample 1",
        "summary": "First line Second line",
        "source_name": "Colon tissue",
        "organism": "Homo sapiens",
        "characteristics": {
            "disease": "Crohn disease",
            "tissue": "colon",
        },
        "platform": "GPL24676",
    }


def test_normalize_sample_handles_missing_fields():
    assert _normalize_sample({}) == {
        "gsm_id": None,
        "title": None,
        "summary": None,
        "source_name": None,
        "organism": None,
        "characteristics": {},
        "platform": None,
    }


def test_parse_soft_normalizes_series_and_samples():
    soft = """^SERIES = GSE123456
!Series_title = Human single-cell dataset
!Series_summary = First summary line
!Series_summary = Second summary line
!Series_overall_design = Single-cell RNA sequencing
!Series_organism = Homo sapiens
!Series_platform_id = GPL24676
!Series_sample_id = GSM100
!Series_sample_id = GSM200
^SAMPLE = GSM200
!Sample_title = Sample 200
!Sample_summary = Sample summary
!Sample_source_name_ch1 = Colon tissue
!Sample_organism_ch1 = Homo sapiens
!Sample_characteristics_ch1 = disease: Crohn disease
!Sample_characteristics_ch1 = tissue: colon
!Sample_platform_id = GPL24676
^SAMPLE = GSM100
!Sample_title = Sample 100
!Sample_characteristics_ch1 = tissue: rectum
"""

    result = _parse_soft(soft, "GSE123456")

    assert result["gse"] == {
        "gse_id": "GSE123456",
        "title": "Human single-cell dataset",
        "summary": "First summary line Second summary line",
        "overall_design": "Single-cell RNA sequencing",
        "organism": "Homo sapiens",
        "taxon": "Homo sapiens",
        "platform": "GPL24676",
        "n_samples": 2,
    }

    assert [sample["gsm_id"] for sample in result["samples"]] == [
        "GSM100",
        "GSM200",
    ]

    assert result["samples"][0]["characteristics"] == {
        "tissue": "rectum",
    }

    assert result["samples"][1]["characteristics"] == {
        "disease": "Crohn disease",
        "tissue": "colon",
    }


def test_parse_soft_raises_when_no_samples_found():
    soft = "^SERIES = GSE123456\n!Series_title = No samples\n"

    with pytest.raises(ValueError, match="No GSM sample records"):
        _parse_soft(soft, "GSE123456")


def test_parse_soft_keeps_samples_not_listed_in_series_sample_ids():
    soft = """^SERIES = GSE123456
!Series_sample_id = GSM100
^SAMPLE = GSM100
!Sample_title = Listed sample
^SAMPLE = GSM200
!Sample_title = Unlisted sample
"""

    result = _parse_soft(soft, "GSE123456")

    assert [sample["gsm_id"] for sample in result["samples"]] == [
        "GSM100",
        "GSM200",
    ]


def test_parse_soft_handles_repeated_sample_characteristics():
    soft = """^SERIES = GSE123456
^SAMPLE = GSM100
!Sample_characteristics_ch1 = disease: Crohn disease
!Sample_characteristics_ch1 = disease: ulcerative colitis
"""

    result = _parse_soft(soft, "GSE123456")

    assert result["samples"][0]["characteristics"] == {
        "disease": ["Crohn disease", "ulcerative colitis"],
    }


def test_get_geo_metadata_fetches_and_parses_gzipped_soft(monkeypatch):
    soft = b"""^SERIES = GSE123456
!Series_title = Example
^SAMPLE = GSM123456
!Sample_title = Sample 1
"""

    response = Mock()
    response.content = gzip.compress(soft)
    response.raise_for_status.return_value = None

    get_mock = Mock(return_value=response)

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get",
        get_mock,
    )

    result = get_geo_metadata("gse123456", timeout=12.0)

    assert result["gse"]["gse_id"] == "GSE123456"
    assert result["gse"]["n_samples"] == 1
    assert result["samples"][0]["gsm_id"] == "GSM123456"

    get_mock.assert_called_once()

    args, kwargs = get_mock.call_args

    assert args[0] == _get_soft_url("GSE123456")
    assert kwargs["timeout"] == 12.0
    assert kwargs["headers"]["User-Agent"] == ("hermes-work/sc-dataset-qualification")


@pytest.mark.parametrize(
    "accession",
    ["", "GSM123456", "GSEabc", "123"],
)
def test_get_geo_metadata_rejects_invalid_accession_before_request(
    monkeypatch,
    accession,
):
    get_mock = Mock()

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get",
        get_mock,
    )

    with pytest.raises(ValueError, match="Invalid GEO Series accession"):
        get_geo_metadata(accession)

    get_mock.assert_not_called()


def test_get_geo_metadata_rejects_invalid_gzip(monkeypatch):
    response = Mock()
    response.content = b"not gzip"
    response.raise_for_status.return_value = None

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get",
        Mock(return_value=response),
    )

    with pytest.raises(ValueError, match="not a valid gzip file"):
        get_geo_metadata("GSE123456")


def test_get_geo_metadata_propagates_http_errors(monkeypatch):
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("failed")

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get",
        Mock(return_value=response),
    )

    with pytest.raises(requests.HTTPError, match="failed"):
        get_geo_metadata("GSE123456")


def test_get_geo_metadata_rejects_valid_gzip_without_samples(monkeypatch):
    soft = b"^SERIES = GSE123456\n!Series_title = No samples\n"

    response = Mock()
    response.content = gzip.compress(soft)
    response.raise_for_status.return_value = None

    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.requests.get",
        Mock(return_value=response),
    )

    with pytest.raises(ValueError, match="No GSM sample records"):
        get_geo_metadata("GSE123456")


def test_cli_outputs_json(monkeypatch):
    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.get_geo_metadata",
        lambda accession, timeout: {
            "gse": {"gse_id": accession},
            "samples": [],
        },
    )

    result = CliRunner().invoke(main, ["GSE123456", "--pretty"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "gse": {"gse_id": "GSE123456"},
        "samples": [],
    }


def test_cli_converts_expected_errors_to_click_exception(monkeypatch):
    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.get_geo_metadata",
        Mock(side_effect=ValueError("bad accession")),
    )

    result = CliRunner().invoke(main, ["GSE123456"])

    assert result.exit_code != 0
    assert "bad accession" in result.output


def test_cli_converts_http_errors_to_click_exception(monkeypatch):
    monkeypatch.setattr(
        "tools.sc_dataset.geo_metadata.get_geo_metadata",
        Mock(side_effect=requests.HTTPError("network failure")),
    )

    result = CliRunner().invoke(main, ["GSE123456"])

    assert result.exit_code != 0
    assert "network failure" in result.output
