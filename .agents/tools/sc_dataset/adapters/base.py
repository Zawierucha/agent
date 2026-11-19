"""Interfaces shared by source-specific AnnData adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol


@dataclass
class LoadCandidate:
    gsm_id: str
    source_path: Path
    source_relative_path: str
    format_guess: str
    components: dict[str, str]
    archive_member_map: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class AdapterContext:
    gse_id: str
    supplementary_dir: Path
    include_gsms: set[str]
    sample_metadata: dict[str, dict[str, Any]]
    manifest: dict[str, Any]
    format_report: dict[str, Any]
    canonical_schema: dict[str, Any]
    schema_sha256: str


class Adapter(Protocol):
    name: str

    def discover(self, context: AdapterContext) -> Iterable[LoadCandidate]: ...

    def load(self, context: AdapterContext, candidate: LoadCandidate) -> Any: ...
