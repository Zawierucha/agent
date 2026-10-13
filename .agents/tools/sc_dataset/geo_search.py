from __future__ import annotations

import json
import sys
from typing import Any

import click
import requests


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def _search_geo_ids(
    query: str,
    *,
    max_results: int,
    timeout: float,
    session: requests.Session,
) -> list[str]:
    """Search NCBI GEO and return GEO record IDs."""

    response = session.get(
        ESEARCH_URL,
        params={
            "db": "gds",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
        },
        timeout=timeout,
    )
    response.raise_for_status()

    data = response.json()

    return data["esearchresult"]["idlist"]


def _fetch_geo_records(
    ids: list[str],
    *,
    timeout: float,
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Fetch GEO summaries for NCBI GEO record IDs."""

    if not ids:
        return []

    response = session.get(
        ESUMMARY_URL,
        params={
            "db": "gds",
            "id": ",".join(ids),
            "retmode": "json",
        },
        timeout=timeout,
    )
    response.raise_for_status()

    data = response.json()
    result = data["result"]

    records: list[dict[str, Any]] = []

    for record_id in result.get("uids", []):
        record = result.get(record_id)

        if not isinstance(record, dict):
            continue

        records.append(record)

    return records


def _extract_gse(record: dict[str, Any]) -> str | None:
    """Extract a GSE accession from a GEO summary record."""

    accession = record.get("accession")

    if isinstance(accession, str) and accession.startswith("GSE"):
        return accession

    title = record.get("title")

    if isinstance(title, str):
        for token in title.split():
            if token.startswith("GSE") and token[3:].isdigit():
                return token.rstrip(",.;:)")

    return None


def _normalize_record(
    record: dict[str, Any],
    *,
    query: str,
) -> dict[str, Any]:
    """Convert an NCBI GEO record into the tool output schema."""

    gse_id = _extract_gse(record)

    return {
        "gse_id": gse_id,
        "title": record.get("title"),
        "summary": record.get("summary"),
        "taxon": record.get("taxon"),
        "gds_type": record.get("gdstype"),
        "organism": record.get("organism"),
        "platform": record.get("gpl"),
        "n_samples": record.get("n_samples"),
        "query": query,
    }


def _deduplicate_gses(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate GEO records by GSE accession."""

    deduplicated: dict[str, dict[str, Any]] = {}

    for record in records:
        gse_id = record.get("gse_id")

        if not gse_id:
            continue

        if gse_id not in deduplicated:
            deduplicated[gse_id] = record
            continue

        # Preserve the query that discovered the GSE.
        existing = deduplicated[gse_id]

        existing_query = existing.get("query")
        new_query = record.get("query")

        if existing_query and new_query and new_query != existing_query:
            existing["query"] = f"{existing_query}; {new_query}"

    return list(deduplicated.values())


def search_geo(
    queries: list[str],
    *,
    max_results_per_query: int = 100,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Search NCBI GEO using arbitrary queries.

    This function contains no domain-specific search terminology.
    The caller is responsible for constructing the queries.
    """

    if not queries:
        return []

    cleaned_queries = list(
        dict.fromkeys(
            query.strip()
            for query in queries
            if isinstance(query, str) and query.strip()
        )
    )

    if not cleaned_queries:
        return []

    all_records: list[dict[str, Any]] = []

    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": "hermes-work/sc-dataset-search",
            }
        )

        for query in cleaned_queries:
            ids = _search_geo_ids(
                query,
                max_results=max_results_per_query,
                timeout=timeout,
                session=session,
            )

            records = _fetch_geo_records(
                ids,
                timeout=timeout,
                session=session,
            )

            all_records.extend(
                _normalize_record(
                    record,
                    query=query,
                )
                for record in records
            )

    return _deduplicate_gses(all_records)


@click.command()
@click.option(
    "--query",
    "queries",
    multiple=True,
    required=True,
    help="GEO search query. Can be specified multiple times.",
)
@click.option(
    "--max-results",
    default=100,
    show_default=True,
    type=click.IntRange(min=1),
    help="Maximum number of results per query.",
)
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
    queries: tuple[str, ...],
    max_results: int,
    timeout: float,
    pretty: bool,
) -> None:
    """Search NCBI GEO using arbitrary search queries."""

    results = search_geo(
        list(queries),
        max_results_per_query=max_results,
        timeout=timeout,
    )

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