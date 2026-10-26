from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import duckdb
import requests
from bs4 import BeautifulSoup

VERSION = "0.2.0"
DEFAULT_DB = Path("data/geo.duckdb")
DEFAULT_ROOT = Path("data/downloads")
USER_AGENT = f"sc-dataset-download/{VERSION}"


def table_exists(connection: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchone()[0])


def columns(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [row[0] for row in connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchall()]


def rows_for(connection: duckdb.DuckDBPyConnection, table: str, column: str, value: str) -> list[dict[str, Any]]:
    if not table_exists(connection, table):
        return []
    names = columns(connection, table)
    if column not in names:
        return []
    quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in names)
    rows = connection.execute(
        f'SELECT {quoted} FROM "{table}" WHERE "{column}"=? ORDER BY 1',
        [value],
    ).fetchall()
    return [dict(zip(names, row)) for row in rows]


def eligible_gses(connection: duckdb.DuckDBPyConnection) -> list[str]:
    required = {"geo_datasets", "geo_qualification", "geo_samples"}
    missing = sorted(table for table in required if not table_exists(connection, table))
    if missing:
        raise RuntimeError(f"Required catalog tables are missing: {', '.join(missing)}")
    qcols = set(columns(connection, "geo_qualification"))
    if not {"gse_id", "qualification"}.issubset(qcols):
        raise RuntimeError("geo_qualification must contain gse_id and qualification")
    query = """
        SELECT d.gse_id
        FROM geo_datasets d
        JOIN geo_qualification q USING (gse_id)
        WHERE lower(q.qualification)='include'
        ORDER BY d.gse_id
    """
    return [row[0] for row in connection.execute(query).fetchall()]


def catalog_snapshot(db_path: Path, gse_id: str) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"DuckDB catalog does not exist: {db_path}")
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        dataset_rows = rows_for(connection, "geo_datasets", "gse_id", gse_id)
        qualification_rows = rows_for(connection, "geo_qualification", "gse_id", gse_id)
        sample_rows = rows_for(connection, "geo_samples", "gse_id", gse_id)
        sample_qualifications = []
        if table_exists(connection, "geo_sample_qualification"):
            sample_qualifications = [dict(zip(
                ["gsm_id", "qualification", "tissue_source", "disease_status", "modality_evidence", "evidence", "uncertainties", "qualified_at"], row
            )) for row in connection.execute("""
                SELECT q.gsm_id, q.qualification, q.tissue_source, q.disease_status,
                       q.modality_evidence, q.evidence, q.uncertainties, q.qualified_at
                FROM geo_sample_qualification q
                JOIN geo_samples s USING (gsm_id)
                WHERE s.gse_id=? ORDER BY q.gsm_id
            """, [gse_id]).fetchall()]
    finally:
        connection.close()
    if not dataset_rows:
        raise ValueError(f"{gse_id} is not present in geo_datasets")
    qualification = qualification_rows[0] if qualification_rows else None
    decision = str(qualification.get("qualification", "")).lower() if qualification else ""
    if decision != "include":
        raise ValueError(f"{gse_id} has stored qualification {decision or 'missing'}; downloads require include")
    sample_by_id = {row["gsm_id"]: row for row in sample_qualifications}
    eligible_gsm_ids = sorted(gsm for gsm in (row.get("gsm_id") for row in sample_rows)
                              if gsm and sample_by_id.get(gsm, {}).get("qualification") == "include")
    return {
        "dataset": dataset_rows[0],
        "qualification": qualification,
        "samples": sample_rows,
        "gsm_ids": sorted({row["gsm_id"] for row in sample_rows if row.get("gsm_id")}),
        "sample_qualifications": sample_by_id,
        "eligible_gsm_ids": eligible_gsm_ids,
    }


def supplementary_url(gse_id: str) -> str:
    match = re.fullmatch(r"GSE(\d+)", gse_id.upper())
    if not match:
        raise ValueError(f"Invalid GEO series accession: {gse_id}")
    digits = match.group(1)
    bucket = f"GSE{digits[:-3]}nnn" if len(digits) > 3 else "GSEnnn"
    return f"https://ftp.ncbi.nlm.nih.gov/geo/series/{bucket}/{gse_id.upper()}/suppl/"


