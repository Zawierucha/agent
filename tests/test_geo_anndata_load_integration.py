from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

import anndata as ad
import duckdb
import numpy as np
import pytest
from conftest import make_geo_db

ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / ".agents" / "tools" / "sc_dataset"
SCHEMA = (ROOT / "data" / "anndata_schema.json").resolve()
sys.path.insert(0, str(TOOL_DIR))
spec = importlib.util.spec_from_file_location("geo_anndata_load_integration", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)

MATRIX = """%%MatrixMarket matrix coordinate integer general
% fixture
3 2 3
1 1 1
2 1 2
3 2 5
"""
BARCODES = "AAAC-1\nTTTG-1\n"
FEATURES = "ENSG000001\tGene A\tGene Expression\nENSG000002\tGene B\tGene Expression\nENSG000003\tGene C\tGene Expression\n"
EXPECTED = np.array([[1, 2, 0], [0, 0, 5]], dtype=np.float32)


def _put_triplet(root: Path, gsm: str, prefix: str | None = None, corrupt: bool = False) -> None:
    prefix = prefix or f"{gsm}_P1"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{prefix}_matrix.mtx.gz").write_bytes(b"not gzip matrix" if corrupt else gzip.compress(MATRIX.encode()))
    (root / f"{prefix}_barcodes.tsv.gz").write_bytes(gzip.compress(BARCODES.encode()))
    (root / f"{prefix}_features.tsv.gz").write_bytes(gzip.compress(FEATURES.encode()))


def _inventory(dataset: Path, gse: str, *, tenx: bool) -> None:
    supplementary = dataset / "supplementary"
    inventory = []
    manifest_files = []
    for path in sorted(p for p in supplementary.rglob("*") if p.is_file()):
        relative = str(path.relative_to(supplementary))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        inventory.append({"relative_path": relative, "format_guess": "matrix_market" if "matrix.mtx" in path.name else "supplementary", "sha256": digest})
        manifest_files.append({"filename": path.name, "sha256": digest})
    (dataset / "manifest.json").write_text(json.dumps({
        "gse_id": gse,
        "catalogued_gsm_ids": sorted({gsm for p in supplementary.rglob("*") if p.is_file() for gsm in re.findall(r"GSM\d+", p.name)}),
        "files": manifest_files,
    }), encoding="utf-8")
    report: dict[str, Any] = {"gse_id": gse, "files": inventory}
    if tenx:
        report["10x_candidate_groups"] = [{"complete_standard_10x_triplet": True}]
    (dataset / "format_report.json").write_text(json.dumps(report), encoding="utf-8")


def _seed(db: Path, gse: str, gsms: list[str], *, dataset_decision: str = "include",
          decisions: dict[str, str] | None = None, metadata: set[str] | None = None,
          organism: str | None = "Homo sapiens") -> None:
    decisions = decisions or {}
    metadata = set(gsms) if metadata is None else metadata
    with duckdb.connect(str(db)) as con:
        con.execute("INSERT INTO geo_datasets (gse_id,organism) VALUES (?,?)", [gse, organism])
        con.execute("INSERT INTO geo_qualification (gse_id,qualification,is_human,is_single_cell) VALUES (?, ?, TRUE, TRUE)", [gse, dataset_decision])
        for gsm in gsms:
            con.execute("INSERT INTO geo_samples (gsm_id,gse_id,organism) VALUES (?,?,?)", [gsm, gse, organism])
            con.execute("INSERT INTO geo_sample_qualification (gsm_id,qualification) VALUES (?,?)", [gsm, decisions.get(gsm, "include")])
            if gsm in metadata:
                con.execute("INSERT INTO geo_sample_metadata (gsm_id,disease,tissue) VALUES (?,'UC','colon')", [gsm])


def _setup(tmp: Path, gse: str, gsms: list[str], *, corrupt: str | None = None,
           decision: str = "include", sample_decisions: dict[str, str] | None = None,
           metadata: set[str] | None = None, organism: str | None = "Homo sapiens"):
    db = make_geo_db(tmp / "geo.duckdb")
    downloads, output = tmp / "downloads", tmp / "output"
    dataset = downloads / gse
    supp = dataset / "supplementary"
    for gsm in gsms:
        _put_triplet(supp, gsm, corrupt=(gsm == corrupt))
    _inventory(dataset, gse, tenx=True)
    _seed(db, gse, gsms, dataset_decision=decision, decisions=sample_decisions, metadata=metadata, organism=organism)
    return db, downloads, output, supp


