#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from typing import Any

import click
import requests


NCBI_EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

DEFAULT_TIMEOUT = 30.0

IBD_QUERIES = [
    "inflammatory bowel disease",
    "IBD",
    "Crohn disease",
    "Crohn's disease",
    "ulcerative colitis",
    "ulcerative colitis single cell",
    "Crohn disease single cell",
    "inflammatory bowel disease single cell",
]


@dataclass
class GEOCandidate:
    """Candidate GEO Series dataset."""

    gse_id: str
    accession: str
    title: str | None
    summary: str | None
    organism: str | None
    source: str = "NCBI GEO"

    def to_dict(self) -> dict[str, Any]:
        """Convert the candidate to a dictionary."""
        return asdict(self)


def _get(
    endpoint: str,
    params: dict[str, Any],
    *,
    timeout: float,
) -> requests.Response:
    """Perform an HTTP GET request against NCBI E-utilities."""

    response = requests.get(
        f"{NCBI_EUTILS_URL}/{endpoint}",
        params=params,
        timeout=timeout,
        headers={
            "User-Agent": "sc-dataset-search/0.1.0",
        },
    )

    response.raise_for_status()

    return response


def search_geo(
    query: str,
    *,
    max_results: int = 100,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """
    Search NCBI GEO for a single query.

    Parameters
    ----------
    query:
        NCBI GEO search query.

    max_results:
        Maximum number of GEO records to retrieve.

    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    list[dict[str, Any]]
        GEO candidate datasets.
    """

    response = _get(
        "esearch.fcgi",
        {
            "db": "gds",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
        },
        timeout=timeout,
    )

    payload = response.json()

    search_result = payload.get("esearchresult", {})
    ids = search_result.get("idlist", [])

    if not ids:
        return []

    return _fetch_geo_records(
        ids,
        timeout=timeout,
    )


def _fetch_geo_records(
    ids: list[str],
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    """
    Retrieve GEO metadata for GEO database IDs.

    The GEO `gds` database contains several record types.
    Only GSE accessions are retained.
    """

    response = _get(
        "esummary.fcgi",
        {
            "db": "gds",
            "id": ",".join(ids),
            "retmode": "json",
        },
        timeout=timeout,
    )

    payload = response.json()
    records = payload.get("result", {})

    candidates: list[dict[str, Any]] = []

    for geo_id in ids:
        record = records.get(geo_id)

        if not isinstance(record, dict):
            continue

        accession = record.get("accession")

        if not accession:
            continue

        if not accession.startswith("GSE"):
            continue

        candidate = GEOCandidate(
            gse_id=accession,
            accession=accession,
            title=record.get("title"),
            summary=record.get("summary"),
            organism=record.get("taxon"),
        )

        candidates.append(candidate.to_dict())

    return candidates


def search_ibd_sc_datasets(
    *,
    max_results_per_query: int = 100,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """
    Search GEO using the complete IBD candidate-discovery strategy.

    Each query is executed independently and duplicate GSE accessions
    are merged.

    This function performs candidate discovery only. It does not
    determine whether a dataset is truly related to IBD or whether
    it contains single-cell RNA-seq data.
    """

    datasets: dict[str, dict[str, Any]] = {}

    for query in IBD_QUERIES:
        candidates = search_geo(
            query,
            max_results=max_results_per_query,
            timeout=timeout,
        )

        for candidate in candidates:
            gse_id = candidate["gse_id"]

            if gse_id not in datasets:
                datasets[gse_id] = candidate

    return list(datasets.values())


@click.command()
@click.option(
    "--query",
    type=str,
    default=None,
    help="Run one custom GEO query.",
)
@click.option(
    "--ibd",
    is_flag=True,
    help="Run the complete IBD search strategy.",
)
@click.option(
    "--max-results",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Maximum number of results per query.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0.1),
    default=DEFAULT_TIMEOUT,
    show_default=True,
    help="HTTP timeout in seconds.",
)
@click.option(
    "--pretty",
    is_flag=True,
    help="Pretty-print JSON output.",
)
def main(
    query: str | None,
    ibd: bool,
    max_results: int,
    timeout: float,
    pretty: bool,
) -> None:
    """Search NCBI GEO for candidate IBD single-cell datasets."""

    if not query and not ibd:
        raise click.UsageError(
            "Specify either --query or --ibd."
        )

    if query and ibd:
        raise click.UsageError(
            "Specify either --query or --ibd, not both."
        )

    try:
        if query:
            results = search_geo(
                query,
                max_results=max_results,
                timeout=timeout,
            )
        else:
            results = search_ibd_sc_datasets(
                max_results_per_query=max_results,
                timeout=timeout,
            )

    except requests.RequestException as exc:
        raise click.ClickException(
            f"NCBI GEO request failed: {exc}"
        ) from exc

    except ValueError as exc:
        raise click.ClickException(
            f"Invalid response from NCBI GEO: {exc}"
        ) from exc

    if pretty:
        click.echo(
            json.dumps(
                results,
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        click.echo(
            json.dumps(
                results,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()