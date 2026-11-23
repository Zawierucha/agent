from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import click
import requests
from tools.sc_dataset.geo_envelope import (
    derive_search_status,
    envelope_skeleton,
    new_run_id,
    utc_now,
    write_envelope_atomic,
)

TOOL_VERSION = "1.1.0"

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}


class SearchRequestError(RuntimeError):
    """Raised when a request fails after bounded retries."""


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None

    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (OSError, TypeError, ValueError, OverflowError):
            return None


def _get_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    timeout: float,
    max_attempts: int = 4,
    sleep=time.sleep,
) -> Any:
    """GET JSON with bounded retries for transient NCBI/request failures."""

    last_status: int | None = None
    last_error: (
        requests.exceptions.ConnectionError | requests.exceptions.Timeout | None
    ) = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            last_error = exc
            last_status = None
            if attempt == max_attempts:
                raise SearchRequestError(
                    f"Request to {url} failed after {max_attempts} attempts; "
                    f"last error: {type(exc).__name__}: {exc}"
                ) from exc
            sleep(min(2 ** (attempt - 1), 30))
            continue

        status = response.status_code
        if status >= 400 and status not in RETRYABLE_STATUSES:
            raise requests.HTTPError(f"HTTP {status} for {url}", response=response)

        if status in RETRYABLE_STATUSES:
            last_status = status
            last_error = None
            if attempt == max_attempts:
                raise SearchRequestError(
                    f"Request to {url} failed after {max_attempts} attempts; "
                    f"last status: {status}"
                )
            delay = _retry_after_seconds(response.headers.get("Retry-After"))
            sleep(delay if delay is not None else min(2 ** (attempt - 1), 30))
            continue

        return response.json()

    # max_attempts is a public helper argument, but the normal path raises above.
    detail = (
        f"last status: {last_status}"
        if last_status is not None
        else f"last error: {type(last_error).__name__}"
    )
    raise SearchRequestError(
        f"Request to {url} failed after {max_attempts} attempts; {detail}"
    )


def _search_geo_ids(
    query: str,
    *,
    max_results: int,
    timeout: float,
    session: requests.Session,
    sleep=time.sleep,
) -> tuple[list[str], int | None]:
    """Search NCBI GEO and return record IDs and the reported count."""

    data = _get_json(
        session,
        ESEARCH_URL,
        params={
            "db": "gds",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
        },
        timeout=timeout,
        sleep=sleep,
    )
    esearchresult = data["esearchresult"]
    try:
        count = int(esearchresult["count"])
    except (KeyError, TypeError, ValueError):
        count = None

    return esearchresult["idlist"], count


