from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock

import duckdb
import pytest
import requests

from tools.sc_dataset import geo_download as downloader


class FakeResponse:
    def __init__(self, body: bytes = b"abc", status_code: int = 200, headers: dict | None = None, error: Exception | None = None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {"Content-Length": str(len(body))}
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.error:
            raise self.error
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size: int):
        yield self.body[:1]
        yield b""
        yield self.body[1:]


def create_catalog(path: Path, qualification: str = "include") -> None:
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE geo_datasets (gse_id VARCHAR, title VARCHAR)")
    connection.execute("CREATE TABLE geo_qualification (gse_id VARCHAR, qualification VARCHAR)")
    connection.execute("CREATE TABLE geo_samples (gsm_id VARCHAR, gse_id VARCHAR)")
    connection.execute("INSERT INTO geo_datasets VALUES ('GSE123456', 'Example')")
    connection.execute("INSERT INTO geo_qualification VALUES ('GSE123456', ?)", [qualification])
    connection.execute("INSERT INTO geo_samples VALUES ('GSM123456', 'GSE123456')")
    connection.close()


def test_supplementary_url_builds_geo_bucket() -> None:
    assert downloader.supplementary_url("GSE346721") == "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE346nnn/GSE346721/suppl/"
    assert downloader.supplementary_url("gse123") == "https://ftp.ncbi.nlm.nih.gov/geo/series/GSEnnn/GSE123/suppl/"


@pytest.mark.parametrize("gse_id", ["bad", "GSM123456", "GSE12x"])
def test_supplementary_url_rejects_invalid_accession(gse_id: str) -> None:
    with pytest.raises(ValueError):
        downloader.supplementary_url(gse_id)


def test_safe_filename_rejects_path_components() -> None:
    for name in ("../file.txt", "dir/file.txt", r"dir\file.txt", "..", ""):
        with pytest.raises(ValueError):
            downloader.safe_filename(name)
    assert downloader.safe_filename("matrix.mtx.gz") == "matrix.mtx.gz"


def test_eligible_gses_only_returns_included_datasets(tmp_path: Path) -> None:
    db = tmp_path / "catalog.duckdb"
    create_catalog(db, "include")
    connection = duckdb.connect(str(db))
    assert downloader.eligible_gses(connection) == ["GSE123456"]
    connection.execute("UPDATE geo_qualification SET qualification='exclude'")
    assert downloader.eligible_gses(connection) == []
    connection.close()


def test_eligible_gses_reports_missing_tables() -> None:
    connection = duckdb.connect(":memory:")
    with pytest.raises(RuntimeError, match="Required catalog tables are missing"):
        downloader.eligible_gses(connection)
    connection.close()


def test_catalog_snapshot_requires_include_qualification(tmp_path: Path) -> None:
    db = tmp_path / "catalog.duckdb"
    create_catalog(db, "exclude")
    with pytest.raises(ValueError, match="require include"):
        downloader.catalog_snapshot(db, "GSE123456")


def test_catalog_snapshot_returns_dataset_and_samples(tmp_path: Path) -> None:
    db = tmp_path / "catalog.duckdb"
    create_catalog(db)
    snapshot = downloader.catalog_snapshot(db, "GSE123456")
    assert snapshot["dataset"]["gse_id"] == "GSE123456"
    assert snapshot["gsm_ids"] == ["GSM123456"]


def test_list_files_parses_listing_and_ignores_external_links(monkeypatch: pytest.MonkeyPatch) -> None:
    html = '''<table><tr><td><a href="matrix.mtx.gz">matrix.mtx.gz</a></td><td>123</td></tr><tr><td><a href="../">Parent</a></td></tr><tr><td><a href="https://example.com/evil.txt">evil</a></td></tr></table>'''
    response = Mock()
    response.status_code = 200
    response.text = html
    response.raise_for_status.return_value = None
    monkeypatch.setattr(downloader.requests, "get", Mock(return_value=response))
    url, files = downloader.list_files("GSE123456", 10)
    assert url.endswith("/GSE123456/suppl/")
    assert len(files) == 1
    assert files[0]["filename"] == "matrix.mtx.gz"
    assert files[0]["listed_size_bytes"] == 123


def test_list_files_returns_empty_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.status_code = 404
    monkeypatch.setattr(downloader.requests, "get", Mock(return_value=response))
    url, files = downloader.list_files("GSE123456", 10)
    assert url.endswith("/GSE123456/suppl/")
    assert files == []


def test_download_one_writes_file_and_hashes_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"matrix-data"
    monkeypatch.setattr(downloader.requests, "get", Mock(return_value=FakeResponse(body)))
    result = downloader.download_one({"filename": "matrix.mtx.gz", "source_url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456/suppl/matrix.mtx.gz"}, tmp_path, 10, 0)
    target = tmp_path / "matrix.mtx.gz"
    assert target.read_bytes() == body
    assert result["download_status"] == "downloaded"
    assert result["size_bytes"] == len(body)
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["attempts"] == 1


def test_download_one_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [FakeResponse(status_code=503), FakeResponse(b"ok")]
    get = Mock(side_effect=responses)
    monkeypatch.setattr(downloader.requests, "get", get)
    monkeypatch.setattr(downloader.time, "sleep", Mock())
    result = downloader.download_one({"filename": "x.txt", "source_url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456/suppl/x.txt"}, tmp_path, 10, 1)
    assert result["download_status"] == "downloaded"
    assert result["attempts"] == 2
    assert (tmp_path / "x.txt").read_bytes() == b"ok"


def test_download_one_removes_partial_file_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader.requests, "get", Mock(return_value=FakeResponse(b"short", headers={"Content-Length": "20"})))
    result = downloader.download_one({"filename": "x.txt", "source_url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456/suppl/x.txt"}, tmp_path, 10, 0)
    assert result["download_status"] == "failed"
    assert not (tmp_path / "x.txt").exists()
    assert not (tmp_path / ".x.txt.part").exists()


def test_run_gse_dry_run_writes_no_manifest_or_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "catalog.duckdb"
    create_catalog(db)
    monkeypatch.setattr(downloader, "list_files", lambda gse, timeout: ("https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456/suppl/", [{"filename": "x.txt", "source_url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456/suppl/x.txt", "listed_size_bytes": 3}]))
    result = downloader.run_gse(db, tmp_path / "downloads", "GSE123456", 10, 0, False)
    assert result["dry_run"] is True
    assert result["source_file_count"] == 1
    assert result["files"][0]["download_status"] == "not_downloaded"
    assert not (tmp_path / "downloads" / "GSE123456").exists()
