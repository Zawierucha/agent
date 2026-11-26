from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import h5py
import numpy as np

TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))

from adapters.base import AdapterContext
from adapters.tenx_hdf5 import ADAPTER

spec = importlib.util.spec_from_file_location("geo_anndata_load", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)


def _context(tmp_path: Path, source: Path) -> AdapterContext:
    schema = json.loads(Path("data/anndata_schema.json").read_text())
    return AdapterContext(
        gse_id="GSE12345",
        supplementary_dir=tmp_path,
        include_gsms={"GSM123"},
        sample_metadata={"GSM123": {"patient_id": "patient-1", "organism": "Homo sapiens", "tissue": "colon", "disease": "UC"}},
        manifest={"files": [{"filename": source.name, "sha256": "abc123"}]},
        format_report={},
        canonical_schema=schema,
        schema_sha256="schema-hash",
    )


def _write_v3(path: Path, values: list[float | int]) -> None:
    data = np.asarray(values)
    indices = np.array([0, 2, 1, 2])
    indptr = np.array([0, 2, 4])
    with h5py.File(path, "w") as handle:
        matrix = handle.create_group("matrix")
        matrix.create_dataset("data", data=data)
        matrix.create_dataset("indices", data=indices)
        matrix.create_dataset("indptr", data=indptr)
        matrix.create_dataset("shape", data=np.asarray((3, 2), dtype=np.int64))
        matrix.create_dataset("barcodes", data=np.asarray([b"AA-1", b"BB-1"]))
        features = matrix.create_group("features")
        features.create_dataset("name", data=np.asarray([b"Gene A", b"Gene B", b"Gene C"]))
        features.create_dataset("feature_type", data=np.asarray([b"Gene Expression"] * 3))
        features.create_dataset("id", data=np.asarray([b"ENSG0001", b"ENSG0002", b"ENSG0003"]))


def test_tenx_hdf5_standard_layout_loads(tmp_path: Path) -> None:
    source = tmp_path / "GSM123_matrix.h5"
    _write_v3(source, [1, 2, 3, 4])
    context = _context(tmp_path, source)

    candidates = ADAPTER.discover(context)
    assert len(candidates) == 1
    adata = ADAPTER.load(context, candidates[0])

    assert adata.shape == (2, 3)
    np.testing.assert_array_equal(adata.X.toarray(), [[1, 0, 2], [0, 3, 4]])
    assert adata.obs["source_barcode"].tolist() == ["AA-1", "BB-1"]
    assert adata.var.index.tolist() == ["ENSG0001", "ENSG0002", "ENSG0003"]
    assert adata.uns["matrix_semantics"] == "raw counts"
    assert "counts" in adata.layers
    loader.validate_adata(adata, context.canonical_schema)


def test_tenx_hdf5_rejects_unknown_layout(tmp_path: Path) -> None:
    source = tmp_path / "GSM123_unknown.h5"
    with h5py.File(source, "w") as handle:
        handle.create_dataset("not_a_matrix", data=[1, 2, 3])
    context = _context(tmp_path, source)

    assert ADAPTER.discover(context) == []


def test_tenx_hdf5_noninteger_values_marked_unknown(tmp_path: Path) -> None:
    source = tmp_path / "GSM123_matrix.h5"
    _write_v3(source, [1.0, 2.5, 3.0, 4.0])
    context = _context(tmp_path, source)

    candidate = ADAPTER.discover(context)[0]
    adata = ADAPTER.load(context, candidate)

    assert adata.uns["matrix_semantics"] == "unknown"
    assert "counts" not in adata.layers
    loader.validate_adata(adata, context.canonical_schema)