def _nested_archive(supp: Path, gsm: str) -> None:
    inner = io.BytesIO()
    entries = {
        "matrix.mtx.gz": gzip.compress(b"%%MatrixMarket matrix coordinate integer general\n2 2 2\n1 1 4\n2 2 2\n"),
        "barcodes.tsv.gz": gzip.compress(b"CELL_A\nCELL_B\n"),
        "features.tsv.gz": gzip.compress(b"ENSG100\tGene 100\tGene Expression\nENSG200\tGene 200\tGene Expression\n"),
    }
    with tarfile.open(fileobj=inner, mode="w:gz") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    with tarfile.open(supp / "GSE81000_RAW.tar", mode="w") as archive:
        payload = inner.getvalue()
        info = tarfile.TarInfo(f"{gsm}_sample.tar.gz")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def _args(gse: str, db: Path, downloads: Path, output: Path, run_id: str | None = None, schema: Path = SCHEMA):
    import argparse
    return argparse.Namespace(gse=gse, db=db, download_root=downloads, output_root=output,
                              schema=schema, run_id=run_id, max_iterations=10, adapter=None, pretty=False)


def _rows(db: Path, sql: str, params: list[Any] | None = None):
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


def test_standard_10x_gse_end_to_end(tmp_path: Path) -> None:
    gse, gsm = "GSE81001", "GSM810001"
    db, downloads, output, _ = _setup(tmp_path, gse, [gsm])
    result = loader.run(_args(gse, db, downloads, output))
    path = output / gse / f"{gsm}.h5ad"
    assert result["status"] == "success" and path.is_file()
    assert _rows(db, "SELECT status FROM geo_anndata_import_runs WHERE run_id=?", [result["run_id"]]) == [("success",)]
    a = ad.read_h5ad(path)
    assert a.shape == (2, 3)
    np.testing.assert_array_equal(a.X.toarray(), EXPECTED)
    assert a.obs.index.name == "cell_id" and a.obs.index.tolist() == [f"{gsm}:AAAC-1", f"{gsm}:TTTG-1"]
    assert a.var.index.name == "feature_id" and a.var.index.tolist() == ["ENSG000001", "ENSG000002", "ENSG000003"]
    assert a.uns["schema_version"] == "1.1"
    assert a.uns["matrix_semantics"] in {"raw counts", "normalized", "unknown"}
    assert _rows(db, "SELECT n_obs,n_vars FROM geo_anndata_import_samples WHERE run_id=? AND gsm_id=?", [result["run_id"], gsm]) == [(a.n_obs, a.n_vars)]


def test_multi_sample_gse_loads_each_gsm(tmp_path: Path) -> None:
    gse, gsms = "GSE81002", ["GSM810011", "GSM810012", "GSM810013"]
    db, downloads, output, _ = _setup(tmp_path, gse, gsms)
    result = loader.run(_args(gse, db, downloads, output))
    assert result["status"] == "success" and result["loaded_samples"] == 3
    for gsm in gsms:
        a = ad.read_h5ad(output / gse / f"{gsm}.h5ad")
        assert a.obs["sample_id"].tolist() == [gsm, gsm]
        assert all(str(cell).startswith(gsm + ":") for cell in a.obs.index)


def test_other_supported_format_loads(tmp_path: Path) -> None:
    gse, gsm = "GSE81000", "GSM810020"
    db = make_geo_db(tmp_path / "geo.duckdb")
    downloads, output = tmp_path / "downloads", tmp_path / "output"
    dataset, supp = downloads / gse, downloads / gse / "supplementary"
    supp.mkdir(parents=True)
    _nested_archive(supp, gsm)
    _inventory(dataset, gse, tenx=False)
    _seed(db, gse, [gsm])
    result = loader.run(_args(gse, db, downloads, output))
    a = ad.read_h5ad(output / gse / f"{gsm}.h5ad")
    assert result["status"] == "success" and a.uns["matrix_semantics"] == "raw counts"
    assert "counts" in a.layers
    assert _rows(db, "SELECT adapter_name FROM geo_anndata_import_samples WHERE run_id=? AND gsm_id=?", [result["run_id"], gsm]) == [("nested_tar_expression",)]


def test_missing_metadata_columns_remain_null(tmp_path: Path) -> None:
    gse, gsm = "GSE81003", "GSM810030"
    db, downloads, output, _ = _setup(tmp_path, gse, [gsm], metadata=set(), organism=None)
    result = loader.run(_args(gse, db, downloads, output))
    a = ad.read_h5ad(output / gse / f"{gsm}.h5ad")
    assert result["status"] == "success"
    for field in ("tissue", "disease", "organism"):
        assert a.obs[field].isna().all()
    schema, _ = loader.load_schema(SCHEMA)
    loader.validate_adata(a, schema)


