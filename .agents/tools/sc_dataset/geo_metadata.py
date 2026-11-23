from __future__ import annotations

import sys
from pathlib import Path

_TOOL_IMPORT_ROOT = Path(__file__).resolve().parents[2]
if not any((_candidate / 'tools').is_dir() for _candidate in map(Path, sys.path)):
    sys.path.insert(0, str(_TOOL_IMPORT_ROOT))


import codecs
import fcntl
import gzip
import hashlib
import io
import json
import os
import random
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import click
import requests

TOOL_VERSION = "1.0.0"
PARSER_CONTRACT = "soft-parser-v1"
GEO_FTP_URL = "https://ftp.ncbi.nlm.nih.gov/geo/series"


MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
DOWNLOAD_CHUNK_SIZE = 64 * 1024


class OversizedResponse(ValueError):
    def __init__(self, message: str = "GEO SOFT response exceeded configured size limits") -> None:
        self.error_class = "oversized_response"
        super().__init__(message)


def _limited_gunzip(
    path_or_bytes: Path | bytes,
    *,
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES,
    max_ratio: int = MAX_COMPRESSION_RATIO,
) -> str:
    """Decode gzip incrementally while enforcing absolute and ratio bounds."""
    import zlib

    text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    output: list[str] = []
    decompressed_total = 0
    compressed_total = 0
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    source = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else None
    with source if source is not None else Path(path_or_bytes).open("rb") as input_stream:
        while True:
            compressed = input_stream.read(DOWNLOAD_CHUNK_SIZE)
            if not compressed:
                break
            compressed_total += len(compressed)
            pending = compressed
            while pending:
                try:
                    decoded = decoder.decompress(pending, DOWNLOAD_CHUNK_SIZE)
                except zlib.error as exc:
                    raise gzip.BadGzipFile("Not a gzipped file") from exc
                if decoded:
                    decompressed_total += len(decoded)
                    if decompressed_total > max_decompressed_bytes:
                        raise OversizedResponse("GEO SOFT response exceeded decompressed byte limit")
                    if decompressed_total // max(1, compressed_total) > max_ratio:
                        raise OversizedResponse("GEO SOFT response exceeded compression ratio limit")
                    output.append(text_decoder.decode(decoded))
                if decoder.eof:
                    pending = decoder.unused_data
                    if pending:
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    else:
                        break
                elif decoder.unconsumed_tail:
                    pending = decoder.unconsumed_tail
                else:
                    break
        if not decoder.eof:
            raise EOFError("Compressed file ended before the end-of-stream marker was reached")
    output.append(text_decoder.decode(b"", final=True))
    return "".join(output)


def _get_soft_url(accession: str) -> str:
    match = re.fullmatch(r"GSE(\d+)", accession.upper())
    if not match:
        raise ValueError(f"Invalid GEO Series accession: {accession}")

    digits = match.group(1)
    series_accession = f"GSE{digits}"
    # GEO buckets group accessions by thousands: GSE9686 -> GSE9nnn, GSE10191 -> GSE10nnn.
    # Accessions below 1000 use the GSEnnn bucket.
    bucket_prefix = digits[:-3]
    series_bucket = f"GSE{bucket_prefix}nnn"

    return (
        f"{GEO_FTP_URL}/{series_bucket}/{series_accession}"
        f"/soft/{series_accession}_family.soft.gz"
    )


def _parse_soft_value(value: str) -> str:
    return value.strip().strip('"')


def _parse_characteristics(values: list[str]) -> dict[str, Any]:
    characteristics: dict[str, Any] = {}

    for value in values:
        value = _parse_soft_value(value)
        if not value:
            continue

        if ":" in value:
            key, item_value = value.split(":", 1)
            key = key.strip()
            item_value = item_value.strip()

            if key in characteristics:
                existing = characteristics[key]
                if isinstance(existing, list):
                    existing.append(item_value)
                else:
                    characteristics[key] = [existing, item_value]
            else:
                characteristics[key] = item_value
        else:
            characteristics.setdefault("unparsed", []).append(value)

    return characteristics


def _normalize_sample(sample: dict[str, Any]) -> dict[str, Any]:
    characteristics = _parse_characteristics(sample.get("characteristics", []))

    for key, value in characteristics.items():
        if isinstance(value, list):
            characteristics[key] = value

    return {
        "gsm_id": sample.get("gsm_id"),
        "title": sample.get("title"),
        "summary": " ".join(sample.get("summary", [])) or None,
        "source_name": " ".join(sample.get("source_name", [])) or None,
        "organism": " ".join(sample.get("organism", [])) or None,
        "characteristics": characteristics,
        "platform": sample.get("platform"),
    }


