from __future__ import annotations

import gzip
import json
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest

from tools.sc_dataset import geo_format_inspect as inspector


def test_classify_name_recognizes_common_geo_files() -> None:
    assert inspector.classify_name("GSM1_barcodes.tsv.gz") == "cell_barcodes"
    assert inspector.classify_name("features.tsv.gz") == "gene_or_feature_annotations"
    assert inspector.classify_name("GSM1_matrix.mtx.gz") == "matrix_market_expression_matrix"
    assert inspector.classify_name("counts.h5ad") == "anndata_h5ad"
    assert inspector.classify_name("unknown.bin") == "unknown"


def test_sniff_magic_detects_gzip_and_matrix_market(tmp_path: Path) -> None:
    gz = tmp_path / "sample.tsv.gz"
    with gzip.open(gz, "wb") as handle:
        handle.write(b"a\tb\n1\t2\n")
    matrix = tmp_path / "matrix.mtx"
    matrix.write_text("%%MatrixMarket matrix coordinate integer general\n", encoding="utf-8")
    assert inspector.sniff_magic(gz)["magic"] == "gzip"
    assert inspector.sniff_magic(matrix)["magic"] == "matrix_market"


def test_group_10x_candidates_detects_complete_and_partial_bundles() -> None:
    items = [
        {"name": "GSM1_P1_barcodes.tsv.gz"},
        {"name": "GSM1_P1_features.tsv.gz"},
        {"name": "GSM1_P1_matrix.mtx.gz"},
        {"name": "GSM2_P2_barcodes.tsv.gz"},
        {"name": "GSM2_P2_matrix.mtx.gz"},
    ]
    groups = inspector.group_10x_candidates(items)
    assert len(groups) == 2
    by_prefix = {item["sample_or_bundle_prefix"]: item for item in groups}
    assert by_prefix["GSM1_P1"]["complete_standard_10x_triplet"] is True
    assert by_prefix["GSM1_P1"]["missing_components"] == []
    assert by_prefix["GSM2_P2"]["complete_standard_10x_triplet"] is False
    assert by_prefix["GSM2_P2"]["missing_components"] == ["features"]


def test_group_10x_candidates_keeps_samples_separate_by_archive() -> None:
    items = [
        {"name": "archive1.tar::GSM1_barcodes.tsv.gz"},
        {"name": "archive1.tar::GSM1_features.tsv.gz"},
        {"name": "archive1.tar::GSM1_matrix.mtx.gz"},
        {"name": "archive2.tar::GSM1_barcodes.tsv.gz"},
        {"name": "archive2.tar::GSM1_features.tsv.gz"},
    ]
    groups = inspector.group_10x_candidates(items)
    assert len(groups) == 2
    assert sum(item["complete_standard_10x_triplet"] for item in groups) == 1


def test_inspect_archive_lists_tar_members_without_extracting(tmp_path: Path) -> None:
    archive_path = tmp_path / "GSE123_RAW.tar"
    source = tmp_path / "GSM1_matrix.mtx.gz"
    source.write_bytes(b"matrix")
    with tarfile.open(archive_path, "w") as archive:
        archive.add(source, arcname="GSM1_matrix.mtx.gz")
    source.unlink()
    result = inspector.inspect_archive(archive_path)
    assert result["archive"] is True
    assert result["member_count"] == 1
    assert result["members"][0]["name"] == "GSM1_matrix.mtx.gz"
    assert not source.exists()


def test_find_input_root_prefers_supplementary_directory(tmp_path: Path) -> None:
    supplementary = tmp_path / "GSE123456" / "supplementary"
    supplementary.mkdir(parents=True)
    assert inspector.find_input_root(tmp_path, "GSE123456", None) == supplementary.resolve()


def test_find_input_root_rejects_missing_dataset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Dataset download directory does not exist"):
        inspector.find_input_root(tmp_path, "GSE000001", None)


def test_load_manifest_reads_manifest_and_reports_missing_manifest(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"gse_id": "GSE123456", "catalogued_gsm_ids": ["GSM1"]}), encoding="utf-8")
    manifest, path = inspector.load_manifest(tmp_path)
    assert manifest["gse_id"] == "GSE123456"
    assert path.name == "manifest.json"
    assert inspector.load_manifest(tmp_path / "missing")[0] is None


def test_inspect_file_reports_hash_and_gzip_text_sample(tmp_path: Path) -> None:
    path = tmp_path / "GSM1_barcodes.tsv.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"barcode\ncell1\ncell2\n")
    result = inspector.inspect_file(path, tmp_path, False)
    assert result["format_guess"] == "cell_barcodes"
    assert len(result["sha256"]) == 64
    assert result["compressed_content_sample"]["readable_text_sample"] is True
    assert result["compressed_content_sample"]["nonempty_lines_sampled"] == 3


def test_analyze_reports_complete_triplets_and_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gse_dir = tmp_path / "GSE123456"
    supplementary = gse_dir / "supplementary"
    supplementary.mkdir(parents=True)
    (gse_dir / "manifest.json").write_text(json.dumps({"gse_id": "GSE123456", "catalogued_gsm_ids": ["GSM1"]}), encoding="utf-8")
    (supplementary / "GSM1_barcodes.tsv.gz").write_bytes(gzip.compress(b"barcode\ncell1\n"))
    (supplementary / "GSM1_features.tsv.gz").write_bytes(gzip.compress(b"gene\tname\nENSG1\tA\n"))
    (supplementary / "GSM1_matrix.mtx.gz").write_bytes(gzip.compress(b"%%MatrixMarket matrix coordinate integer general\n1 1 1\n1 1 1\n"))
    output = tmp_path / "report.json"
    args = Namespace(download_root=tmp_path, gse="GSE123456", input_dir=None, require_manifest=True, skip_hash=False, output=str(output))
    assert inspector.analyze(args) == 0
    cli_result = json.loads(capsys.readouterr().out)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert cli_result["status"] == "success"
    assert cli_result["complete_10x_triplet_candidates"] == 1
    assert cli_result["partial_10x_candidates"] == 0
    assert report["manifest_found"] is True
    assert report["summary"]["files_on_disk"] == 3


def test_analyze_requires_manifest_when_requested(tmp_path: Path) -> None:
    supplementary = tmp_path / "GSE123456" / "supplementary"
    supplementary.mkdir(parents=True)
    args = Namespace(download_root=tmp_path, gse="GSE123456", input_dir=None, require_manifest=True, skip_hash=True, output=str(tmp_path / "report.json"))
    with pytest.raises(ValueError, match="Required manifest.json not found"):
        inspector.analyze(args)
