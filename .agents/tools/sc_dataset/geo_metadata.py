from __future__ import annotations

import json
from typing import Any

import click
import requests


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    response = session.get(
        url,
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _find_geo_id(
    accession: str,
    *,
    timeout: float,
    session: requests.Session,
) -> str:
    """Resolve a GEO accession to an NCBI GEO UID."""

    data = _request_json(
        session,
        ESEARCH_URL,
        params={
            "db": "gds",
            "term": f"{accession}[Accession]",
            "retmax": 1,
            "retmode": "json",
        },
        timeout=timeout,
    )

    ids = data.get("esearchresult", {}).get("idlist", [])

    if not ids:
        raise ValueError(f"GEO accession not found: {accession}")

    return ids[0]


def _fetch_summary(
    geo_id: str,
    *,
    timeout: float,
    session: requests.Session,
) -> dict[str, Any]:
    """Fetch a GEO summary record."""

    data = _request_json(
        session,
        ESUMMARY_URL,
        params={
            "db": "gds",
            "id": geo_id,
            "retmode": "json",
        },
        timeout=timeout,
    )

    result = data.get("result", {})

    record = result.get(geo_id)

    if not isinstance(record, dict):
        raise ValueError(
            f"No GEO summary returned for GEO ID: {geo_id}"
        )

    return record


def _extract_gsm_ids(record: dict[str, Any]) -> list[str]:
    """Extract GSM accessions from a GEO summary record."""

    raw_samples = record.get("samples")

    if not raw_samples:
        return []

    if isinstance(raw_samples, list):
        return [
            sample
            for sample in raw_samples
            if isinstance(sample, str)
            and sample.startswith("GSM")
        ]

    return []


def _normalize_series(
    record: dict[str, Any],
    accession: str,
) -> dict[str, Any]:
    """Normalize a GEO Series summary."""

    return {
        "gse_id": accession,
        "title": record.get("title"),
        "summary": record.get("summary"),
        "overall_design": record.get("overall_design"),
        "organism": record.get("organism"),
        "taxon": record.get("taxon"),
        "platform": record.get("gpl"),
        "n_samples": record.get("n_samples"),
        "samples": _extract_gsm_ids(record),
    }


def _normalize_sample(
    record: dict[str, Any],
    accession: str,
) -> dict[str, Any]:
    """Normalize a GEO sample summary."""

    return {
        "gsm_id": accession,
        "title": record.get("title"),
        "summary": record.get("summary"),
        "source_name": record.get("source_name"),
        "organism": record.get("organism"),
        "characteristics": record.get("characteristics"),
        "platform": record.get("gpl"),
    }


def _fetch_sample_summaries(
    gsm_ids: list[str],
    *,
    timeout: float,
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Fetch GSM summaries."""

    if not gsm_ids:
        return []

    data = _request_json(
        session,
        ESUMMARY_URL,
        params={
            "db": "gds",
            "id": ",".join(gsm_ids),
            "retmode": "json",
        },
        timeout=timeout,
    )

    result = data.get("result", {})

    samples: list[dict[str, Any]] = []

    for gsm_id in gsm_ids:
        record = result.get(gsm_id)

        if not isinstance(record, dict):
            continue

        samples.append(
            _normalize_sample(
                record,
                gsm_id,
            )
        )

    return samples


def get_geo_metadata(
    accession: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Retrieve normalized metadata for a GEO Series."""

    accession = accession.strip().upper()

    if not accession.startswith("GSE"):
        raise ValueError(
            f"Expected a GSE accession, got: {accession}"
        )

    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": "hermes-work/sc-dataset-qualification",
            }
        )

        geo_id = _find_geo_id(
            accession,
            timeout=timeout,
            session=session,
        )

        series_record = _fetch_summary(
            geo_id,
            timeout=timeout,
            session=session,
        )

        series = _normalize_series(
            series_record,
            accession,
        )

        gsm_ids = series.pop("samples")

        samples = _fetch_sample_summaries(
            gsm_ids,
            timeout=timeout,
            session=session,
        )

        return {
            "gse": series,
            "samples": samples,
        }


@click.command()
@click.argument("accession")
@click.option(
    "--timeout",
    default=30.0,
    show_default=True,
    type=float,
    help="HTTP request timeout in seconds.",
)
@click.option(
    "--pretty",
    is_flag=True,
    help="Pretty-print JSON output.",
)
def main(
    accession: str,
    timeout: float,
    pretty: bool,
) -> None:
    """Retrieve metadata for a GEO Series."""

    metadata = get_geo_metadata(
        accession,
        timeout=timeout,
    )

    click.echo(
        json.dumps(
            metadata,
            indent=2 if pretty else None,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()