def _parse_soft(text: str, accession: str) -> dict[str, Any]:
    series: dict[str, Any] = {
        "gse_id": accession,
        "title": None,
        "summary": [],
        "overall_design": [],
        "organism": [],
        "taxon": None,
        "platform": None,
        "n_samples": 0,
    }

    samples: list[dict[str, Any]] = []
    current_sample: dict[str, Any] | None = None
    current_section: str | None = None

    series_sample_ids: list[str] = []

    series_fields = {
        "!Series_title": "title",
        "!Series_summary": "summary",
        "!Series_overall_design": "overall_design",
        "!Series_organism": "organism",
        "!Series_platform_id": "platform",
        "!Series_sample_id": "sample_id",
    }

    sample_fields = {
        "!Sample_title": "title",
        "!Sample_summary": "summary",
        "!Sample_source_name_ch1": "source_name",
        "!Sample_organism_ch1": "organism",
        "!Sample_characteristics_ch1": "characteristics",
        "!Sample_platform_id": "platform",
    }

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("^SERIES = "):
            current_section = "series"
            current_sample = None
            continue

        if line.startswith("^SAMPLE = "):
            current_section = "sample"
            gsm_id = line.split("=", 1)[1].strip()
            current_sample = {
                "gsm_id": gsm_id,
                "title": None,
                "summary": [],
                "source_name": [],
                "organism": [],
                "characteristics": [],
                "platform": None,
            }
            samples.append(current_sample)
            continue

        if line.startswith("^"):
            current_section = None
            current_sample = None
            continue

        if not line.startswith("!"):
            continue

        if current_section == "series":
            matched_key = next(
                (key for key in series_fields if line.startswith(f"{key} = ")),
                None,
            )
            if matched_key is None:
                continue

            value = _parse_soft_value(line.split("=", 1)[1])
            field = series_fields[matched_key]

            if field == "sample_id":
                if value.startswith("GSM"):
                    series_sample_ids.append(value)
            elif field in {"summary", "overall_design", "organism"}:
                series[field].append(value)
            else:
                series[field] = value

        elif current_section == "sample" and current_sample is not None:
            matched_key = next(
                (key for key in sample_fields if line.startswith(f"{key} = ")),
                None,
            )
            if matched_key is None:
                continue

            value = _parse_soft_value(line.split("=", 1)[1])
            field = sample_fields[matched_key]

            if field in {
                "summary",
                "source_name",
                "organism",
                "characteristics",
            }:
                current_sample[field].append(value)
            else:
                current_sample[field] = value

    if not samples:
        raise ValueError(
            f"No GSM sample records were found in GEO SOFT for {accession}"
        )

    if series_sample_ids:
        sample_by_id = {sample["gsm_id"]: sample for sample in samples}
        ordered_samples = [
            sample_by_id[gsm_id]
            for gsm_id in series_sample_ids
            if gsm_id in sample_by_id
        ]
        remaining_samples = [
            sample
            for sample in samples
            if sample["gsm_id"] not in set(series_sample_ids)
        ]
        samples = ordered_samples + remaining_samples

    series["summary"] = " ".join(series["summary"]) or None
    series["overall_design"] = " ".join(series["overall_design"]) or None
    series["organism"] = " ".join(series["organism"]) or None
    series["taxon"] = series["organism"]
    series["n_samples"] = len(samples)

    if isinstance(series["platform"], str):
        series["platform"] = series["platform"].strip() or None

    normalized_samples = [_normalize_sample(sample) for sample in samples]

    return {
        "gse": series,
        "samples": normalized_samples,
    }


def _classify(exc_or_status: BaseException | int) -> str:
    """Return the stable fetch error class for an exception or HTTP status."""
    error_class = getattr(exc_or_status, "error_class", None)
    if error_class:
        return str(error_class)
    if isinstance(exc_or_status, int):
        status = exc_or_status
    elif isinstance(exc_or_status, requests.HTTPError):
        response = exc_or_status.response
        status = getattr(response, "status_code", None)
    else:
        status = None
    if status == 404:
        return "not_found"
    if status == 403:
        return "forbidden"
    if status in {429, 500, 502, 503, 504}:
        return "transient_http"
    if isinstance(exc_or_status, (requests.ConnectionError, requests.Timeout)):
        return "transient_network"
    if isinstance(exc_or_status, (gzip.BadGzipFile, EOFError)):
        return "bad_response"
    if isinstance(exc_or_status, requests.HTTPError) and status and 400 <= status < 500:
        return "permanent_http"
    return "permanent"


class DeferredFetch(RuntimeError):
    def __init__(self, retry_after_seconds: float, attempts_so_far: int):
        self.retry_after_seconds = retry_after_seconds
        self.defer_until_utc = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        ).isoformat().replace("+00:00", "Z")
        self.attempts_so_far = attempts_so_far
        super().__init__(
            f"deferred_fetch: retry after {retry_after_seconds:g} seconds"
        )


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _classify_empty_soft(text: str) -> str:
    lines = text.splitlines()
    series_type = ""
    relations: list[str] = []
    listed_ids: list[str] = []
    has_sample_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("^SAMPLE = "):
            has_sample_block = True
        elif stripped.startswith("!Series_type = "):
            series_type = _parse_soft_value(stripped.split("=", 1)[1])
        elif stripped.startswith("!Series_relation = "):
            relations.append(_parse_soft_value(stripped.split("=", 1)[1]))
        elif stripped.startswith("!Series_sample_id = "):
            value = _parse_soft_value(stripped.split("=", 1)[1])
            if value.startswith("GSM"):
                listed_ids.append(value)
    if has_sample_block:
        return "has_samples"
    if series_type.casefold() == "superseries" or any(
        "subsample" in relation.casefold() or "subseries" in relation.casefold()
        or "superseries" in relation.casefold()
        for relation in relations
    ):
        return "confirmed_superseries"
    if listed_ids:
        return "truncated_soft"
    return "no_samples_unexpected"


