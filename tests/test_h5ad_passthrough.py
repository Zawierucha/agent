from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse

TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))

from adapters.base import AdapterContext
from adapters.h5ad_passthrough import ADAPTER

spec = importlib.util.spec_from_file_location("geo_anndata_load", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)


def _source(tmp_path: Path, x: np.ndarray, *, extras: bool = False) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    obs = pd.DataFrame({
        "sample_id": ["original", "original"], "source_barcode": ["AA", "BB"],
        "donor_id": ["d1", "d1"], "organism": ["human", "human"],
        "tissue": ["colon", "colon"], "cell_type": ["tumor", "normal"],
        "disease": ["UC", "UC"], "condition": ["case", "case"], "assay": ["RNA", "RNA"],
        "modality": ["scRNA", "scRNA"], "timepoint": ["T1", "T1"],
        "source_accession": ["GSE-old", "GSE-old"], "n_counts": [4, 5],
        "n_features": [2, 3], "percent_mito": [1.5, 2.5],
    }, index=pd.Index(["cell-a", "cell-b"], name=None))
    var = pd.DataFrame({"feature_name": ["Gene A", "Gene B"], "feature_type": ["RNA", "RNA"],
                        "assembly": ["GRCh38", "GRCh38"]}, index=pd.Index(["ENSG1", "ENSG2"], name="feature_id"))
    if extras:
        obs["private_annotation"] = ["x", "y"]
        var["unused_var_field"] = ["u", "v"]
    source = ad.AnnData(X=sparse.csr_matrix(x), obs=obs, var=var)
    path = tmp_path / "GSM123_source.h5ad"
    source.write_h5ad(path)
    return path


def _context(tmp_path: Path) -> AdapterContext:
    schema = json.loads(Path("data/anndata_schema.json").read_text())
    return AdapterContext("GSE12345", tmp_path, {"GSM123"}, {"GSM123": {"patient_id": "p1"}},
                          {"files": []}, {}, schema, "schema-hash")


def _load(tmp_path: Path, values: np.ndarray, *, extras: bool = False):
    _source(tmp_path, values, extras=extras)
    context = _context(tmp_path)
    candidates = ADAPTER.discover(context)
    assert len(candidates) == 1
    return ADAPTER.load(context, candidates[0]), context


def test_h5ad_passthrough_maps_contract_columns(tmp_path: Path) -> None:
    adata, context = _load(tmp_path, np.array([[1, 0], [0, 2]], dtype=np.int32))

    assert adata.shape == (2, 2)
    assert adata.obs.index.tolist() == ["GSM123:cell-a", "GSM123:cell-b"]
    assert adata.obs["source_barcode"].tolist() == ["cell-a", "cell-b"]
    assert adata.obs["sample_id"].tolist() == ["original", "original"]
    assert adata.obs["cell_type"].isna().all()
    assert adata.var["feature_name"].tolist() == ["Gene A", "Gene B"]
    assert adata.var["genome_build"].tolist() == ["GRCh38", "GRCh38"]
    assert "assembly" in adata.uns["harmonization_notes"]
    loader.validate_adata(adata, context.canonical_schema)


def test_h5ad_passthrough_counts_layer_only_when_integer(tmp_path: Path) -> None:
    integer, context = _load(tmp_path / "integer", np.array([[1, 0], [0, 2]], dtype=np.int32))
    fractional, fractional_context = _load(tmp_path / "fractional", np.array([[1.00000001, 0], [0, 2]], dtype=np.float64))

    assert integer.uns["matrix_semantics"] == "raw counts"
    assert "counts" in integer.layers
    assert fractional.uns["matrix_semantics"] == "unknown"
    assert "counts" not in fractional.layers
    loader.validate_adata(integer, context.canonical_schema)
    loader.validate_adata(fractional, fractional_context.canonical_schema)


def test_h5ad_passthrough_extras_recorded_and_celltype_null(tmp_path: Path) -> None:
    adata, context = _load(tmp_path, np.array([[1, 0], [0, 2]], dtype=np.int32), extras=True)

    assert adata.obs["cell_type"].isna().all()
    notes = adata.uns["harmonization_notes"]
    assert "private_annotation" in notes and "unused_var_field" in notes
    assert "cell_type annotations; not copied" in notes
    assert "x" not in notes and "tumor" not in notes
    loader.validate_adata(adata, context.canonical_schema)
