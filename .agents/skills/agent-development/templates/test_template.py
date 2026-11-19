"""Tests for <module>. Run: uv run pytest tests/test_<module>.py -q.

Template for new test files under `tests/`. Copy the real import line out of
a comment: project modules import as `tools.<package>.<module>` because
pyproject sets `pythonpath = [".agents"]`. Rules: no live network (mock at
the injected seam), filesystem via tmp_path, every bug fix starts as a
failing regression test here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

# from tools.<package>.<module> import collect_files, to_payload
# from tools.<package>.<module> import _search_geo_ids  # inject mocks at seams


def test_collect_files_returns_sorted_records(tmp_path: Path) -> None:
    """Happy path: only regular files, deterministic order, capped."""
    (tmp_path / "b.txt").write_bytes(b"12345")
    (tmp_path / "a.txt").write_bytes(b"1")
    (tmp_path / "sub").mkdir()

    # records = collect_files(tmp_path, max_items=10)
    # assert [r.name for r in records] == ["a.txt", "b.txt"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == [
        "a.txt",
        "b.txt",
    ]


def test_collect_files_rejects_missing_directory(tmp_path: Path) -> None:
    """Boundary: invalid input fails with an actionable ClickException."""
    import click

    with pytest.raises(click.ClickException):
        # collect_files(tmp_path / "nope", max_items=10)
        raise click.ClickException(f"not a directory: {tmp_path / 'nope'}")


def test_http_seam_is_injectable() -> None:
    """Pattern: replace the injected dependency instead of patching requests."""
    session = Mock()
    session.get.return_value.json.return_value = {"result": "stubbed"}
    assert session.get("https://example.invalid").json() == {"result": "stubbed"}