def _parse_response(content: Path | bytes, accession: str) -> dict[str, Any]:
    try:
        text = _limited_gunzip(content, max_decompressed_bytes=MAX_DECOMPRESSED_BYTES, max_ratio=MAX_COMPRESSION_RATIO)
    except OversizedResponse:
        raise
    except gzip.BadGzipFile as exc:
        error = ValueError(
            f"GEO SOFT response for {accession} was not a valid gzip file"
        )
        error.error_class = "bad_response"
        raise error from exc
    if not any(line.strip().startswith("^SAMPLE = ") for line in text.splitlines()):
        classification = _classify_empty_soft(text)
        message = f"No GSM sample records were found in GEO SOFT for {accession}"
        error = EOFError(message) if classification == "truncated_soft" else ValueError(message)
        error.error_class = classification
        raise error
    return _parse_soft(text, accession)


def _stream_to_file(response: Any, raw_path: Path, max_bytes: int) -> str:
    """Stream a response to a bounded .part file and atomically publish it."""
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = raw_path.with_name(raw_path.name + ".part")
    part_path.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        with part_path.open("wb") as handle:
            iterator_method = getattr(response, "iter_content", None)
            chunks = None
            if callable(iterator_method):
                try:
                    chunks = iter(iterator_method(DOWNLOAD_CHUNK_SIZE))
                except TypeError:
                    chunks = None
            if chunks is None:
                chunks = iter((response.content,))
            for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise OversizedResponse("GEO SOFT response exceeded compressed byte limit")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, raw_path)
        return digest.hexdigest()
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()

def get_geo_metadata(
    accession: str,
    timeout: float = 30.0,
    retries: int = 4,
    *,
    max_mb: int = 256,
    max_attempts: int | None = None,
    sleep=None,
    rng=None,
) -> dict[str, Any]:
    if max_attempts is not None:
        retries = max_attempts
    accession = accession.strip().upper()
    if not re.fullmatch(r"GSE\d+", accession):
        raise ValueError(
            f"Invalid GEO Series accession: {accession}. Expected GSE followed by digits."
        )
    if retries < 1:
        raise ValueError("retries must be at least 1")
    if max_mb < 1:
        raise ValueError("max_mb must be at least 1")
    url = _get_soft_url(accession)
    source = rng if rng is not None else random.Random()
    sleeper = sleep if sleep is not None else time.sleep
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "hermes-work/sc-dataset-qualification"},
                timeout=timeout,
                stream=True,
            )
            status = getattr(response, "status_code", 200)
            if isinstance(status, int) and status >= 400:
                raise requests.HTTPError(f"HTTP {status} for {url}", response=response)
            response.raise_for_status()
            raw_fd, raw_name = tempfile.mkstemp(prefix="geo-metadata-", suffix=".gz")
            os.close(raw_fd)
            raw_path = Path(raw_name)
            try:
                _stream_to_file(response, raw_path, max_mb * 1024 * 1024)
                return _parse_response(raw_path, accession)
            finally:
                raw_path.unlink(missing_ok=True)
        except OversizedResponse:
            raise
        except (requests.RequestException, gzip.BadGzipFile, EOFError) as exc:
            kind = _classify(exc)
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if kind in {"oversized_response", "bad_response", "permanent_http", "not_found", "permanent"}:
                exc.error_class = kind
                raise
            retry_after = _retry_after_seconds(
                getattr(response, "headers", {}).get("Retry-After") if response else None
            ) if status in {429, 403, 500, 502, 503, 504} else None
            if retry_after is not None and retry_after > 60:
                raise DeferredFetch(retry_after, attempt) from exc
            if kind == "forbidden" and attempt == retries:
                exc.args = (f"forbidden_exhausted: {exc}",)
                exc.error_class = "forbidden_exhausted"
                raise
            if attempt == retries:
                if isinstance(exc, gzip.BadGzipFile):
                    error = ValueError(
                        f"GEO SOFT response for {accession} was not a valid gzip file"
                    )
                    error.error_class = "bad_response"
                    raise error from exc
                exc.error_class = "bad_response" if kind == "bad_response" else kind
                raise
            if retry_after is not None:
                delay = min(retry_after, 60)
            else:
                ceiling = min(2 ** (attempt - 1), 30)
                delay = 0.5 * ceiling + source.uniform(0, 0.5 * ceiling)
            sleeper(delay)
    raise AssertionError("unreachable")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "metadata_fetch.jsonl"
    if not path.exists():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, raw in enumerate(lines):
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index == len(lines) - 1:
                break
            raise ValueError(f"corrupt metadata event line {index + 1}")
        if event.get("status") not in {"fetched", "ok", "failed", "deferred", "registered"}:
            raise ValueError(f"invalid metadata event status on line {index + 1}")
        if not isinstance(event.get("seq"), int) or not isinstance(event.get("gse_id"), str):
            raise TypeError(f"invalid metadata event identity on line {index + 1}")
        key = (event.get("gse_id"), event.get("seq"))
        if key in seen:
            raise ValueError(f"duplicate seq for {key[0]}")
        seen.add(key)
        events.append(event)
    return events