def _fetch_geo_records(
    ids: list[str],
    *,
    timeout: float,
    session: requests.Session,
    sleep=time.sleep,
) -> list[dict[str, Any]]:
    """Fetch GEO summaries for NCBI GEO record IDs."""

    if not ids:
        return []

    data = _get_json(
        session,
        ESUMMARY_URL,
        params={
            "db": "gds",
            "id": ",".join(ids),
            "retmode": "json",
        },
        timeout=timeout,
        sleep=sleep,
    )
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
            token = token.rstrip(",.;:)")
            if token.startswith("GSE") and token[3:].isascii() and token[3:].isdigit():
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
    sleep=time.sleep,
) -> dict[str, Any]:
    """Search NCBI GEO and return per-query reports and deduplicated records."""

    cleaned_queries = list(
        dict.fromkeys(
            query.strip()
            for query in queries
            if isinstance(query, str) and query.strip()
        )
    )
    if not cleaned_queries:
        return {"queries": [], "records": [], "dropped_no_gse": 0}

    reports: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    dropped_no_gse = 0

    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": "hermes-work/sc-dataset-search",
            }
        )

        for query in cleaned_queries:
            count: int | None = None
            ids: list[str] | None = None
            try:
                ids, count = _search_geo_ids(
                    query,
                    max_results=max_results_per_query,
                    timeout=timeout,
                    session=session,
                    sleep=sleep,
                )
                raw_records = (
                    _fetch_geo_records(
                        ids,
                        timeout=timeout,
                        session=session,
                        sleep=sleep,
                    )
                    if ids
                    else []
                )
                normalized = [
                    _normalize_record(record, query=query) for record in raw_records
                ]
                dropped_no_gse += sum(not record["gse_id"] for record in normalized)
                all_records.extend(normalized)
                retrieved = len(ids)
                status = (
                    "partial" if count is not None and count > retrieved else "success"
                )
                error = None
            except (
                SearchRequestError,
                requests.HTTPError,
                requests.RequestException,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                status = "failed"
                retrieved = len(ids) if ids is not None else 0
                error = f"{type(exc).__name__}: {exc}"

            reports.append(
                {
                    "query": query,
                    "status": status,
                    "count": count,
                    "retrieved": retrieved,
                    "error": error,
                }
            )

    return {
        "queries": reports,
        "records": _deduplicate_gses(all_records),
        "dropped_no_gse": dropped_no_gse,
    }


@click.command()
@click.option("--run-id", default=None, help="Execution envelope run identifier.")
@click.option("--output-envelope", type=click.Path(path_type=Path), default=None, help="Write an execution envelope JSON file.")
@click.option("--skill-version", default=None, help="Search skill version (or SC_DATASET_SEARCH_VERSION).")
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
    type=click.IntRange(min=1, max=10000),
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
    run_id: str | None,
    output_envelope: Path | None,
    skill_version: str | None,
    queries: tuple[str, ...],
    max_results: int,
    timeout: float,
    pretty: bool,
) -> None:
    """Search NCBI GEO using arbitrary search queries."""

    if output_envelope is None:
        results = search_geo(
            list(queries),
            max_results_per_query=max_results,
            timeout=timeout,
        )
    else:
        cleaned = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
        query_reports: dict[str, dict[str, Any]] = {}
        all_records: list[dict[str, Any]] = []
        dropped_no_gse = 0
        reports: list[dict[str, Any]] = []
        aborted = False
        if not cleaned:
            results = {"queries": [], "records": [], "dropped_no_gse": 0}
        for index, query in enumerate(cleaned, 1):
            try:
                partial = search_geo(
                    [query],
                    max_results_per_query=max_results,
                    timeout=timeout,
                )
                row = partial["queries"][0] if partial.get("queries") else {"query": query, "status": "success", "count": 0, "retrieved": 0, "error": None}
                query_reports[query] = row
                if row.get("status") == "not_run":
                    aborted = True
                    break
                reports.append(row)
                all_records.extend(partial["records"])
                dropped_no_gse += partial["dropped_no_gse"]
            except (SearchRequestError, requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
                row = {"query": query, "status": "failed", "count": None, "retrieved": 0, "error": f"{type(exc).__name__}: {exc}"}
                query_reports[query] = row
                reports.append(row)
                for remaining in cleaned[index:]:
                    query_reports[remaining] = {"query": remaining, "status": "not_run"}
                break
        results = {"queries": reports, "records": _deduplicate_gses(all_records), "dropped_no_gse": dropped_no_gse}
    if output_envelope is not None:
        actual_run_id = run_id or new_run_id()
        if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", actual_run_id):
            raise click.BadParameter("run_id must match YYYYMMDDTHHMMSSZ-<8 lowercase hex>", param_hint="--run-id")
        actual_skill_version = skill_version or os.environ.get("SC_DATASET_SEARCH_VERSION", "unknown")
        params = {"queries": [{"query_id": f"q{index}", "query": query, "max_results": max_results} for index, query in enumerate(cleaned, 1)], "species_constraint": "Homo sapiens [Organism]"}
        limits = {"retstart_pagination": False, "max_results_per_query": max_results}
        env = envelope_skeleton(actual_run_id, utc_now(), {"name": "sc-dataset-search", "version": actual_skill_version}, "geo_search.py", TOOL_VERSION, params, limits)
        per_query = []
        errors = []
        for planned in params["queries"]:
            row = query_reports.get(planned["query"])
            if row is not None and row.get("status") == "not_run":
                aborted = True
                row = None
            if row is None or aborted:
                per_query.append({"query_id": planned["query_id"], "status": "not_run", "count": None, "retrieved": 0, "error": "run aborted before this query", "substituted_query": None, "attempt": 1})
            else:
                per_query.append({"query_id": planned["query_id"], "status": row["status"], "count": row["count"], "retrieved": row["retrieved"], "error": row["error"], "substituted_query": None, "attempt": 1})
                if row.get("error"):
                    errors.append({"query_id": planned["query_id"], "message": row["error"]})
        status = derive_search_status([q["query_id"] for q in params["queries"]], per_query)
        if status == "FAILED" and (not per_query or all(row["status"] == "not_run" for row in per_query)):
            errors.append({"code": "no_queries_attempted", "message": "no planned query was attempted"})
        gse_ids = [record["gse_id"] for record in results.get("records", []) if isinstance(record.get("gse_id"), str)]
        env["search"].update({"status": status, "per_query": per_query, "results": {"unique_gse_count": len(set(gse_ids)), "gse_ids": list(dict.fromkeys(gse_ids)), "dropped_no_gse": results.get("dropped_no_gse", 0)}, "errors": errors, "finished_at_utc": utc_now()})
        write_envelope_atomic(output_envelope, env)
        results["run_id"] = actual_run_id
        results["envelope_path"] = str(output_envelope)

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
