"""Adapters for GEO dense gene-by-cell DGE matrices stored in TAR files."""
from __future__ import annotations

import gzip
import io
import re
import tarfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse

from .base import AdapterContext, LoadCandidate

GSM_RE = re.compile(r"^(GSM\d+)_", re.IGNORECASE)
GSE150_RE = re.compile(r"^(GSM\d+)_.*\.umi\.dge(?:_1_)?\.txt\.gz$", re.IGNORECASE)
GSE162_RE = re.compile(r"^(GSM\d+)_SingleCellCounts_(UCI_\d+)\.txt\.gz$", re.IGNORECASE)


class DenseDgeTarAdapter:
    name = "dense_dge_tar"

    def discover(self, context: AdapterContext) -> list[LoadCandidate]:
        if context.gse_id not in {"GSE150115", "GSE162335"}:
            return []
        archive = context.supplementary_dir / f"{context.gse_id}_RAW.tar"
        if not archive.is_file() or not tarfile.is_tarfile(archive):
            return []
        pattern = GSE150_RE if context.gse_id == "GSE150115" else GSE162_RE
        found: dict[str, list[tuple[str, str | None]]] = {}
        with tarfile.open(archive, "r:*") as handle:
            for member in handle:
                if not member.isfile():
                    continue
                safe_name = member.name.replace("\\", "/")
                if safe_name.startswith("/") or ".." in Path(safe_name).parts:
                    continue
                match = pattern.match(Path(safe_name).name)
                if not match:
                    continue
                gsm = match.group(1).upper()
                if gsm in context.include_gsms:
                    label = match.group(2) if context.gse_id == "GSE162335" else None
                    found.setdefault(gsm, []).append((member.name, label))
        candidates: list[LoadCandidate] = []
        for gsm in sorted(context.include_gsms):
            matches = found.get(gsm, [])
            if len(matches) > 1:
                raise ValueError(f"Multiple dense DGE members map to {gsm}: {[m[0] for m in matches]}")
            if len(matches) == 1:
                member, label = matches[0]
                candidates.append(LoadCandidate(
                    gsm_id=gsm,
                    source_path=archive,
                    source_relative_path=archive.name,
                    format_guess="dense_gene_by_cell_dge",
                    components={"member": member, "sample_label": label or ""},
                    archive_member_map=True,
                ))
        return candidates

    @contextmanager
    def _text_stream(self, candidate: LoadCandidate) -> Iterator[io.TextIOWrapper]:
        archive = tarfile.open(candidate.source_path, "r:*")
        raw = None
        compressed = None
        wrapper = None
        try:
            member = archive.getmember(candidate.components["member"])
            raw = archive.extractfile(member)
            if raw is None:
                raise ValueError(f"Cannot open archive member {member.name}")
            compressed = gzip.GzipFile(fileobj=raw, mode="rb")
            wrapper = io.TextIOWrapper(compressed, encoding="utf-8-sig")
            yield wrapper
        finally:
            if wrapper is not None:
                wrapper.close()
            elif compressed is not None:
                compressed.close()
            if raw is not None:
                raw.close()
            archive.close()

    @staticmethod
    def _typed_frame(length: int, values: dict[str, Any], specs: dict[str, Any]) -> pd.DataFrame:
        frame = pd.DataFrame(index=range(length))
        for column, spec in specs.items():
            if column == "index":
                continue
            supplied = values.get(column, [pd.NA] * length)
            frame[column] = pd.Series(supplied, dtype=spec["dtype"])
        return frame

    def _read_matrix(self, candidate: LoadCandidate, require_integer: bool) -> tuple[sparse.csr_matrix, list[str], list[str]]:
        row_parts: list[np.ndarray] = []
        col_parts: list[np.ndarray] = []
        data_parts: list[np.ndarray] = []
        features: list[str] = []
        with self._text_stream(candidate) as stream:
            header = stream.readline().rstrip("\r\n")
            header_parts = header.split("\t")
            if len(header_parts) < 2 or header_parts[0].strip().lower() not in {"gene", "genes"}:
                raise ValueError("Dense DGE header must begin with Gene/GENE followed by cell identifiers")
            barcodes = [value.strip() for value in header_parts[1:]]
            if any(not value for value in barcodes) or len(set(barcodes)) != len(barcodes):
                raise ValueError("Cell identifiers are empty or duplicated")
            for row_number, line in enumerate(stream):
                line = line.rstrip("\r\n")
                if not line:
                    continue
                feature, separator, values_text = line.partition("\t")
                feature = feature.strip()
                if not separator or not feature:
                    raise ValueError(f"Malformed dense DGE row {row_number + 2}")
                values = np.fromstring(values_text, sep="\t", dtype=np.float64)
                if values.size != len(barcodes):
                    raise ValueError(f"Row {row_number + 2} has {values.size} values; expected {len(barcodes)}")
                if not np.isfinite(values).all() or (values < 0).any():
                    raise ValueError(f"Row {row_number + 2} contains non-finite or negative values")
                if require_integer and not np.equal(values, np.floor(values)).all():
                    raise ValueError(f"Row {row_number + 2} contains non-integer values in a confirmed count matrix")
                features.append(feature)
                nonzero = np.flatnonzero(values)
                if nonzero.size:
                    row_parts.append(np.full(nonzero.size, len(features) - 1, dtype=np.int32))
                    col_parts.append(nonzero.astype(np.int32, copy=False))
                    data_parts.append(values[nonzero].astype(np.float32))
        if not features:
            raise ValueError("Dense DGE contains no feature rows")
        if len(set(features)) != len(features):
            raise ValueError("Feature identifiers are duplicated; refusing to alter source identifiers")
        if data_parts:
            rows = np.concatenate(row_parts)
            cols = np.concatenate(col_parts)
            data = np.concatenate(data_parts)
            feature_by_cell = sparse.coo_matrix((data, (rows, cols)), shape=(len(features), len(barcodes)))
        else:
            feature_by_cell = sparse.coo_matrix((len(features), len(barcodes)), dtype=np.float32)
        return feature_by_cell.transpose().tocsr().astype(np.float32), barcodes, features

    def _metadata_162335(self, context: AdapterContext, candidate: LoadCandidate, barcodes: list[str]) -> pd.DataFrame:
        path = context.supplementary_dir / "GSE162335_SingleCellMetadata.txt.gz"
        if not path.is_file():
            raise ValueError("GSE162335 cell metadata file is missing")
        metadata = pd.read_csv(path, sep="\t")
        required = {"orig.ident", "Barcodes", "nCount_RNA", "nFeature_RNA", "pct_mito"}
        if not required.issubset(metadata.columns):
            raise ValueError(f"GSE162335 metadata lacks columns: {sorted(required - set(metadata.columns))}")
        label = candidate.components["sample_label"]
        subset = metadata.loc[metadata["orig.ident"].astype(str) == label].copy()
        source_barcodes = []
        for cell in barcodes:
            if "." not in cell:
                raise ValueError(f"Expected donor-prefixed cell identifier, got {cell}")
            source_barcodes.append(cell.split(".", 1)[1])
        if subset["Barcodes"].astype(str).tolist() != source_barcodes:
            raise ValueError(f"Matrix cells do not exactly match metadata order for {label}")
        return subset.reset_index(drop=True)

    def load(self, context: AdapterContext, candidate: LoadCandidate) -> ad.AnnData:
        is_counts = context.gse_id == "GSE150115"
        x, barcodes, features = self._read_matrix(candidate, require_integer=is_counts)
        sample = context.sample_metadata.get(candidate.gsm_id, {})
        n_cells = len(barcodes)
        metadata = self._metadata_162335(context, candidate, barcodes) if not is_counts else None
        if is_counts:
            n_counts = np.asarray(x.sum(axis=1)).ravel().astype(np.int64).tolist()
            n_features = np.diff(x.indptr).astype(np.int32).tolist()
            percent_mito = [pd.NA] * n_cells
        else:
            assert metadata is not None
            n_counts = metadata["nCount_RNA"].astype("int64").tolist()
            n_features = metadata["nFeature_RNA"].astype("int32").tolist()
            percent_mito = metadata["pct_mito"].astype("float32").tolist()
        modality = context.manifest.get("stored_dataset_qualification", {}).get("modality") or pd.NA
        obs_values = {
            "sample_id": [candidate.gsm_id] * n_cells,
            "source_barcode": barcodes,
            "donor_id": [pd.NA] * n_cells,
            "organism": [sample.get("organism") or pd.NA] * n_cells,
            "tissue": [sample.get("tissue") or sample.get("tissue_source") or pd.NA] * n_cells,
            "cell_type": [pd.NA] * n_cells,
            "disease": [sample.get("disease") or sample.get("disease_status") or pd.NA] * n_cells,
            "condition": [sample.get("disease_status") or pd.NA] * n_cells,
            "assay": [pd.NA] * n_cells,
            "modality": [modality] * n_cells,
            "timepoint": [sample.get("timepoint") or sample.get("timepoint_value") or pd.NA] * n_cells,
            "source_accession": [context.gse_id] * n_cells,
            "n_counts": n_counts,
            "n_features": n_features,
            "percent_mito": percent_mito,
        }
        contract = context.canonical_schema["anndata_contract"]
        obs = self._typed_frame(n_cells, obs_values, contract["obs"])
        obs.index = pd.Index([f"{candidate.gsm_id}:{cell}" for cell in barcodes], name="cell_id")
        genome = "hg19" if is_counts else "GRCh38"
        var = self._typed_frame(len(features), {
            "feature_name": features,
            "feature_type": [pd.NA] * len(features),
            "genome_build": [genome] * len(features),
        }, contract["var"])
        var.index = pd.Index(features, name="feature_id")
        adata = ad.AnnData(X=x, obs=obs, var=var)
        if is_counts:
            adata.layers["counts"] = x.copy()
            adata.uns["counts_confirmed"] = True
        adata.uns["schema_version"] = context.canonical_schema["schema_version"]
        adata.uns["schema_id"] = context.canonical_schema["schema_id"]
        adata.uns["schema_sha256"] = context.schema_sha256
        adata.uns["source_gse"] = context.gse_id
        adata.uns["source_format"] = {
            "format": "dense gene-by-cell DGE TSV in TAR",
            "file": candidate.source_relative_path,
            "member": candidate.components["member"],
        }
        source_record = next((item for item in context.manifest.get("files", []) if item.get("filename") == candidate.source_path.name), None)
        if source_record and source_record.get("sha256"):
            adata.uns["source_format"]["source_sha256"] = source_record["sha256"]
        adata.uns["matrix_semantics"] = "raw counts" if is_counts else "normalized"
        adata.uns["feature_id_namespace"] = "source-supplied gene symbols" if is_counts else "source-supplied symbol-like gene labels"
        if is_counts:
            adata.uns["harmonization_notes"] = "Loaded unfiltered UMI-collapsed DGE values without normalization; raw-count semantics and hg19 were confirmed from GEO source documentation."
        else:
            adata.uns["harmonization_notes"] = "Loaded source normalized values without transformation; GEO documents Seurat LogNormalize (scale factor 10000). QC fields were joined by exact sample label, barcode set, and order. Source cell annotations were not copied; cell_type remains null."
        return adata


ADAPTER = DenseDgeTarAdapter()
