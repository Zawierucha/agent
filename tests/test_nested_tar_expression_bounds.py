from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))

from adapters import nested_tar_expression
from adapters.base import AdapterContext

spec = importlib.util.spec_from_file_location("geo_anndata_load", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)


def test_nested_tar_skips_oversized_nested_archive_member(tmp_path: Path, monkeypatch) -> None:
    nested = io.BytesIO()
    with tarfile.open(fileobj=nested, mode="w:gz") as archive:
        import gzip

        members = (
            ("matrix.mtx.gz", gzip.compress(b"%%MatrixMarket matrix coordinate integer general\n2 2 2\n1 1 1\n2 2 2\n")),
            ("barcodes.tsv.gz", gzip.compress(b"AA\nBB\n")),
            ("features.tsv.gz", gzip.compress(b"ENSG1\tGene A\tGene Expression\nENSG2\tGene B\tGene Expression\n")),
        )
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    payload = nested.getvalue()
    outer = tmp_path / "not_raw.TAR.GZ"
    with tarfile.open(outer, mode="w:gz") as archive:
        info = tarfile.TarInfo("GSM123_nested.tar.gz")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    monkeypatch.setattr(nested_tar_expression, "MAX_NESTED_MEMBER_BYTES", len(payload) - 1)
    schema = json.loads(Path("data/anndata_schema.json").read_text())
    context = AdapterContext("GSE12345", tmp_path, {"GSM123"}, {}, {}, {}, schema, "hash")

    assert nested_tar_expression.ADAPTER.discover(context) == []
    monkeypatch.setattr(nested_tar_expression, "MAX_NESTED_MEMBER_BYTES", len(payload))
    candidates = nested_tar_expression.ADAPTER.discover(context)
    assert len(candidates) == 1
    adata = nested_tar_expression.ADAPTER.load(context, candidates[0])
    loader.validate_adata(adata, context.canonical_schema)
