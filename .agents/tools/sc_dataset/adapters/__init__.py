"""Adapter registry. Additional trusted project adapters export ``ADAPTER``."""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .tenx_mtx import TenxMatrixMarketAdapter


def load_adapters(adapter_dir: Path | None = None) -> tuple[list[Any], list[dict[str, str]]]:
    adapters: list[Any] = [TenxMatrixMarketAdapter()]
    errors: list[dict[str, str]] = []
    root = adapter_dir or Path(__file__).resolve().parent
    for path in sorted(root.glob("*.py")):
        if path.name in {"__init__.py", "base.py", "tenx_mtx.py"}:
            continue
        module_name = f"{__name__}.{path.stem}"
        try:
            module = importlib.import_module(module_name)
            adapter = getattr(module, "ADAPTER", None)
            if adapter is None or not hasattr(adapter, "discover") or not hasattr(adapter, "load"):
                errors.append({"path": str(path), "error": "Module must export ADAPTER with discover() and load() methods"})
                continue
            adapters.append(adapter)
        except Exception as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return adapters, errors
