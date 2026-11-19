from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
import sys
import tarfile

import numpy as np
import pandas as pd

TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))

from adapters.base import AdapterContext
from adapters.dense_dge_tar import ADAPTER


def _schema() -> dict:
    return json.loads(Path("data/anndata_schema.json").read_text())


def _tar_member(path: Path, name: str, text: str) -> None:
    compressed = gzip.compress(text.encode())
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(compressed)
        archive.addfile(info, io.BytesIO(compressed))


def _context(tmp_path: Path, gse: str, gsm: str, archive: Path) -> AdapterContext:
    return AdapterContext(
        gse_id=gse,
        supplementary_dir=tmp_path,
        include_gsms={gsm},
        sample_metadata={gsm: {"organism": "Homo sapiens", "tissue": "colon", "disease": "UC", "disease_status": "inflamed"}},
        manifest={"files": [{"filename": archive.name, "sha256": "abc"}], "stored_dataset_qualification": {"modality": "scRNA-seq"}},
        format_report={}, canonical_schema=_schema(), schema_sha256="hash",
    )


def test_gse150115_dense_dge_loads_confirmed_counts(tmp_path: Path) -> None:
    archive = tmp_path / "GSE150115_RAW.tar"
    _tar_member(archive, "GSM4523789_reads.test.umi.dge.txt.gz", "GENE\tBC1\tBC2\nG1\t1\t0\nG2\t2\t3\n")
    context = _context(tmp_path, "GSE150115", "GSM4523789", archive)

    candidates = ADAPTER.discover(context)
    assert len(candidates) == 1
    adata = ADAPTER.load(context, candidates[0])

    assert adata.shape == (2, 2)
    np.testing.assert_array_equal(adata.X.toarray(), [[1, 2], [0, 3]])
    assert adata.uns["matrix_semantics"] == "raw counts"
    assert adata.uns["counts_confirmed"] is True
    assert "counts" in adata.layers
    assert list(adata.obs["n_counts"].astype(int)) == [3, 3]
    assert adata.var["genome_build"].tolist() == ["hg19", "hg19"]


def test_gse162335_dense_dge_loads_normalized_values_and_qc(tmp_path: Path) -> None:
    archive = tmp_path / "GSE162335_RAW.tar"
    _tar_member(archive, "GSM4949927_SingleCellCounts_UCI_1.txt.gz", "Gene\tAG.BC1\tAG.BC2\n7SK.1\t0.0\t1.25\nG2\t2.5\t0.0\n")
    pd.DataFrame({
        "orig.ident": ["UCI_1", "UCI_1"], "Barcodes": ["BC1", "BC2"],
        "pouch.status": ["UC_inflamed", "UC_inflamed"],
        "MajorPopulations": ["x", "y"], "MinorPopulations": ["a", "b"],
        "nCount_RNA": [10, 20], "nFeature_RNA": [2, 1], "pct_mito": [1.5, 2.5],
    }).to_csv(tmp_path / "GSE162335_SingleCellMetadata.txt.gz", sep="\t", index=False, compression="gzip")
    context = _context(tmp_path, "GSE162335", "GSM4949927", archive)

    candidates = ADAPTER.discover(context)
    assert len(candidates) == 1
    adata = ADAPTER.load(context, candidates[0])

    assert adata.shape == (2, 2)
    np.testing.assert_array_equal(adata.X.toarray(), [[0, 2.5], [1.25, 0]])
    assert adata.uns["matrix_semantics"] == "normalized"
    assert "counts" not in adata.layers
    assert list(adata.obs["n_counts"].astype(int)) == [10, 20]
    assert list(adata.obs["percent_mito"].astype(float)) == [1.5, 2.5]
    assert adata.obs["cell_type"].isna().all()
    assert adata.var["genome_build"].tolist() == ["GRCh38", "GRCh38"]
