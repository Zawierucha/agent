from __future__ import annotations

import gzip
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))

from adapters.base import AdapterContext
from adapters.tenx_mtx import TenxMatrixMarketAdapter

spec = importlib.util.spec_from_file_location("geo_anndata_load", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)


def _context(root: Path) -> AdapterContext:
    schema = json.loads(Path("data/anndata_schema.json").read_text())
    return AdapterContext("GSE12345", root, {"GSM123"}, {}, {},
                          {"10x_candidate_groups": [{"complete_standard_10x_triplet": True}]}, schema, "hash")


def _files(prefix: str = "GSM123") -> dict[str, bytes]:
    return {
        f"{prefix}_matrix.mtx.gz": gzip.compress(b"%%MatrixMarket matrix coordinate integer general\n2 2 2\n1 1 1\n2 2 2\n"),
        f"{prefix}_barcodes.tsv.gz": gzip.compress(b"AA\nBB\n"),
        f"{prefix}_features.tsv.gz": gzip.compress(b"ENSG1\tGene A\tGene Expression\nENSG2\tGene B\tGene Expression\n"),
    }


def _add_tar(archive_path: Path, files: dict[str, bytes], mode: str) -> None:
    with tarfile.open(archive_path, mode) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_tenx_discover_finds_triplets_in_tar_gz(tmp_path: Path) -> None:
    archive = tmp_path / "triplet.tar.gz"
    _add_tar(archive, _files(), "w:gz")

    candidates = TenxMatrixMarketAdapter().discover(_context(tmp_path))

    assert len(candidates) == 1
    assert candidates[0].archive_member_map is True
    assert candidates[0].components.keys() == {"matrix", "barcodes", "features"}
    with TenxMatrixMarketAdapter()._component_stream(candidates[0], "matrix") as stream:
        assert b"MatrixMarket" in stream.read()
    adata = TenxMatrixMarketAdapter().load(_context(tmp_path), candidates[0])
    loader.validate_adata(adata, _context(tmp_path).canonical_schema)


def test_tenx_discover_finds_triplets_in_zip(tmp_path: Path) -> None:
    archive = tmp_path / "triplet.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for name, data in _files().items():
            handle.writestr(name, data)

    candidates = TenxMatrixMarketAdapter().discover(_context(tmp_path))

    assert len(candidates) == 1
    with TenxMatrixMarketAdapter()._component_stream(candidates[0], "barcodes") as stream:
        assert stream.read().decode().splitlines() == ["AA", "BB"]
    adata = TenxMatrixMarketAdapter().load(_context(tmp_path), candidates[0])
    loader.validate_adata(adata, _context(tmp_path).canonical_schema)


def test_tenx_discover_skips_unsafe_member_paths(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    unsafe = {f"../{name}": data for name, data in _files().items()}
    unsafe.update({f"C:/unsafe/{name}": data for name, data in _files().items()})
    _add_tar(archive, unsafe, "w:gz")

    assert TenxMatrixMarketAdapter().discover(_context(tmp_path)) == []


def test_tenx_existing_tar_and_loose_still_discovered(tmp_path: Path) -> None:
    archive = tmp_path / "old.tar"
    _add_tar(archive, _files(), "w")
    loose = _files("GSM123")
    for name, data in loose.items():
        (tmp_path / name).write_bytes(data)

    candidates = TenxMatrixMarketAdapter().discover(_context(tmp_path))

    assert len(candidates) == 2
    assert {candidate.archive_member_map for candidate in candidates} == {True, False}
    context = _context(tmp_path)
    for candidate in candidates:
        adata = TenxMatrixMarketAdapter().load(context, candidate)
        loader.validate_adata(adata, context.canonical_schema)
