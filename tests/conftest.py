from __future__ import annotations

import hashlib
from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "data" / "schema.sql"


def make_geo_db(db_path: Path) -> Path:
    """Create a GEO database using the production schema."""
    connection = duckdb.connect(str(db_path))
    try:
        connection.execute(SCHEMA_PATH.read_text())
    finally:
        connection.close()
    return db_path


@pytest.fixture
def geo_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a schema-driven test database and isolate relative paths."""
    monkeypatch.chdir(tmp_path)
    return make_geo_db(tmp_path / "geo.duckdb")


def _database_state(path: Path) -> tuple[int, int, str] | None:
    """Return size, mtime, and MD5, or None when the database is absent."""
    if not path.exists():
        return None
    stat = path.stat()
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    return stat.st_size, stat.st_mtime_ns, digest


def _assert_database_unchanged(
    path: Path,
    original_state: tuple[int, int, str] | None,
) -> None:
    """Reject production database modification or unexpected appearance."""
    if _database_state(path) != original_state:
        raise AssertionError(
            f"Production database changed or appeared during tests: {path}"
        )


@pytest.fixture(scope="session", autouse=True)
def production_db_untouched() -> Generator[None]:
    """Fail the test session if the production database changes."""
    production_db = REPO_ROOT / "data" / "geo.duckdb"
    original_state = _database_state(production_db)
    yield
    try:
        _assert_database_unchanged(production_db, original_state)
    except AssertionError as error:
        pytest.fail(str(error), pytrace=False)