def _append_event(run_dir: Path, gse_id: str, status: str, **fields: Any) -> dict[str, Any]:
    """Append one locked JSONL event; callers use one writer per run directory."""
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_dir / "metadata_fetch.jsonl"
    lock_path = run_dir / "metadata_fetch.jsonl.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        events = _read_events(run_dir)
        if log.exists():
            content = log.read_bytes()
            valid_lines = []
            for line in content.splitlines(keepends=True):
                try:
                    json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
                valid_lines.append(line)
            if len(valid_lines) != len(content.splitlines(keepends=True)):
                _atomic_write(log, b"".join(valid_lines))
        seq = max((e["seq"] for e in events if e.get("gse_id") == gse_id), default=0) + 1
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        event = {
            "run_id": fields.pop("run_id", None) or run_dir.name.removeprefix("run_") or "unknown",
            "gse_id": gse_id,
            "seq": seq,
            "status": status,
            "started_at_utc": now,
            "finished_at_utc": now,
            "attempts": fields.pop("attempts", 1),
            "tool_version": TOOL_VERSION,
            **fields,
        }
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with log.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return event


def _validate_gse_id(gse_id: str) -> str:
    if not isinstance(gse_id, str) or not re.fullmatch(r"GSE\d+", gse_id):
        raise ValueError(f"Invalid GEO Series accession: {gse_id}. Expected GSE followed by digits.")
    return gse_id


def _append_event_best_effort(run_dir: Path, gse_id: str, status: str, **fields: Any) -> None:
    try:
        _append_event(run_dir, gse_id, status, **fields)
    except (ValueError, TypeError, OSError):
        pass


