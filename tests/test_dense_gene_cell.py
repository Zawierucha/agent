from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))

from adapters.base import AdapterContext
from adapters.dense_gene_cell import ADAPTER

spec = importlib.util.spec_from_file_location("geo_anndata_load", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)


def _context(root: Path) -> AdapterContext:
    schema = json.loads(Path("data/anndata_schema.json").read_text())
    return AdapterContext("GSE12345", root, {"GSM123"}, {"GSM123": {"organism": "Homo sapiens"}},
                          {"files": []}, {}, schema, "hash")


def test_dense_gene_cell_admits_evidenced_header(tmp_path: Path) -> None:
    path = tmp_path / "GSM123_expression.tsv"
    path.write_text("gene\tBC1\tBC2\nENSG1\t1\t0\nENSG2\t0\t3\n")
    context = _context(tmp_path)

    candidates = ADAPTER.discover(context)
    assert len(candidates) == 1
    adata = ADAPTER.load(context, candidates[0])

    assert adata.shape == (2, 2)
    assert adata.obs["source_barcode"].tolist() == ["BC1", "BC2"]
    assert adata.var.index.tolist() == ["ENSG1", "ENSG2"]
    assert adata.uns["matrix_semantics"] == "raw counts"
    assert "counts" in adata.layers
    assert "orientation taken from header evidence; cells are columns" in adata.uns["harmonization_notes"]
    loader.validate_adata(adata, context.canonical_schema)


def test_dense_gene_cell_refuses_ambiguous_header(tmp_path: Path) -> None:
    path = tmp_path / "GSM123_expression.tsv"
    path.write_text("Barcode\tBC1\tBC2\nENSG1\t1\t0\n")

    assert ADAPTER.discover(_context(tmp_path)) == []