def test_ambiguous_candidates_refused(tmp_path: Path) -> None:
    gse, gsm = "GSE81004", "GSM810040"
    db = make_geo_db(tmp_path / "geo.duckdb")
    downloads, output = tmp_path / "downloads", tmp_path / "output"
    dataset, supp = downloads / gse, downloads / gse / "supplementary"
    _put_triplet(supp, gsm, f"{gsm}_first")
    _put_triplet(supp, gsm, f"{gsm}_second")
    _inventory(dataset, gse, tenx=True)
    _seed(db, gse, [gsm])
    result = loader.run(_args(gse, db, downloads, output))
    assert result["sample_results"][gsm]["status"] == "needs_adapter"
    assert not (output / gse / f"{gsm}.h5ad").exists()
    assert _rows(db, "SELECT status FROM geo_anndata_import_samples WHERE run_id=? AND gsm_id=?", [result["run_id"], gsm]) == [("needs_adapter",)]
    statuses = _rows(db, "SELECT status FROM geo_anndata_import_attempts WHERE run_id=? AND gsm_id=?", [result["run_id"], gsm])
    assert len(statuses) == 2 and {row[0] for row in statuses} == {"ambiguous_candidates"}


def test_corrupt_source_file_fails_in_isolation(tmp_path: Path) -> None:
    gse, good, bad = "GSE81005", "GSM810051", "GSM810052"
    db, downloads, output, _ = _setup(tmp_path, gse, [good, bad], corrupt=bad)
    result = loader.run(_args(gse, db, downloads, output))
    assert result["status"] == "partial"
    assert (output / gse / f"{good}.h5ad").is_file()
    assert not (output / gse / f"{bad}.h5ad").exists()
    assert _rows(db, "SELECT status,error_category FROM geo_anndata_import_samples WHERE run_id=? AND gsm_id=?", [result["run_id"], bad]) == [("failed", "read_or_validation_error")]
    report = json.loads((output / gse / "anndata_import_report.json").read_text())
    assert {good, bad}.issubset(report["sample_results"])


def test_unconvertible_format_needs_adapter(tmp_path: Path) -> None:
    gse, gsm = "GSE81006", "GSM810060"
    db = make_geo_db(tmp_path / "geo.duckdb")
    downloads, output = tmp_path / "downloads", tmp_path / "output"
    dataset, supp = downloads / gse, downloads / gse / "supplementary"
    supp.mkdir(parents=True)
    (supp / f"{gsm}_reads.fastq.gz").write_bytes(gzip.compress(b"@read\nACGT\n+\n!!!!\n"))
    _inventory(dataset, gse, tenx=False)
    _seed(db, gse, [gsm])
    result = loader.run(_args(gse, db, downloads, output))
    assert result["sample_results"][gsm]["status"] == "needs_adapter"
    assert result["sample_results"][gsm]["error_category"] in {"no_matching_adapter", "no_compatible_adapter"}
    assert result["status"] == "needs_adapter" and not (output / gse / f"{gsm}.h5ad").exists()


def test_failure_isolation_preserves_successes(tmp_path: Path) -> None:
    gse, good, bad = "GSE81007", "GSM810071", "GSM810072"
    db, downloads, output, supp = _setup(tmp_path, gse, [good, bad], corrupt=bad)
    first = loader.run(_args(gse, db, downloads, output))
    success_path = output / gse / f"{good}.h5ad"
    old_hash = hashlib.sha256(success_path.read_bytes()).hexdigest()
    (supp / f"{bad}_P1_matrix.mtx.gz").write_bytes(gzip.compress(MATRIX.encode()))
    resumed = loader.run(_args(gse, db, downloads, output, first["run_id"] ))
    assert first["status"] == "partial" and resumed["status"] == "success"
    assert hashlib.sha256(success_path.read_bytes()).hexdigest() == old_hash
    assert _rows(db, "SELECT COUNT(*) FROM geo_anndata_import_attempts WHERE run_id=? AND iteration_number=2 AND gsm_id=?", [first["run_id"], good]) == [(0,)]
    # Regression: per-file statuses must aggregate the whole run history, not
    # only the current iteration (a resumed iteration used to erase success).
    assert _rows(db, "SELECT status FROM geo_anndata_import_files WHERE run_id=? AND source_relative_path=?",
                 [first["run_id"], f"{good}_P1_matrix.mtx.gz"]) == [("success",)]
    assert _rows(db, "SELECT status FROM geo_anndata_import_files WHERE run_id=? AND source_relative_path=?",
                 [first["run_id"], f"{bad}_P1_matrix.mtx.gz"]) == [("partial",)]