def _latest_events(run_dir: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in _read_events(run_dir):
        gse = event.get("gse_id")
        if gse not in latest or event["seq"] > latest[gse]["seq"]:
            latest[gse] = event
    return latest


def _parse_raw(raw: Path | bytes, gse_id: str) -> dict[str, Any]:
    try:
        return _parse_response(raw, gse_id)
    except BaseException as exc:
        if not getattr(exc, "error_class", None):
            exc.error_class = "bad_response"
        raise


def _artifact_bytes(parsed: dict[str, Any]) -> bytes:
    obj = {"gse": parsed["gse"], "samples": parsed["samples"], "parser_contract": PARSER_CONTRACT}
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_parsed(run_dir: Path, gse_id: str) -> tuple[dict[str, Any], Path, bytes]:
    raw_path = run_dir / "metadata_raw" / f"{gse_id}.soft.gz"
    parsed_path = run_dir / "metadata" / f"{gse_id}.json"
    events = [event for event in _read_events(run_dir) if event.get("gse_id") == gse_id]
    logged = next((event for event in reversed(events) if event.get("raw_sha256")), None)
    if logged and logged["raw_sha256"] != _sha256_file(raw_path):
        raise ValueError("raw_sha_mismatch")
    raw_parsed = _parse_raw(raw_path, gse_id)
    parsed_bytes = parsed_path.read_bytes() if parsed_path.exists() else b""
    if parsed_bytes:
        try:
            document = json.loads(parsed_bytes)
            if document.get("parser_contract") == PARSER_CONTRACT:
                parsed = {"gse": document["gse"], "samples": document["samples"]}
            else:
                parsed = raw_parsed
        except (json.JSONDecodeError, KeyError, TypeError):
            parsed = raw_parsed
    else:
        parsed = raw_parsed
    return parsed, raw_path, parsed_bytes


def _recover_run(run_dir: Path, requested: tuple[str, ...] = ()) -> dict[str, Any]:
    latest = _latest_events(run_dir)
    raw_dir = run_dir / "metadata_raw"
    found = {path.name[:-8] for path in raw_dir.glob("GSE*.soft.gz")} if raw_dir.exists() else set()
    if requested:
        found |= set(requested)
    outcomes: dict[str, Any] = {}
    oversized_found: list[tuple[str, str]] = []
    for gse_id in sorted(found):
        raw_path = raw_dir / f"{gse_id}.soft.gz"
        parsed_path = run_dir / "metadata" / f"{gse_id}.json"
        event = latest.get(gse_id)
        if not raw_path.exists():
            continue
        raw_hash = _sha256_file(raw_path)
        try:
            parsed = _parse_raw(raw_path, gse_id)
        except OversizedResponse as exc:
            (run_dir / "metadata" / f"{gse_id}.json").unlink(missing_ok=True)
            (run_dir / "metadata_parsed" / f"{gse_id}.json").unlink(missing_ok=True)
            _append_event_best_effort(run_dir, gse_id, "failed", attempts=1, error_class="oversized_response", last_error=str(exc))
            outcomes[gse_id] = "oversized_response"
            oversized_found.append((gse_id, str(exc)))
            continue
        except (ValueError, TypeError, EOFError, gzip.BadGzipFile, OSError) as exc:
            if getattr(exc, "error_class", None) in {"truncated_soft", "confirmed_superseries", "no_samples_unexpected"}:
                error_class = exc.error_class
            else:
                error_class = "corrupt_raw"
            _append_event_best_effort(run_dir, gse_id, "failed", attempts=1, error_class=error_class, last_error=str(exc))
            outcomes[gse_id] = error_class
            continue
        if event and event.get("raw_sha256") and event["raw_sha256"] != raw_hash:
            _append_event_best_effort(run_dir, gse_id, "failed", error_class="corrupt_raw", last_error="raw_sha_mismatch")
            outcomes[gse_id] = "corrupt_raw"
            continue
        disk_contract = None
        if parsed_path.exists():
            try:
                disk_contract = json.loads(parsed_path.read_bytes()).get("parser_contract")
            except (json.JSONDecodeError, OSError):
                pass
        origin = "recovered_orphan" if event is None else "recovered"
        if disk_contract not in (None, PARSER_CONTRACT) or (event and event.get("parser_contract") not in (None, PARSER_CONTRACT)):
            origin = "parser_upgrade"
        elif event and event.get("parser_contract") is None:
            origin = "legacy_contract_unknown"
        needs_reparse = True
        if parsed_path.exists():
            try:
                disk = json.loads(parsed_path.read_bytes())
                needs_reparse = disk.get("parser_contract") != PARSER_CONTRACT or not event or event.get("parsed_sha256") != _sha256_file(parsed_path)
            except (json.JSONDecodeError, OSError):
                needs_reparse = True
        if needs_reparse:
            parsed_bytes = _artifact_bytes(parsed)
            _atomic_write(parsed_path, parsed_bytes)
            _atomic_write(run_dir / "metadata_parsed" / f"{gse_id}.json", parsed_bytes)
            if event is None:
                _append_event(run_dir, gse_id, "fetched", origin=origin, raw_path=str(raw_path.relative_to(run_dir)), raw_sha256=raw_hash)
            _append_event_best_effort(run_dir, gse_id, "ok", origin=origin, raw_path=str(raw_path.relative_to(run_dir)), raw_sha256=raw_hash, parsed_path=str(parsed_path.relative_to(run_dir)), parsed_sha256=_sha256(parsed_bytes), parser_contract=PARSER_CONTRACT, n_samples=len(parsed["samples"]))
            outcomes[gse_id] = origin
        elif event and event.get("status") == "failed":
            parsed_bytes = parsed_path.read_bytes()
            _append_event_best_effort(run_dir, gse_id, "ok", origin="recovered", raw_path=str(raw_path.relative_to(run_dir)), raw_sha256=raw_hash, parsed_path=str(parsed_path.relative_to(run_dir)), parsed_sha256=_sha256(parsed_bytes), parser_contract=PARSER_CONTRACT, n_samples=len(parsed["samples"]))
            outcomes[gse_id] = "recovered"
        else:
            outcomes[gse_id] = "consistent"
        if parsed_path.exists():
            sidecar_path = run_dir / "metadata_parsed" / f"{gse_id}.json"
            parsed_bytes = parsed_path.read_bytes()
            if not sidecar_path.exists() or sidecar_path.read_bytes() != parsed_bytes:
                _atomic_write(sidecar_path, parsed_bytes)
    if oversized_found:
        gse_id, message = oversized_found[0]
        error = OversizedResponse(f"{gse_id}: {message}")
        error.outcomes = outcomes
        raise error
    return outcomes

def _fetch_and_record(
    run_dir: Path,
    accession: str,
    timeout: float,
    sleep=None,
    rng=None,
    max_attempts: int = 4,
    max_mb: int = 256,
) -> dict[str, Any]:
    if not re.fullmatch(r"GSE\d+", accession):
        raise ValueError(f"Invalid GEO Series accession: {accession}. Expected GSE followed by digits.")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if max_mb < 1:
        raise ValueError("max_mb must be at least 1")
    url = _get_soft_url(accession)
    source = rng if rng is not None else random.Random()
    sleeper = sleep if sleep is not None else time.sleep
    raw_path = run_dir / "metadata_raw" / f"{accession}.soft.gz"
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "hermes-work/sc-dataset-qualification"},
                timeout=timeout,
                stream=True,
            )
            status = getattr(response, "status_code", 200)
            if isinstance(status, int) and status >= 400:
                raise requests.HTTPError(f"HTTP {status} for {url}", response=response)
            response.raise_for_status()
            raw_hash = _stream_to_file(response, raw_path, max_mb * 1024 * 1024)
        except OversizedResponse as exc:
            raw_path.unlink(missing_ok=True)
            (run_dir / "metadata" / f"{accession}.json").unlink(missing_ok=True)
            (run_dir / "metadata_parsed" / f"{accession}.json").unlink(missing_ok=True)
            exc.attempts_so_far = attempt
            raise
        except requests.RequestException as exc:
            kind = _classify(exc)
            response_for_error = getattr(exc, "response", None)
            status = getattr(response_for_error, "status_code", None)
            exc.attempts_so_far = attempt
            if kind in {"oversized_response", "bad_response", "not_found", "permanent_http", "permanent"}:
                exc.error_class = kind
                raise
            retry_after = _retry_after_seconds(getattr(response_for_error, "headers", {}).get("Retry-After")) if status in {403, 429, 500, 502, 503, 504} else None
            if retry_after is not None and retry_after > 60:
                raise DeferredFetch(retry_after, attempt) from exc
            if kind == "forbidden" and attempt == max_attempts:
                exc.args = (f"forbidden_exhausted: {exc}",)
                exc.error_class = "forbidden_exhausted"
                raise
            if attempt == max_attempts:
                exc.error_class = kind
                raise
            ceiling = min(2 ** (attempt - 1), 30)
            delay = min(retry_after, 60) if retry_after is not None else 0.5 * ceiling + source.uniform(0, 0.5 * ceiling)
            sleeper(delay)
            continue
        _append_event_best_effort(run_dir, accession, "fetched", attempts=attempt, run_id=getattr(_fetch_and_record, "run_id", None), raw_path=str(raw_path.relative_to(run_dir)), raw_sha256=raw_hash)
        try:
            parsed = _parse_raw(raw_path, accession)
        except OversizedResponse:
            (run_dir / "metadata" / f"{accession}.json").unlink(missing_ok=True)
            (run_dir / "metadata_parsed" / f"{accession}.json").unlink(missing_ok=True)
            raise
        except EOFError as exc:
            exc.attempts_so_far = attempt
            if attempt == max_attempts:
                if not getattr(exc, "error_class", None):
                    exc.error_class = "bad_response"
                raise
            ceiling = min(2 ** (attempt - 1), 30)
            sleeper(0.5 * ceiling + source.uniform(0, 0.5 * ceiling))
            continue
        parsed_path = run_dir / "metadata" / f"{accession}.json"
        parsed_bytes = _artifact_bytes(parsed)
        _atomic_write(parsed_path, parsed_bytes)
        _atomic_write(run_dir / "metadata_parsed" / f"{accession}.json", parsed_bytes)
        _append_event_best_effort(run_dir, accession, "ok", attempts=attempt, run_id=getattr(_fetch_and_record, "run_id", None), raw_path=str(raw_path.relative_to(run_dir)), raw_sha256=raw_hash, parsed_path=str(parsed_path.relative_to(run_dir)), parsed_sha256=_sha256(parsed_bytes), parser_contract=PARSER_CONTRACT, n_samples=len(parsed["samples"]))
        return parsed
    raise AssertionError("unreachable")

