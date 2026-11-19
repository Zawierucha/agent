#!/usr/bin/env python3
"""Profile inspected GEO formats against the project's canonical AnnData contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

VERSION = "0.3.0"
GSM_RE = re.compile(r"GSM\d+", re.IGNORECASE)
DEFAULT_SCHEMA = Path(__file__).resolve().parents[3] / "data" / "anndata_schema.json"


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return value


def load_canonical_schema(path: Path) -> tuple[dict[str, Any], str]:
    schema = read_json(path, "canonical AnnData schema")
    if schema.get("status") != "canonical" or not schema.get("schema_id") or not schema.get("schema_version"):
        raise ValueError(f"Schema file is not a versioned canonical contract: {path}")
    required_sections = {"X", "layers", "obs", "var", "uns"}
    contract = schema.get("anndata_contract")
    if not isinstance(contract, dict) or not required_sections.issubset(contract):
        raise ValueError(f"Canonical schema is missing contract sections: {sorted(required_sections)}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return schema, digest


def catalog_gsms(db_path: Path, gse: str) -> tuple[list[str], dict[str, dict[str, Any]], str | None]:
    if not db_path.is_file():
        raise FileNotFoundError(f"DuckDB catalog not found: {db_path}")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("SELECT gsm_id FROM geo_samples WHERE gse_id = ? ORDER BY gsm_id", [gse]).fetchall()
        dataset_row = con.execute("SELECT qualification FROM geo_qualification WHERE gse_id=?", [gse]).fetchone()
        decisions = {}
        has_table = con.execute("""SELECT count(*) FROM information_schema.tables
            WHERE table_schema='main' AND table_name='geo_sample_qualification'""").fetchone()[0]
        if has_table:
            decisions = {row[0]: {"decision": row[1], "tissue_source": row[2],
                                  "disease_status": row[3], "modality_evidence": row[4]}
                         for row in con.execute("""SELECT q.gsm_id, q.qualification, q.tissue_source,
                             q.disease_status, q.modality_evidence FROM geo_sample_qualification q
                             JOIN geo_samples s USING(gsm_id) WHERE s.gse_id=?""", [gse]).fetchall()}
    finally:
        con.close()
    return [str(row[0]).upper() for row in rows], decisions, dataset_row[0] if dataset_row else None


def classify_route(format_guess: str) -> dict[str, str]:
    routes = {
        "matrix_market_expression_matrix": ("10x Matrix Market", "inspect_triplet_and_matrix_dimensions"),
        "matrix_market_file": ("Matrix Market, identity unclear", "confirm_orientation_and_cell_feature_axes"),
        "possible_10x_hdf5_matrix": ("possible 10x HDF5", "inspect_hdf5_structure_before_mapping"),
        "hdf5_unknown": ("unidentified HDF5", "inspect_container_metadata_and_schema"),
        "anndata_h5ad": ("AnnData H5AD", "inspect_existing_obs_var_layers_uns_and_matrix"),
        "loom_expression_matrix": ("Loom", "inspect_row_col_attributes_and_matrix_orientation"),
        "r_serialized_object": ("RDS/RData", "convert_with_declared_R_workflow_then_validate"),
        "delimited_table_csv": ("CSV table", "identify_cell_feature_axes_and_identifier_columns"),
        "delimited_text_or_table": ("TSV/text table", "identify_cell_feature_axes_and_identifier_columns"),
        "parquet_table": ("Parquet table", "inspect_columns_and_declared_matrix_layout"),
        "sequencing_reads_fastq": ("FASTQ reads", "run_upstream_quantification_before_AnnData"),
        "possible_archive": ("archive", "inspect_members_without_extracting; classify member files"),
        "unknown": ("unknown", "manual_format_identification_required"),
    }
    label, next_action = routes.get(format_guess, (format_guess.replace("_", " "), "inspect_format_specific_structure_before_mapping"))
    return {"format": label, "next_action": next_action}


def build_report(gse: str, manifest: dict[str, Any], inspected: dict[str, Any], gsms: list[str],
                 sample_decisions: dict[str, dict[str, Any]] | None = None,
                 dataset_decision: str | None = None,
                 canonical_schema: dict[str, Any] | None = None,
                 schema_sha256: str | None = None) -> dict[str, Any]:
    sample_decisions = sample_decisions or {}
    if not canonical_schema or canonical_schema.get("status") != "canonical":
        raise ValueError("A versioned canonical AnnData schema is required")
    if str(manifest.get("gse_id", "")).upper() != gse:
        raise ValueError("Download manifest GSE does not match requested GSE")
    if str(inspected.get("gse_id", "")).upper() != gse or not inspected.get("manifest_found"):
        raise ValueError("Format report is missing, belongs to another GSE, or has no manifest")

    format_counts = Counter()
    for item in inspected.get("files", []):
        format_counts[str(item.get("format_guess", "unknown"))] += 1
        for member in item.get("archive_inspection", {}).get("members", []):
            format_counts[str(member.get("format_guess", "unknown"))] += 1

    groups = inspected.get("10x_candidate_groups", [])
    gsm_to_groups: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        prefix = str(group.get("sample_or_bundle_prefix", ""))
        gsm_match = GSM_RE.search(prefix)
        if gsm_match:
            gsm_to_groups.setdefault(gsm_match.group(0).upper(), []).append(group)

    manifest_gsms = {str(value).upper() for value in manifest.get("catalogued_gsm_ids", [])}
    catalog_gsms = set(gsms)
    sample_rows = []
    for gsm in sorted(manifest_gsms | catalog_gsms | set(gsm_to_groups)):
        candidates = gsm_to_groups.get(gsm, [])
        complete = [group for group in candidates if group.get("complete_standard_10x_triplet")]
        sample_rows.append({
            "gsm_id": gsm,
            "in_catalog": gsm in catalog_gsms,
            "in_download_manifest": gsm in manifest_gsms,
            "10x_candidate_count": len(candidates),
            "complete_10x_triplet_count": len(complete),
            "candidate_prefixes": [group.get("directory_or_archive_prefix") for group in candidates],
            "qualification": sample_decisions.get(gsm, {}).get("decision"),
            "qualification_evidence": sample_decisions.get(gsm),
            "eligible_for_future_anndata": sample_decisions.get(gsm, {}).get("decision") == "include",
            "status": (
                "multiple_or_ambiguous_10x_candidates" if len(complete) > 1 else
                "complete_10x_candidate" if len(complete) == 1 else
                "partial_10x_candidate" if candidates else
                "no_10x_candidate_detected"
            ),
        })

    present_formats = [name for name, count in sorted(format_counts.items()) if count]
    return {
        "report_version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "gse_id": gse,
        "dataset_qualification": dataset_decision,
        "purpose": "format and sample mapping report against the fixed canonical AnnData contract; no AnnData object is created",
        "canonical_schema_id": canonical_schema["schema_id"],
        "canonical_schema_version": canonical_schema["schema_version"],
        "canonical_schema_sha256": schema_sha256,
        "format_summary": {
            "formats_detected": present_formats,
            "file_and_archive_member_counts": dict(sorted(format_counts.items())),
            "complete_10x_triplet_candidates": sum(bool(group.get("complete_standard_10x_triplet")) for group in groups),
            "partial_10x_candidates": sum(not bool(group.get("complete_standard_10x_triplet")) for group in groups),
            "routes": {format_name: classify_route(format_name) for format_name in present_formats},
        },
        "sample_format_alignment": sample_rows,
        "canonical_schema": canonical_schema,
        "format_specific_handling": {
            "10x_matrix_market": "Map matrix values to canonical X only after the barcode and feature files are matched, dimensions agree, and feature/cell orientation is confirmed. Use feature IDs as var_names and retain feature names/types in var.",
            "10x_hdf5_or_unknown_hdf5": "An HDF5 signature alone is not proof of 10x. Inspect internal groups and datasets with an appropriate reader; do not map from extension alone.",
            "existing_h5ad": "Inspect its obs, var, X, layers, and uns, then map into the canonical contract without losing source annotations; source-specific columns go to provenance notes, not new contract columns.",
            "loom": "Inspect row/column attributes and matrix orientation before mapping cells to obs and features to var.",
            "csv_tsv_parquet": "Identify matrix orientation and identifier columns from evidence or a user-provided mapping; do not guess based on shape alone. Map only into canonical columns and leave unavailable metadata null.",
            "r_serialized": "Use an explicit conversion workflow and inspect the resulting structure before mapping.",
            "fastq": "These are raw reads, not a cell-by-feature expression matrix; quantify upstream before AnnData assembly.",
            "unknown_or_partial": "Record the unresolved format and request format-specific evidence or conversion. Do not force it into the 10x path.",
        },
        "compatibility_rules": [
            "Use the exact canonical obs and var columns and declared dtypes for every dataset; do not propose or add dataset-specific columns.",
            "Keep unavailable fields present as null; never infer values to satisfy a required field.",
            "Keep obs metadata fields string-typed across datasets so category vocabularies can differ without changing the schema.",
            "Keep obs.cell_type null until per-cell annotation is performed; do not copy sample-level labels into it.",
            "Preserve source feature identifiers and use a declared namespace; do not merge features by display name alone.",
            "Align samples by feature ID only after checking genome build and identifier namespace compatibility.",
            "Record whether X contains raw counts, normalized values, or unknown values; populate layers['counts'] only for verified raw counts.",
            "Keep modality-specific features labeled in var.feature_type; do not silently mix RNA, antibody, ATAC, or other modalities in one expression matrix.",
            "Keep source file hashes, transformations, and unresolved mappings in uns.harmonization_notes or a companion provenance report.",
        ],
        "limitations": [
            "Format inspection supplies hints and candidate bundles; this tool does not inspect matrix contents or create AnnData.",
            "Sample decisions are passed through from the qualification catalog; this report does not make or change biological eligibility decisions.",
            "A non-10x source requires its own format-specific reader and explicit mapping before it can share the schema.",
            "The report embeds the exact canonical schema and its SHA-256; physical AnnData objects cannot yet be validated because this pipeline has no AnnData builder stage.",
        ],
        "inputs": {
            "catalogued_sample_count": len(catalog_gsms),
            "manifest_sample_count": len(manifest_gsms),
            "format_report_path": str(inspected.get("report_path") or "format_report.json"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report inspected GEO formats against the canonical AnnData schema")
    parser.add_argument("--gse", required=True, help="GEO series accession, e.g. GSE346721")
    parser.add_argument("--db", type=Path, default=Path("data/geo.duckdb"))
    parser.add_argument("--download-root", type=Path, default=Path("data/downloads"))
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA,
                        help="Canonical schema JSON; defaults to data/anndata_schema.json")
    parser.add_argument("--output", type=Path, help="Report JSON; defaults to <download-root>/<GSE>/anndata_schema_report.json")
    args = parser.parse_args()
    gse = args.gse.upper()
    if not re.fullmatch(r"GSE\d+", gse):
        parser.error("--gse must be a GEO Series accession such as GSE346721")
    try:
        dataset_dir = args.download_root / gse
        manifest = read_json(dataset_dir / "manifest.json", "download manifest")
        format_report = read_json(dataset_dir / "format_report.json", "format report")
        gsm_ids, sample_decisions, dataset_decision = catalog_gsms(args.db, gse)
        canonical_schema, schema_sha256 = load_canonical_schema(args.schema)
        report = build_report(
            gse, manifest, format_report, gsm_ids, sample_decisions,
            dataset_decision, canonical_schema, schema_sha256,
        )
        output = args.output or dataset_dir / "anndata_schema_report.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "success", "gse_id": gse,
                          "canonical_schema_id": report["canonical_schema_id"],
                          "canonical_schema_version": report["canonical_schema_version"],
                          "canonical_schema_sha256": schema_sha256,
                          "formats_detected": report["format_summary"]["formats_detected"],
                          "complete_10x_triplet_candidates": report["format_summary"]["complete_10x_triplet_candidates"],
                          "output": str(output)}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, duckdb.Error) as exc:
        json.dump({"status": "error", "gse_id": gse, "error": str(exc)}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
