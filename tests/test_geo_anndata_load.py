from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse


TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))
spec = importlib.util.spec_from_file_location("geo_anndata_load", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)


def test_nullable_float32_contract_roundtrips_through_h5ad(tmp_path: Path) -> None:
    schema, _ = loader.load_schema(Path("data/anndata_schema.json"))
    obs = pd.DataFrame(index=pd.Index(["GSM1:cell"], name="cell_id"))
    for name, field in schema["anndata_contract"]["obs"].items():
        if name == "index":
            continue
        obs[name] = pd.Series([pd.NA], index=obs.index, dtype=field["dtype"])
    var = pd.DataFrame(index=pd.Index(["gene1"], name="feature_id"))
    for name, field in schema["anndata_contract"]["var"].items():
        if name != "index":
            var[name] = pd.Series([pd.NA], index=var.index, dtype=field["dtype"])
    adata = ad.AnnData(X=sparse.csr_matrix(np.array([[1]], dtype=np.float32)), obs=obs, var=var)
    adata.uns.update(
        schema_version=schema["schema_version"],
        source_gse="GSE1",
        source_format="test",
        matrix_semantics="unknown",
        feature_id_namespace="unknown",
        harmonization_notes="test",
    )

    output = tmp_path / "sample.h5ad"
    loader._write_sample(adata, output, schema)

    reopened = ad.read_h5ad(output)
    loader.validate_adata(reopened, schema)
    assert str(reopened.obs["percent_mito"].dtype) == "float32"