def list_files(gse_id: str, timeout: float) -> tuple[str, list[dict[str, Any]]]:
    listing_url = supplementary_url(gse_id)
    response = requests.get(listing_url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    if response.status_code == 404:
        return listing_url, []
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    files = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href in {"../", "./"} or href.startswith("?"):
            continue
        url = requests.compat.urljoin(listing_url, href)
        parsed = urlparse(url)
        filename = unquote(parsed.path.rstrip("/").split("/")[-1])
        if not filename or filename in {".", ".."} or filename in seen:
            continue
        if parsed.netloc != "ftp.ncbi.nlm.nih.gov":
            continue
        if not parsed.path.startswith(urlparse(listing_url).path):
            continue
        if filename.startswith(".") or "/" in filename or "\\" in filename:
            continue
        seen.add(filename)
        size = None
        row = anchor.find_parent("tr")
        if row:
            for cell in row.find_all(["td", "th"]):
                raw = cell.get_text(" ", strip=True).replace(",", "")
                if raw.isdigit():
                    size = int(raw)
                    break
        files.append({"filename": filename, "source_url": url, "listed_size_bytes": size})
    return listing_url, sorted(files, key=lambda item: item["filename"].lower())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(name: str) -> str:
    if name in {"", ".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError(f"Unsafe filename returned by GEO: {name!r}")
    return name


def download_one(item: dict[str, Any], target_dir: Path, timeout: float, retries: int) -> dict[str, Any]:
    filename = safe_filename(item["filename"])
    target = target_dir / filename
    part = target_dir / f".{filename}.part"
    result = {
        **item,
        "local_relative_path": str(Path("supplementary") / filename),
        "download_status": "not_downloaded",
        "size_bytes": None,
        "sha256": None,
        "attempts": 0,
        "http_status": None,
        "error": None,
        "gsm_mapping_status": "series_level_or_unknown",
    }
    if target.exists() and target.is_file():
        result["size_bytes"] = target.stat().st_size
        result["sha256"] = sha256_file(target)
        result["download_status"] = "already_verified"
        return result
    for attempt in range(1, retries + 2):
        result["attempts"] = attempt
        try:
            with requests.get(item["source_url"], stream=True, timeout=timeout,
                              headers={"User-Agent": USER_AGENT}) as response:
                result["http_status"] = response.status_code
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    response.raise_for_status()
                response.raise_for_status()
                expected = response.headers.get("Content-Length")
                expected_size = int(expected) if expected and expected.isdigit() else item.get("listed_size_bytes")
                written = 0
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
                if expected_size is not None and written != expected_size:
                    raise IOError(f"Downloaded {written} bytes; expected {expected_size}")
                os.replace(part, target)
                result["size_bytes"] = written
                result["sha256"] = sha256_file(target)
                result["download_status"] = "downloaded"
                return result
        except (requests.RequestException, OSError, ValueError) as exc:
            result["error"] = str(exc)
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt <= retries:
                time.sleep(min(2 ** (attempt - 1), 30))
    result["download_status"] = "failed"
    return result


def run_gse(db_path: Path, root: Path, gse_id: str, timeout: float,
            retries: int, download: bool) -> dict[str, Any]:
    gse_id = gse_id.upper()
    snapshot = catalog_snapshot(db_path, gse_id)
    listing_url, source_files = list_files(gse_id, timeout)
    dataset_dir = root / gse_id
    supplementary_dir = dataset_dir / "supplementary"
    manifest_path = dataset_dir / "manifest.json"
    records = []
    eligible_ids = set(snapshot["eligible_gsm_ids"])
    sample_decisions = snapshot["sample_qualifications"]
    if download:
        supplementary_dir.mkdir(parents=True, exist_ok=True)
    for item in source_files:
        explicit_gsms = set(re.findall(r"GSM\d+", item["filename"], flags=re.IGNORECASE))
        explicitly_mapped = bool(explicit_gsms)
        excluded_gsms = sorted(explicit_gsms - eligible_ids) if explicitly_mapped else []
        if explicitly_mapped and not (explicit_gsms & eligible_ids):
            records.append({
                **item,
                "local_relative_path": None,
                "download_status": "not_downloaded",
                "size_bytes": item.get("listed_size_bytes"),
                "sha256": None,
                "attempts": 0,
                "http_status": None,
                "error": "Skipped: filename maps only to GSM samples not qualified as include.",
                "gsm_mapping_status": "explicit_gsm_mapping_excluded",
                "gsm_ids": sorted(explicit_gsms),
            })
        elif download:
            records.append(download_one(item, supplementary_dir, timeout, retries))
            records[-1]["gsm_mapping_status"] = "explicit_gsm_mapping" if explicitly_mapped else "series_level_or_unknown"
            records[-1]["gsm_ids"] = sorted(explicit_gsms) if explicitly_mapped else []
        else:
            records.append({
                **item,
                "local_relative_path": str(Path("supplementary") / safe_filename(item["filename"])),
                "download_status": "not_downloaded",
                "size_bytes": item.get("listed_size_bytes"),
                "sha256": None,
                "attempts": 0,
                "http_status": None,
                "error": None,
                "gsm_mapping_status": "explicit_gsm_mapping" if explicitly_mapped else "series_level_or_unknown",
                "gsm_ids": sorted(explicit_gsms) if explicitly_mapped else [],
            })
    manifest = {
        "manifest_version": "1.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "gse_id": gse_id,
        "stored_dataset_qualification": snapshot["qualification"],
        "dataset_metadata": snapshot["dataset"],
        "catalogued_gsm_ids": snapshot["gsm_ids"],
        "catalogued_sample_count": len(snapshot["gsm_ids"]),
        "eligible_gsm_ids": snapshot["eligible_gsm_ids"],
        "eligible_sample_count": len(snapshot["eligible_gsm_ids"]),
        "sample_qualification": snapshot["sample_qualifications"],
        "source_listing_url": listing_url,
        "files": records,
        "warnings": ([] if source_files else ["No supplementary files were listed by GEO for this series."]) +
                    (["Some GSMs have no stored sample-level decision; no eligibility was inferred for them."]
                     if set(snapshot["gsm_ids"]) - set(snapshot["sample_qualifications"]) else []) +
                    (["Some downloaded GSM-mapped files may contain more than one sample; file-level selection does not subset file contents."]
                     if any(item.get("gsm_mapping_status") == "explicit_gsm_mapping" and
                            set(item.get("gsm_ids", [])) - eligible_ids for item in records) else []),
    }
    if download:
        dataset_dir.mkdir(parents=True, exist_ok=True)
        temporary_manifest = manifest_path.with_suffix(".json.part")
        temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(temporary_manifest, manifest_path)
    failures = sum(item["download_status"] == "failed" for item in records)
    return {
        "gse_id": gse_id,
        "qualification": snapshot["qualification"].get("qualification"),
        "catalogued_sample_count": len(snapshot["gsm_ids"]),
        "eligible_sample_count": len(snapshot["eligible_gsm_ids"]),
        "source_file_count": len(source_files),
        "downloaded": sum(item["download_status"] == "downloaded" for item in records),
        "already_verified": sum(item["download_status"] == "already_verified" for item in records),
        "failed": failures,
        "manifest_path": str(manifest_path) if download else None,
        "dry_run": not download,
        "files": records,
        "warnings": manifest["warnings"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Download supplementary files for already-qualified GEO series")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--download-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--gse", action="append", dest="gse_ids")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        if args.limit < 1 or args.retries < 0:
            raise ValueError("--limit must be >= 1 and --retries must be >= 0")
        if args.download and args.dry_run:
            raise ValueError("Choose either --download or --dry-run")
        if not args.download:
            args.dry_run = True
        if args.gse_ids:
            selected = [g.upper() for g in args.gse_ids]
            if len(selected) > args.limit:
                raise ValueError("Number of --gse arguments exceeds --limit")
        else:
            connection = duckdb.connect(str(args.db), read_only=True)
            try:
                selected = eligible_gses(connection)[:args.limit]
            finally:
                connection.close()
        results = [run_gse(args.db, args.download_root, gse, args.timeout, args.retries, args.download) for gse in selected]
        output = {"tool": "geo_download", "version": VERSION, "database_read_only": True,
                  "dry_run": not args.download, "results": results}
        json.dump(output, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None, default=str)
        sys.stdout.write("\n")
        return 1 if any(result["failed"] for result in results) else 0
    except (OSError, ValueError, RuntimeError, requests.RequestException, duckdb.Error) as exc:
        json.dump({"error": str(exc)}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
