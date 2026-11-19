"""Adapter for standard 10x Matrix Market matrix/barcode/feature triplets."""
from __future__ import annotations

import gzip
import io
import re
import tarfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import anndata as ad
import numpy as np
import pandas as pd
from scipy import io as scipy_io
from scipy import sparse

from .base import AdapterContext, LoadCandidate

GSM_RE = re.compile(r"GSM\d+", re.IGNORECASE)
PART_RE = re.compile(
    r"^(?P<prefix>.+?)[._-](?P<kind>matrix\.mtx(?:\.gz)?|barcodes\.tsv(?:\.gz)?|features\.tsv(?:\.gz)?|genes\.tsv(?:\.gz)?)$",
    re.IGNORECASE,
)


class TenxMatrixMarketAdapter:
    name = "tenx_matrix_market"

    def discover(self, context: AdapterContext) -> list[LoadCandidate]:
        if not any(group.get("complete_standard_10x_triplet")
                   for group in context.format_report.get("10x_candidate_groups", [])):
            return []
        groups: dict[tuple[str, str, str], dict[str, str]] = defaultdict(dict)
        origins: dict[tuple[str, str, str], tuple[Path, bool, str]] = {}

        for archive_path in sorted(context.supplementary_dir.rglob("*.tar")):
            if not tarfile.is_tarfile(archive_path):
                continue
            try:
                with tarfile.open(archive_path, "r:*") as archive:
                    for member in archive:
                        if not member.isfile():
                            continue
                        member_name = member.name.replace("\\", "/")
                        if member_name.startswith("/") or ".." in Path(member_name).parts:
                            continue
                        parent, _, base = member_name.rpartition("/")
                        match = PART_RE.match(base)
                        if not match:
                            continue
                        prefix = match.group("prefix")
                        gsm_match = GSM_RE.search(prefix)
                        if not gsm_match:
                            continue
                        gsm_id = gsm_match.group(0).upper()
                        if gsm_id not in context.include_gsms:
                            continue
                        kind_text = match.group("kind").lower()
                        kind = "matrix" if kind_text.startswith("matrix.") else "barcodes" if kind_text.startswith("barcodes.") else "features"
                        key = (str(archive_path), parent, prefix)
                        groups[key][kind] = member.name
                        relative = str(archive_path.relative_to(context.supplementary_dir))
                        origins[key] = (archive_path, True, relative)
            except (OSError, tarfile.TarError, EOFError):
                continue

        for path in sorted(context.supplementary_dir.rglob("*")):
            if not path.is_file() or path.name.lower().endswith((".tar", ".tar.gz", ".tgz")):
                continue
            match = PART_RE.match(path.name)
            if not match:
                continue
            prefix = match.group("prefix")
            gsm_match = GSM_RE.search(prefix)
            if not gsm_match or gsm_match.group(0).upper() not in context.include_gsms:
                continue
            kind_text = match.group("kind").lower()
            kind = "matrix" if kind_text.startswith("matrix.") else "barcodes" if kind_text.startswith("barcodes.") else "features"
            relative_parent = str(path.parent.relative_to(context.supplementary_dir))
            key = ("directory", relative_parent, prefix)
            groups[key][kind] = str(path)
            origins[key] = (path, False, str(path.relative_to(context.supplementary_dir)))

        candidates = []
        for key, components in sorted(groups.items()):
            if set(components) != {"matrix", "barcodes", "features"}:
                continue
            gsm_match = GSM_RE.search(key[2])
            if not gsm_match:
                continue
            source_path, is_archive, relative = origins[key]
            candidates.append(LoadCandidate(
                gsm_id=gsm_match.group(0).upper(),
                source_path=source_path,
                source_relative_path=relative,
                format_guess="matrix_market_expression_matrix",
                components=components,
                archive_member_map=is_archive,
            ))
        return candidates

    @contextmanager
    def _component_stream(self, candidate: LoadCandidate, key: str) -> Iterator[Any]:
        value = candidate.components[key]
        if candidate.archive_member_map:
            archive = tarfile.open(candidate.source_path, "r:*")
            try:
                member = archive.getmember(value)
                raw = archive.extractfile(member)
                if raw is None:
                    raise ValueError(f"Cannot open archive member {value}")
                try:
                    if value.lower().endswith(".gz"):
                        with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
                            yield stream
                    else:
                        yield raw
                finally:
                    raw.close()
            finally:
                archive.close()
        else:
            path = Path(value)
            with path.open("rb") as raw:
                if path.name.lower().endswith(".gz"):
                    with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
                        yield stream
                else:
                    yield raw

    @staticmethod
    def _read_lines(stream: Any) -> list[str]:
        wrapper = io.TextIOWrapper(stream, encoding="utf-8-sig")
        try:
            return [line.rstrip("\r\n") for line in wrapper if line.strip()]
        finally:
            wrapper.detach()

    @staticmethod
    def _typed_frame(values: dict[str, list[Any]], specs: dict[str, Any]) -> pd.DataFrame:
        frame = pd.DataFrame(index=range(len(next(iter(values.values()), []))))
        dtype_map = {"string": "string", "Int64": "Int64", "Int32": "Int32", "Float32": "Float32"}
        for column, spec in specs.items():
            if column == "index":
                continue
            dtype = dtype_map[spec["dtype"]]
            column_values = values.get(column)
            if column_values is None:
                column_values = [pd.NA] * len(frame)
            frame[column] = pd.Series(column_values, dtype=dtype)
        return frame

    def load(self, context: AdapterContext, candidate: LoadCandidate) -> ad.AnnData:
        with self._component_stream(candidate, "matrix") as stream:
            feature_by_cell = scipy_io.mmread(stream)
        if not sparse.issparse(feature_by_cell):
            feature_by_cell = sparse.coo_matrix(feature_by_cell)
        feature_by_cell = feature_by_cell.tocsr()

        with self._component_stream(candidate, "barcodes") as stream:
            barcodes = self._read_lines(stream)
        with self._component_stream(candidate, "features") as stream:
            feature_lines = self._read_lines(stream)

        feature_ids: list[str] = []
        feature_names: list[str | None] = []
        feature_types: list[str | None] = []
        for line in feature_lines:
            parts = line.split("\t")
            if not parts or not parts[0].strip():
                raise ValueError("Feature annotation has an empty feature ID")
            feature_ids.append(parts[0].strip())
            feature_names.append(parts[1].strip() if len(parts) > 1 and parts[1].strip() else None)
            feature_types.append(parts[2].strip() if len(parts) > 2 and parts[2].strip() else None)

        if feature_by_cell.shape != (len(feature_ids), len(barcodes)):
            raise ValueError(
                f"Matrix dimensions {feature_by_cell.shape} do not match "
                f"features/barcodes ({len(feature_ids)}, {len(barcodes)})"
            )
        if len(set(feature_ids)) != len(feature_ids):
            raise ValueError("Feature IDs are not unique; refusing to alter the source identifiers")
        if len(set(barcodes)) != len(barcodes):
            raise ValueError("Barcodes are not unique within this GSM")
        if not np.isfinite(feature_by_cell.data).all():
            raise ValueError("Matrix contains non-finite values")

        x = feature_by_cell.transpose().tocsr().astype(np.float32)
        contract = context.canonical_schema["anndata_contract"]
        sample = context.sample_metadata.get(candidate.gsm_id, {})
        obs_values: dict[str, list[Any]] = {
            "sample_id": [candidate.gsm_id] * len(barcodes),
            "source_barcode": barcodes,
            "donor_id": [sample.get("patient_id") or pd.NA] * len(barcodes),
            "organism": [sample.get("organism") or pd.NA] * len(barcodes),
            "tissue": [sample.get("tissue") or sample.get("tissue_source") or pd.NA] * len(barcodes),
            "cell_type": [pd.NA] * len(barcodes),
            "disease": [sample.get("disease") or sample.get("disease_status") or pd.NA] * len(barcodes),
            "condition": [sample.get("disease_status") or pd.NA] * len(barcodes),
            "assay": [pd.NA] * len(barcodes),
            "modality": [pd.NA] * len(barcodes),
            "timepoint": [sample.get("timepoint") or sample.get("timepoint_value") or pd.NA] * len(barcodes),
            "source_accession": [context.gse_id] * len(barcodes),
            "n_counts": [pd.NA] * len(barcodes),
            "n_features": [pd.NA] * len(barcodes),
            "percent_mito": [pd.NA] * len(barcodes),
        }
        obs = self._typed_frame(obs_values, contract["obs"])
        obs.index = pd.Index([f"{candidate.gsm_id}:{barcode}" for barcode in barcodes], name="cell_id")

        var_values = {
            "feature_name": feature_names,
            "feature_type": feature_types,
            "genome_build": [pd.NA] * len(feature_ids),
        }
        var = self._typed_frame(var_values, contract["var"])
        var.index = pd.Index(feature_ids, name="feature_id")

        adata = ad.AnnData(X=x, obs=obs, var=var)
        adata.uns["schema_version"] = context.canonical_schema["schema_version"]
        adata.uns["schema_id"] = context.canonical_schema["schema_id"]
        adata.uns["schema_sha256"] = context.schema_sha256
        adata.uns["source_gse"] = context.gse_id
        adata.uns["source_format"] = {
            "format": "10x Matrix Market",
            "file": candidate.source_relative_path,
            "components": candidate.components,
        }
        source_record = next((item for item in context.manifest.get("files", [])
                              if item.get("filename") == candidate.source_path.name), None)
        if source_record and source_record.get("sha256"):
            adata.uns["source_format"]["source_sha256"] = source_record["sha256"]
        adata.uns["matrix_semantics"] = "unknown"
        adata.uns["feature_id_namespace"] = "unknown"
        adata.uns["harmonization_notes"] = "Loaded without normalization; matrix semantics and feature namespace were not confirmed by source documentation."
        return adata