@click.command()
@click.argument("accession")
@click.option("--timeout", default=30.0, type=float, show_default=True)
@click.option("--max-mb", default=256, type=click.IntRange(min=1), show_default=True)
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
@click.option("--run-dir", type=click.Path(path_type=Path))
@click.option("--run-id", default=None)
def main(accession: str, timeout: float, max_mb: int, pretty: bool, run_dir: Path | None = None, run_id: str | None = None) -> None:
    accession = accession.strip().upper()
    if run_dir is not None and not re.fullmatch(r"GSE\d+", accession):
        raise click.ClickException(f"Invalid GEO Series accession: {accession}. Expected GSE followed by digits.")
    try:
        if run_dir is not None:
            _fetch_and_record.run_id = run_id
            result = _fetch_and_record(run_dir, accession, timeout, max_mb=max_mb)
        else:
            result = get_geo_metadata(accession, timeout=timeout, max_mb=max_mb) if max_mb != 256 else get_geo_metadata(accession, timeout=timeout)
    except DeferredFetch as exc:
        if run_dir is not None:
            _append_event_best_effort(run_dir, accession, "deferred", attempts=exc.attempts_so_far, run_id=run_id, error_class="deferred_fetch", retry_after_seconds=exc.retry_after_seconds, defer_until_utc=exc.defer_until_utc)
        click.echo(json.dumps({"error_class": "deferred_fetch", "retry_after_seconds": exc.retry_after_seconds, "defer_until_utc": exc.defer_until_utc, "attempts_so_far": exc.attempts_so_far}, ensure_ascii=False))
        raise click.exceptions.Exit(75)
    except (ValueError, requests.RequestException, OSError, EOFError) as exc:
        if run_dir is not None:
            _append_event_best_effort(run_dir, accession, "failed", attempts=getattr(exc, "attempts_so_far", 1), run_id=run_id, error_class=getattr(exc, "error_class", _classify(exc)), last_error=str(exc))
        if run_dir is not None:
            click.echo(json.dumps({"error_class": getattr(exc, "error_class", _classify(exc)), "detail": str(exc)}, ensure_ascii=False))
            raise click.exceptions.Exit(1)
        raise click.ClickException(str(exc)) from exc

    click.echo(
        json.dumps(
            result,
            indent=2 if pretty else None,
            ensure_ascii=False,
        )
    )

@click.command("recover")
@click.argument("run_dir", type=click.Path(path_type=Path))
@click.option("--gse", multiple=True)
def recover(run_dir: Path, gse: tuple[str, ...]) -> None:
    try:
        if not run_dir.is_dir():
            raise ValueError(f"run directory does not exist or is not a directory: {run_dir}")
        requested = tuple(_validate_gse_id(item) for item in gse)
        outcomes = _recover_run(run_dir, requested)
    except (ValueError, TypeError, OSError) as exc:
        report = {"error_class": getattr(exc, "error_class", "recover_failed")}
        if report["error_class"] != "oversized_response":
            report["detail"] = str(exc)
        click.echo(json.dumps(report, ensure_ascii=False))
        raise click.exceptions.Exit(1)
    click.echo(json.dumps(outcomes, sort_keys=True))


