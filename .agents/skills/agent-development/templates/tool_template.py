"""Collect file records for a directory. Must not write outside the output path.

Template for new tools under `.agents/tools/`. Keep the shape: a typed pure
core, validation at the boundary, and a thin click `main()`. Replace the
example logic, options, and record dataclass with the real contract; delete
this guidance paragraph afterwards.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import click


@dataclass(frozen=True)
class FileRecord:
    """One output record; replace with the real schema."""

    name: str
    size_bytes: int


def collect_files(root: Path, *, max_items: int) -> list[FileRecord]:
    """Return records for regular files under root, capped at max_items.

    Pure read logic only: no click, no printing, no writes — unit-testable
    with a tmp_path fixture.
    """
    if not root.is_dir():
        raise click.ClickException(f"not a directory: {root}")
    if max_items < 1:
        raise click.ClickException(f"max_items must be >= 1, got {max_items}")

    records: list[FileRecord] = []
    for entry in sorted(root.iterdir()):
        if entry.is_file():
            records.append(FileRecord(name=entry.name, size_bytes=entry.stat().st_size))
        if len(records) >= max_items:
            break
    return records


def to_payload(records: list[FileRecord]) -> dict[str, Any]:
    """Wrap records in the stable JSON envelope consumed by skills."""
    return {"count": len(records), "items": [asdict(record) for record in records]}


@click.command()
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory to scan.",
)
@click.option(
    "--max-items",
    default=100,
    show_default=True,
    type=click.IntRange(min=1),
    help="Hard cap on returned records.",
)
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
def main(root: Path, max_items: int, pretty: bool) -> None:
    """Scan a directory and emit JSON file records on stdout."""
    payload = to_payload(collect_files(root, max_items=max_items))
    click.echo(json.dumps(payload, indent=2 if pretty else None, ensure_ascii=False))


if __name__ == "__main__":
    main()