def test_resume_does_not_reprocess_successes(tmp_path: Path) -> None:
    gse, good, bad = "GSE81008", "GSM810081", "GSM810082"
    db, downloads, output, supp = _setup(tmp_path, gse, [good, bad], corrupt=bad)
    first = loader.run(_args(gse, db, downloads, output))
    old_path = first["sample_results"][good]["output_path"]
    (supp / f"{bad}_P1_matrix.mtx.gz").write_bytes(gzip.compress(MATRIX.encode()))
    resumed = loader.run(_args(gse, db, downloads, output, first["run_id"] ))
    assert resumed["iteration"] == 2
    assert resumed["sample_results"][good]["status"] == "success"
    assert resumed["sample_results"][good]["output_path"] == old_path
    assert _rows(db, "SELECT COUNT(*) FROM geo_anndata_import_attempts WHERE run_id=? AND iteration_number=2 AND gsm_id=?", [first["run_id"], good]) == [(0,)]


def test_h5ad_roundtrip_preserves_ids_and_shape(tmp_path: Path) -> None:
    gse, gsm = "GSE81009", "GSM810090"
    db, downloads, output, supp = _setup(tmp_path, gse, [gsm])
    result = loader.run(_args(gse, db, downloads, output))
    a = ad.read_h5ad(output / gse / f"{gsm}.h5ad")
    schema, _ = loader.load_schema(SCHEMA)
    loader.validate_adata(a, schema)
    source_features = gzip.decompress((supp / f"{gsm}_P1_features.tsv.gz").read_bytes()).decode().splitlines()
    source_barcodes = gzip.decompress((supp / f"{gsm}_P1_barcodes.tsv.gz").read_bytes()).decode().splitlines()
    assert result["status"] == "success"
    assert a.var.index.tolist() == [line.split("\t")[0] for line in source_features]
    assert a.obs.index.tolist() == [f"{gsm}:{barcode}" for barcode in source_barcodes]
    assert a.X.nnz == int(np.count_nonzero(EXPECTED)) and float(a.X.sum()) == float(EXPECTED.sum())


def test_dataset_requires_include_decision(tmp_path: Path) -> None:
    gse, gsm = "GSE81010", "GSM810100"
    db, downloads, output, _ = _setup(tmp_path, gse, [gsm], decision="exclude")
    with pytest.raises(ValueError, match="requires include"):
        loader.run(_args(gse, db, downloads, output))
    assert _rows(db, "SELECT COUNT(*) FROM geo_anndata_import_runs WHERE gse_id=?", [gse]) == [(0,)]


def test_not_eligible_samples_recorded(tmp_path: Path) -> None:
    gse, included, uncertain = "GSE81011", ["GSM810111", "GSM810112"], "GSM810113"
    db, downloads, output, _ = _setup(tmp_path, gse, included)
    with duckdb.connect(str(db)) as con:
        con.execute("INSERT INTO geo_samples (gsm_id,gse_id,organism) VALUES (?,?,'Homo sapiens')", [uncertain, gse])
        con.execute("INSERT INTO geo_sample_qualification (gsm_id,qualification) VALUES (?,'uncertain')", [uncertain])
    result = loader.run(_args(gse, db, downloads, output))
    assert result["status"] == "success" and result["loaded_samples"] == 2
    row = _rows(db, "SELECT status,last_error FROM geo_anndata_import_samples WHERE run_id=? AND gsm_id=?", [result["run_id"], uncertain])
    assert len(row) == 1 and row[0][0] == "not_eligible" and "uncertain" in row[0][1].lower()
    assert set(result["sample_results"]) == set(included)


def test_schema_change_refuses_resume(tmp_path: Path) -> None:
    gse, good, bad = "GSE81012", "GSM810121", "GSM810122"
    db, downloads, output, supp = _setup(tmp_path, gse, [good, bad], corrupt=bad)
    first = loader.run(_args(gse, db, downloads, output))
    assert first["status"] == "partial"
    modified = tmp_path / "changed-schema.json"
    modified.write_bytes(SCHEMA.read_bytes() + b" ")
    with pytest.raises(ValueError, match="schema changed"):
        loader.run(_args(gse, db, downloads, output, first["run_id"], modified))
    assert _rows(db, "SELECT iteration_count,status FROM geo_anndata_import_runs WHERE run_id=?", [first["run_id"]]) == [(1, "partial")]
    (supp / f"{bad}_P1_matrix.mtx.gz").write_bytes(gzip.compress(MATRIX.encode()))
    resumed = loader.run(_args(gse, db, downloads, output, first["run_id"]))
    assert resumed["iteration"] == 2 and resumed["status"] == "success"
