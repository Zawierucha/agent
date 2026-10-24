import gzip
import json
import re
from typing import Any

import click
import requests

GEO_FTP_URL = "https://ftp.ncbi.nlm.nih.gov/geo/series"


def _request_text(url: str, timeout: float) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": "hermes-work/sc-dataset-qualification"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


def _request_bytes(url: str, timeout: float) -> bytes:
    response = requests.get(
        url,
        headers={"User-Agent": "hermes-work/sc-dataset-qualification"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.content


def _get_soft_url(accession: str) -> str:
    match = re.fullmatch(r"GSE(\d+)", accession.upper())
    if not match:
        raise ValueError(f"Invalid GEO Series accession: {accession}")

    digits = match.group(1)
    if len(digits) < 3:
        raise ValueError(f"Invalid GEO Series accession: {accession}")

    series_accession = f"GSE{digits}"
    series_bucket = f"GSE{digits[:3]}nnn"

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


def get_geo_metadata(accession: str, timeout: float = 30.0) -> dict[str, Any]:
    accession = accession.strip().upper()

    if not re.fullmatch(r"GSE\d+", accession):
        raise ValueError(
            f"Invalid GEO Series accession: {accession}. Expected GSE followed by digits."
        )

    url = _get_soft_url(accession)
    content = _request_bytes(url, timeout)

    try:
        soft_text = gzip.decompress(content).decode("utf-8", errors="replace")
    except gzip.BadGzipFile as exc:
        raise ValueError(
            f"GEO SOFT response for {accession} was not a valid gzip file"
        ) from exc

    return _parse_soft(soft_text, accession)


@click.command()
@click.argument("accession")
@click.option("--timeout", default=30.0, type=float, show_default=True)
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
def main(accession: str, timeout: float, pretty: bool) -> None:
    try:
        result = get_geo_metadata(accession, timeout=timeout)
    except (ValueError, requests.RequestException, OSError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        json.dumps(
            result,
            indent=2 if pretty else None,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