def _verified_artifact(run_dir: Path, gse_id: str, *, repair: bool = False) -> tuple[dict[str, Any], set[str]]:
    _validate_gse_id(gse_id)
    try:
        parsed, raw_path_or_bytes, parsed_bytes = _load_parsed(run_dir, gse_id)
    except OversizedResponse:
        if repair:
            (run_dir / "metadata" / f"{gse_id}.json").unlink(missing_ok=True)
            (run_dir / "metadata_parsed" / f"{gse_id}.json").unlink(missing_ok=True)
        raise
    parsed_path = run_dir / "metadata" / f"{gse_id}.json"
    document: dict[str, Any] = {}
    if parsed_bytes:
        try:
            document = json.loads(parsed_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            document = {}
    events = [event for event in _read_events(run_dir) if event.get("gse_id") == gse_id]
    logged = next((event for event in reversed(events) if event.get("parsed_sha256")), None)
    consistent = bool(document) and document.get("parser_contract") == PARSER_CONTRACT and logged is not None and logged.get("parser_contract") == PARSER_CONTRACT and logged.get("parsed_sha256") == _sha256(parsed_bytes)
    if not consistent:
        if not repair:
            raise ValueError("metadata artifact is inconsistent or unverifiable; run recover")
        parsed = _parse_raw(raw_path_or_bytes, gse_id)
        parsed_bytes = _artifact_bytes(parsed)
        _atomic_write(parsed_path, parsed_bytes)
        _atomic_write(run_dir / "metadata_parsed" / f"{gse_id}.json", parsed_bytes)
        origin = "legacy_contract_unknown" if logged and not logged.get("parser_contract") else "recovered"
        raw_path = run_dir / "metadata_raw" / f"{gse_id}.soft.gz"
        _append_event_best_effort(run_dir, gse_id, "ok", origin=origin, raw_path=str(raw_path.relative_to(run_dir)), raw_sha256=_sha256_file(raw_path_or_bytes), parsed_path=str(parsed_path.relative_to(run_dir)), parsed_sha256=_sha256(parsed_bytes), parser_contract=PARSER_CONTRACT, n_samples=len(parsed["samples"]))
    return parsed, {str(sample["gsm_id"]) for sample in parsed["samples"]}


@click.command("preflight-registration")
@click.argument("run_dir", type=click.Path(path_type=Path))
@click.argument("gse_id")
@click.option("--db", "db_path", required=True, type=click.Path(path_type=Path))
@click.option("--run-id", default=None, help="Run identity recorded in the grant.")
def preflight_registration(run_dir: Path, gse_id: str, db_path: Path, run_id: str | None) -> None:
    import duckdb
    grant_path: Path | None = None
    try:
        gse_id = _validate_gse_id(gse_id)
        grant_path = run_dir / "preflight" / f"{gse_id}.grant.json"
        if not run_dir.is_dir():
            raise ValueError(f"run directory does not exist or is not a directory: {run_dir}")
        if not db_path.is_file():
            raise ValueError(f"database file does not exist or is not a file: {db_path}")
        _, expected = _verified_artifact(run_dir, gse_id, repair=False)
        connection = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = connection.execute("SELECT gsm_id, gse_id FROM geo_samples WHERE gsm_id IN (" + ",".join("?" for _ in expected) + ")", list(expected)).fetchall() if expected else []
        finally:
            connection.close()
        conflicts = sorted(gsm for gsm, owner in rows if owner != gse_id)
        if conflicts:
            grant_path.unlink(missing_ok=True)
            click.echo(json.dumps({"error_class": "ownership_conflict", "gsm_ids": conflicts}))
            raise click.exceptions.Exit(2)
        parsed_path = run_dir / "metadata_parsed" / f"{gse_id}.json"
        raw_path = run_dir / "metadata_raw" / f"{gse_id}.soft.gz"
        latest_event = next(
            (event for event in reversed(_read_events(run_dir)) if event.get("gse_id") == gse_id),
            None,
        )
        grant = {
            "schema": "grant-v1",
            "gse_id": gse_id,
            "run_id": run_id or (latest_event or {}).get("run_id") or run_dir.name.removeprefix("run_") or "unknown",
            "expected_gsm_ids": sorted(expected),
            "parsed_sha256": _sha256(parsed_path.read_bytes()),
            "raw_sha256": _sha256(raw_path.read_bytes()),
            "issued_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        grant["grant_sha256"] = hashlib.sha256(json.dumps(grant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        grant_path = run_dir / "preflight" / f"{gse_id}.grant.json"
        _atomic_write(grant_path, json.dumps(grant, sort_keys=True, separators=(",", ":")).encode())
        click.echo(json.dumps({"gse_id": gse_id, "expected_gsm_ids": sorted(expected), "status": "ready", "grant_path": str(grant_path)}))
    except click.exceptions.Exit:
        raise
    except (ValueError, TypeError, OSError, duckdb.Error) as exc:
        if grant_path is not None:
            try:
                grant_path.unlink(missing_ok=True)
            except OSError:
                pass
        click.echo(json.dumps({"error_class": getattr(exc, "error_class", "preflight_failed"), "detail": str(exc)}, ensure_ascii=False))
        raise click.exceptions.Exit(1)


@click.command("verify-registration")
@click.argument("run_dir", type=click.Path(path_type=Path))
@click.argument("gse_id")
@click.option("--db", "db_path", required=True, type=click.Path(path_type=Path))
@click.option("--run-id", default=None, help="Run identity recorded in the registered event.")
def verify_registration(run_dir: Path, gse_id: str, db_path: Path, run_id: str | None) -> None:
    import duckdb
    try:
        gse_id = _validate_gse_id(gse_id)
        if not run_dir.is_dir():
            raise ValueError(f"run directory does not exist or is not a directory: {run_dir}")
        if not db_path.is_file():
            raise ValueError(f"database file does not exist or is not a file: {db_path}")
        parsed, expected = _verified_artifact(run_dir, gse_id, repair=True)
        connection = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = connection.execute("SELECT gsm_id, gse_id FROM geo_samples WHERE gse_id=? OR gsm_id IN (" + ",".join("?" for _ in expected) + ")", [gse_id, *sorted(expected)]).fetchall() if expected else connection.execute("SELECT gsm_id, gse_id FROM geo_samples WHERE gse_id=?", [gse_id]).fetchall()
            actual = {gsm for gsm, owner in rows if owner == gse_id}
            owners = {gsm: owner for gsm, owner in rows}
            expected_chars = set()
            for sample in parsed["samples"]:
                for key, value in sample.get("characteristics", {}).items():
                    norm = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else None if value is None else str(value)
                    expected_chars.add((sample["gsm_id"], str(key), norm))
            actual_chars = set(connection.execute("SELECT c.gsm_id, c.characteristic, c.value FROM geo_sample_characteristics c LEFT JOIN geo_samples s USING(gsm_id) WHERE s.gse_id=? OR c.gsm_id IN (" + ",".join("?" for _ in expected) + ")", [gse_id, *sorted(expected)]).fetchall()) if expected else set()
            connection.execute("SELECT 1 FROM geo_sample_metadata LIMIT 0")
            metadata = connection.execute("SELECT gsm_id FROM geo_sample_metadata WHERE gsm_id IN (" + ",".join("?" for _ in expected) + ")", list(expected)).fetchall() if expected else []
        finally:
            connection.close()
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        ownership = sorted(gsm for gsm in expected if gsm in owners and owners[gsm] != gse_id)
        expected_by_key = {(gsm, key): value for gsm, key, value in expected_chars}
        actual_by_key = {(gsm, key): value for gsm, key, value in actual_chars}
        matched_keys = expected_by_key.keys() & actual_by_key.keys()
        value_mismatch = sorted(
            (gsm, key, expected_by_key[(gsm, key)], actual_by_key[(gsm, key)])
            for gsm, key in matched_keys
            if expected_by_key[(gsm, key)] != actual_by_key[(gsm, key)]
        )
        mismatch_keys = {(gsm, key) for gsm, key, _, _ in value_mismatch}
        missing_chars = sorted(
            row for row in expected_chars - actual_chars
            if (row[0], row[1]) not in mismatch_keys
        )
        extra_chars = sorted(
            row for row in actual_chars - expected_chars
            if (row[0], row[1]) not in mismatch_keys
        )
        problems = {}
        if missing: problems["samples_missing"] = missing
        if extra: problems["samples_extra"] = extra
        if ownership: problems["ownership_conflict"] = ownership
        if missing_chars: problems["features_missing"] = [list(row) for row in missing_chars]
        if extra_chars: problems["features_extra"] = [list(row) for row in extra_chars]
        if value_mismatch: problems["features_value_mismatch"] = [list(row) for row in value_mismatch]
        if problems:
            click.echo(json.dumps({"error_class": "registration_mismatch", "subcodes": problems, "metadata_rows_present": len(metadata)}))
            raise click.exceptions.Exit(1)
        _append_event(
            run_dir,
            gse_id,
            "registered",
            run_id=run_id,
            registration={
                "schema": "registration-v1",
                "gse_id": gse_id,
                "expected_gsm_ids": sorted(expected),
                "samples": [
                    {"gsm_id": sample["gsm_id"], "characteristics": sample.get("characteristics", {})}
                    for sample in sorted(parsed["samples"], key=lambda sample: sample["gsm_id"])
                ],
                "sample_count": len(expected),
                "verified": True,
            },
        )
        click.echo(json.dumps({"gse_id": gse_id, "status": "registered", "metadata_rows_present": len(metadata)}))
    except click.exceptions.Exit:
        raise
    except (ValueError, TypeError, OSError, RuntimeError, duckdb.Error) as exc:
        error_class = getattr(exc, "error_class", "registration_mismatch")
        click.echo(json.dumps({"error_class": error_class, "subcodes": {"verification_error": str(exc)}}))
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    import sys
    commands = {"recover": recover, "preflight-registration": preflight_registration, "verify-registration": verify_registration}
    if len(sys.argv) > 1 and sys.argv[1] in commands:
        command = commands[sys.argv[1]]
        sys.argv.pop(1)
        command()
    else:
        main()
