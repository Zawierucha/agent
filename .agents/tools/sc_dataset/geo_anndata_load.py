#!/usr/bin/env python3
"""Try source adapters, enforce the canonical AnnData contract, and record outcomes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anndata as ad
import duckdb
import numpy as np
import pandas as pd
from scipy import sparse

from adapters import load_adapters
from adapters.base import AdapterContext, LoadCandidate

VERSION = "0.1.0"
DEFAULT_DB = Path("data/geo.duckdb")
DEFAULT_DOWNLOAD_ROOT = Path("data/downloads")
DEFAULT_OUTPUT_ROOT = Path("data/anndata")
DEFAULT_SCHEMA = Path(__file__).resolve().parents[3] / "data" / "anndata_schema.json"
TERMINAL_STATUSES = {"success", "not_eligible", "needs_manual_review"}


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def load_schema(path: Path) -> tuple[dict[str, Any], str]:
    schema = read_json(path, "canonical AnnData schema")
    contract = schema.get("anndata_contract")
    if schema.get("status") != "canonical" or not contract:
        raise ValueError("Schema file is not a canonical AnnData contract")
    if not {"X", "layers", "obs", "var", "uns"}.issubset(contract):
        raise ValueError("Canonical schema is missing required AnnData sections")
    return schema, hashlib.sha256(path.read_bytes()).hexdigest()


def validate_adata(adata: ad.AnnData, schema: dict[str, Any]) -> None:
    contract = schema["anndata_contract"]
    if not sparse.isspmatrix_csr(adata.X):
        raise ValueError("X must be a CSR sparse matrix")
    if adata.X.dtype != np.dtype("float32"):
        raise ValueError(f"X must be float32; received {adata.X.dtype}")
    if adata.X.shape != (adata.n_obs, adata.n_vars):
        raise ValueError("X dimensions do not agree with obs and var")

    obs_specs = contract["obs"]
    expected_obs = [name for name in obs_specs if name != "index"]
    if list(adata.obs.columns) != expected_obs:
        raise ValueError(f"obs columns must exactly match canonical order: {expected_obs}")
    obs_index_name = obs_specs["index"]["name"]
    if adata.obs.index.name != obs_index_name or not adata.obs.index.is_unique:
        raise ValueError(f"obs index must be unique and named {obs_index_name}")

    var_specs = contract["var"]
    expected_var = [name for name in var_specs if name != "index"]
    if list(adata.var.columns) != expected_var:
        raise ValueError(f"var columns must exactly match canonical order: {expected_var}")
    var_index_name = var_specs["index"]["name"]
    if adata.var.index.name != var_index_name or not adata.var.index.is_unique:
        raise ValueError(f"var index must be unique and named {var_index_name}")
    if not adata.var.index.map(lambda value: isinstance(value, str)).all():
        raise ValueError("var feature_id index values must be strings")

    dtype_names = {"string": "string", "Int64": "Int64", "Int32": "Int32", "Float32": "Float32"}
    for col, spec in obs_specs.items():
        if col == "index":
            continue
        actual = str(adata.obs[col].dtype)
        expected = dtype_names[spec["dtype"]]
        accepted = {expected, "float32"} if spec["dtype"] == "Float32" else {expected}
        if actual not in accepted:
            raise ValueError(f"obs.{col} dtype must be {expected}; got {adata.obs[col].dtype}")
    for col, spec in var_specs.items():
        if col != "index" and str(adata.var[col].dtype) != dtype_names[spec["dtype"]]:
            raise ValueError(f"var.{col} dtype must be {dtype_names[spec['dtype']]}; got {adata.var[col].dtype}")
    if "cell_type" in adata.obs and adata.obs["cell_type"].notna().any():
        raise ValueError("cell_type must remain null until the explicit annotation stage")

    if "counts" in adata.layers:
        if adata.layers["counts"].shape != adata.X.shape:
            raise ValueError("layers['counts'] must have the same shape and order as X")
        if not adata.uns.get("counts_confirmed", False):
            raise ValueError("layers['counts'] requires explicit confirmation in uns['counts_confirmed']")
    for key in contract["uns"]:
        if key not in adata.uns:
            raise ValueError(f"uns.{key} is required by the canonical contract")
    if adata.uns["schema_version"] != schema["schema_version"]:
        raise ValueError("AnnData schema_version does not match the canonical contract")
    if adata.uns["matrix_semantics"] not in {"raw counts", "normalized", "unknown"}:
        raise ValueError("uns.matrix_semantics must be raw counts, normalized, or unknown")


def catalog_snapshot(db_path: Path, gse: str) -> tuple[str | None, dict[str, dict[str, Any]], dict[str, str]]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        q = con.execute("SELECT qualification FROM geo_qualification WHERE gse_id=?", [gse]).fetchone()
        rows = con.execute("""
            SELECT s.gsm_id, s.organism, s.patient_id, s.timepoint, s.timepoint_value,
                   m.disease, m.tissue, m.cell_type,
                   q.qualification, q.disease_status, q.tissue_source
            FROM geo_samples s
            LEFT JOIN geo_sample_metadata m USING (gsm_id)
            LEFT JOIN geo_sample_qualification q USING (gsm_id)
            WHERE s.gse_id=? ORDER BY s.gsm_id
        """, [gse]).fetchall()
    finally:
        con.close()
    samples: dict[str, dict[str, Any]] = {}
    for row in rows:
        gsm = str(row[0]).upper()
        samples[gsm] = {
            "gsm_id": gsm, "organism": row[1], "patient_id": row[2],
            "timepoint": row[3], "timepoint_value": row[4], "disease": row[5],
            "tissue": row[6], "cell_type": row[7], "qualification": row[8],
            "disease_status": row[9], "tissue_source": row[10],
        }
    return (str(q[0]).lower() if q else None), samples, {gsm: row["qualification"] or "uncertain" for gsm, row in samples.items()}


def get_format_file_map(report: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for item in report.get("files", []):
        relative = item.get("relative_path") or item.get("name")
        if not relative:
            continue
        path = root / relative
        if path.is_file():
            out[str(path.resolve())] = {
                "relative": str(Path(relative)),
                "format_guess": item.get("format_guess", "unknown"),
                "sha256": item.get("sha256"),
            }
    return out


def ensure_import_tables(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        schema_path = Path(__file__).resolve().parents[3] / "data" / "schema.sql"
        con.execute(schema_path.read_text(encoding="utf-8"))
        names = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        required = {"geo_anndata_import_runs", "geo_anndata_import_files",
                    "geo_anndata_import_attempts", "geo_anndata_import_samples"}
        if not required.issubset(names):
            raise RuntimeError("Import history tables are missing; run geo_database.py init to apply data/schema.sql")
    finally:
        con.close()


def create_or_resume_run(db_path: Path, gse: str, schema: dict[str, Any], schema_hash: str,
                         output_dir: Path, run_id: str | None, max_iterations: int) -> tuple[str, int, int]:
    con = duckdb.connect(str(db_path))
    try:
        if run_id:
            row = con.execute("""
                SELECT gse_id,status,iteration_count,max_iterations,schema_id,schema_version,schema_sha256
                FROM geo_anndata_import_runs WHERE run_id=?
            """, [run_id]).fetchone()
            if row is None:
                raise ValueError(f"Unknown import run_id: {run_id}")
            if row[0] != gse:
                raise ValueError("run_id belongs to a different GSE")
            if row[1] in TERMINAL_STATUSES:
                raise ValueError(f"Import run is already terminal: {row[1]}")
            if (row[4], row[5], row[6]) != (schema["schema_id"], schema["schema_version"], schema_hash):
                raise ValueError("Canonical schema changed during this run; start a new import run")
            iteration = int(row[2]) + 1
            max_iterations = int(row[3])
            if iteration > max_iterations:
                raise ValueError(f"Maximum adapter iteration limit ({max_iterations}) has been reached")
            con.execute("UPDATE geo_anndata_import_runs SET status='running', iteration_count=? WHERE run_id=?", [iteration, run_id])
            con.commit()
            return run_id, iteration, max_iterations

        run_id = str(uuid.uuid4())
        iteration = 1
        con.execute("""
            INSERT INTO geo_anndata_import_runs
                (run_id,gse_id,schema_id,schema_version,schema_sha256,status,
                 iteration_count,max_iterations,output_directory)
            VALUES (?,?,?,?,?,'running',?,?,?)
        """, [run_id, gse, schema["schema_id"], schema["schema_version"], schema_hash,
              iteration, max_iterations, str(output_dir)])
        con.commit()
        return run_id, iteration, max_iterations
    finally:
        con.close()


def previous_successes(db_path: Path, run_id: str | None, schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not run_id:
        return {}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("""
            SELECT gsm_id,output_path,n_obs,n_vars,adapter_name
            FROM geo_anndata_import_samples WHERE run_id=? AND status='success'
        """, [run_id]).fetchall()
    finally:
        con.close()
    results = {}
    for gsm, output_path, n_obs, n_vars, adapter_name in rows:
        path = Path(output_path) if output_path else None
        if not path or not path.is_file():
            continue
        try:
            adata = ad.read_h5ad(path)
            validate_adata(adata, schema)
            results[gsm] = {"status": "success", "adapter_name": adapter_name,
                            "output_path": str(path), "n_obs": int(n_obs or adata.n_obs),
                            "n_vars": int(n_vars or adata.n_vars)}
        except Exception:
            continue
    return results


def _adapter_digest(adapter_path: str | None) -> str | None:
    if not adapter_path:
        return None
    path = Path(adapter_path)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_sample(adata: ad.AnnData, path: Path, schema: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.stem}.part.h5ad")
    try:
        validate_adata(adata, schema)
        # AnnData has no H5AD encoding for pandas FloatingArray. Store nullable
        # Float32 columns as NumPy float32 with NaN, the portable H5AD
        # representation of the same logical contract dtype.
        stored = adata.copy()
        for col, spec in schema["anndata_contract"]["obs"].items():
            if col != "index" and spec["dtype"] == "Float32":
                stored.obs[col] = stored.obs[col].array.to_numpy(dtype=np.float32, na_value=np.nan)
        adata = stored
        adata.write_h5ad(temp, convert_strings_to_categoricals=False)
        reopened = ad.read_h5ad(temp)
        validate_adata(reopened, schema)
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def persist_results(db_path: Path, run_id: str, iteration: int, max_iterations: int,
                    output_root: Path, files_meta: dict[str, dict[str, Any]],
                    adapters_used: list[dict[str, Any]], sample_results: dict[str, dict[str, Any]],
                    include_gsms: set[str], noneligible: dict[str, str], fatal_error: str | None = None) -> dict[str, Any]:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        successes = {gsm for gsm, result in sample_results.items() if result["status"] == "success"}
        failures = {gsm for gsm, result in sample_results.items() if result["status"] == "failed"}
        awaiting = include_gsms - successes - failures

        for gsm, qualification in noneligible.items():
            con.execute("""
                INSERT INTO geo_anndata_import_samples(run_id,gsm_id,status,last_error,updated_at)
                VALUES (?,?,'not_eligible',?,now())
                ON CONFLICT(run_id,gsm_id) DO UPDATE SET status='not_eligible',last_error=EXCLUDED.last_error,updated_at=now()
            """, [run_id, gsm, f"Sample qualification is {qualification}; only include GSMs are loaded."])

        for gsm in include_gsms:
            result = sample_results.get(gsm)
            if result is None:
                result = {"status": "needs_adapter", "error": "No registered adapter recognized a compatible source."}
            con.execute("""
                INSERT INTO geo_anndata_import_samples
                    (run_id,gsm_id,status,adapter_name,output_path,n_obs,n_vars,error_category,last_error,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,now())
                ON CONFLICT(run_id,gsm_id) DO UPDATE SET
                    status=EXCLUDED.status,adapter_name=EXCLUDED.adapter_name,output_path=EXCLUDED.output_path,
                    n_obs=EXCLUDED.n_obs,n_vars=EXCLUDED.n_vars,error_category=EXCLUDED.error_category,
                    last_error=EXCLUDED.last_error,updated_at=now()
            """, [run_id, gsm, result["status"], result.get("adapter_name"), result.get("output_path"),
                  result.get("n_obs"), result.get("n_vars"), result.get("error_category"), result.get("error")])

        for key, meta in files_meta.items():
            candidates = [item for item in adapters_used if item["source_path"] == key]
            statuses = [item["status"] for item in candidates]
            if not statuses:
                file_status = "needs_adapter"
            elif all(status == "success" for status in statuses):
                file_status = "success"
            elif any(status == "success" for status in statuses):
                file_status = "partial"
            else:
                file_status = "failed"
            last = next((item for item in reversed(candidates) if item.get("error")), {})
            adapter_name = candidates[-1]["adapter_name"] if candidates else None
            con.execute("""
                INSERT INTO geo_anndata_import_files
                    (run_id,source_relative_path,source_sha256,format_guess,adapter_name,status,attempt_count,error_category,last_error,updated_at)
                VALUES (?,?,?,?,?,?,?, ?,?,now())
                ON CONFLICT(run_id,source_relative_path) DO UPDATE SET
                    source_sha256=EXCLUDED.source_sha256,format_guess=EXCLUDED.format_guess,
                    adapter_name=EXCLUDED.adapter_name,status=EXCLUDED.status,
                    attempt_count=geo_anndata_import_files.attempt_count+EXCLUDED.attempt_count,
                    error_category=EXCLUDED.error_category,last_error=EXCLUDED.last_error,updated_at=now()
            """, [run_id, meta["relative"], meta.get("sha256"), meta.get("format_guess"), adapter_name,
                  file_status, len(candidates), last.get("error_category"), last.get("error")])

        for attempt in adapters_used:
            con.execute("""
                INSERT INTO geo_anndata_import_attempts
                    (run_id,iteration_number,source_relative_path,gsm_id,adapter_name,status,
                     error_category,error_message,adapter_path,started_at,completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,now())
                ON CONFLICT(run_id,iteration_number,source_relative_path,gsm_id,adapter_name) DO UPDATE SET
                    status=EXCLUDED.status,error_category=EXCLUDED.error_category,
                    error_message=EXCLUDED.error_message,adapter_path=EXCLUDED.adapter_path,completed_at=now()
            """, [run_id, iteration, attempt["source_relative_path"], attempt.get("gsm_id", ""), attempt["adapter_name"],
                  attempt["status"], attempt.get("error_category"), attempt.get("error"),
                  attempt.get("adapter_path"), attempt["started_at"]])

        loaded = len(successes)
        pending = len(awaiting)
        unresolved = len(include_gsms - successes)
        if fatal_error:
            status = "failed"
        elif not include_gsms:
            status = "not_eligible"
        elif include_gsms and loaded == len(include_gsms):
            status = "success"
        elif iteration >= max_iterations:
            status = "needs_manual_review"
        elif loaded:
            status = "partial"
        else:
            status = "needs_adapter"
        summary = {
            "eligible_samples": len(include_gsms),
            "loaded_samples": loaded,
            "failed_samples": len(failures),
            "awaiting_adapter_samples": pending,
            "unresolved_samples": unresolved,
            "not_eligible_samples": len(noneligible),
            "adapter_attempts_this_iteration": len(adapters_used),
        }
        con.execute("""
            UPDATE geo_anndata_import_runs
            SET status=?,summary_json=?,last_error=?,completed_at=?
            WHERE run_id=?
        """, [status, json.dumps(summary), fatal_error,
              datetime.now(timezone.utc).replace(tzinfo=None) if status in TERMINAL_STATUSES | {"failed"} else None,
              run_id])
        con.execute("COMMIT")
        return {"run_id": run_id, "iteration": iteration, "status": status, **summary,
                "output_directory": str(output_root)}
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    gse = args.gse.upper()
    if not re_full_gse(gse):
        raise ValueError("--gse must be a GEO Series accession such as GSE346721")
    dataset_dir = args.download_root / gse
    supplementary = dataset_dir / "supplementary"
    manifest = read_json(dataset_dir / "manifest.json", "download manifest")
    format_report = read_json(dataset_dir / "format_report.json", "format report")
    if str(manifest.get("gse_id", "")).upper() != gse or str(format_report.get("gse_id", "")).upper() != gse:
        raise ValueError("Manifest or format report GSE does not match request")
    schema, schema_hash = load_schema(args.schema)
    ensure_import_tables(args.db)
    dataset_decision, sample_metadata, sample_decisions = catalog_snapshot(args.db, gse)
    if dataset_decision != "include":
        raise ValueError(f"Dataset qualification is {dataset_decision or 'missing'}; loading requires include")
    include_gsms = {gsm for gsm, decision in sample_decisions.items() if decision == "include"}
    noneligible = {gsm: decision for gsm, decision in sample_decisions.items() if decision != "include"}
    if not sample_metadata:
        raise ValueError(f"No GEO GSM samples are registered for {gse}")

    adapters, import_errors = load_adapters()
    if include_gsms and args.adapter:
        adapters = [adapter for adapter in adapters if adapter.name == args.adapter]
        if not adapters:
            raise ValueError(f"Requested adapter is not registered: {args.adapter}")

    output_dir = args.output_root / gse
    run_id, iteration, max_iterations = create_or_resume_run(
        args.db, gse, schema, schema_hash, output_dir, args.run_id, args.max_iterations,
    )
    if not include_gsms:
        result = persist_results(args.db, run_id, iteration, max_iterations, output_dir, {}, [], {}, set(), noneligible)
        result.update({"schema_id": schema["schema_id"], "schema_version": schema["schema_version"],
                       "schema_sha256": schema_hash, "sample_results": {},
                       "sample_qualifications": noneligible})
        return write_report(result, output_dir)

    files_meta = get_format_file_map(format_report, supplementary)
    context = AdapterContext(gse, supplementary, include_gsms, sample_metadata, manifest, format_report, schema, schema_hash)

    attempts: list[dict[str, Any]] = []
    candidates_by_sample: dict[str, list[tuple[Any, LoadCandidate]]] = {}
    discovery_errors = []
    import inspect
    adapter_paths: dict[str, str] = {}
    for adapter in adapters:
        try:
            adapter_paths[adapter.name] = inspect.getsourcefile(type(adapter)) or ""
        except TypeError:
            adapter_paths[adapter.name] = ""
    for adapter in adapters:
        try:
            found = list(adapter.discover(context))
        except Exception as exc:
            discovery_errors.append({"adapter": adapter.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for candidate in found:
            if candidate.gsm_id not in include_gsms:
                continue
            candidates_by_sample.setdefault(candidate.gsm_id, []).append((adapter, candidate))
            if candidate.source_path.exists():
                path_key = str(candidate.source_path.resolve())
                if path_key not in files_meta:
                    rel = str(candidate.source_path.relative_to(supplementary))
                    files_meta[path_key] = {"relative": rel, "format_guess": candidate.format_guess, "sha256": None}

    sample_results: dict[str, dict[str, Any]] = previous_successes(args.db, args.run_id, schema)
    adapter_source_status: dict[str, list[dict[str, Any]]] = {}
    for gsm in sorted(include_gsms):
        if sample_results.get(gsm, {}).get("status") == "success":
            continue
        choices = candidates_by_sample.get(gsm, [])
        if not choices:
            sample_results[gsm] = {"status": "needs_adapter", "error_category": "no_matching_adapter",
                                   "error": "No registered adapter recognized an eligible source file for this GSM."}
            continue
        by_adapter: dict[str, list[tuple[Any, LoadCandidate]]] = {}
        for adapter, candidate in choices:
            by_adapter.setdefault(adapter.name, []).append((adapter, candidate))
        succeeded = False
        for adapter_name, adapter_candidates in by_adapter.items():
            adapter = adapter_candidates[0][0]
            if len(adapter_candidates) > 1:
                for _, candidate in adapter_candidates:
                    attempts.append({"source_path": str(candidate.source_path.resolve()),
                        "source_relative_path": candidate.source_relative_path, "gsm_id": gsm, "adapter_name": adapter_name,
                        "status": "ambiguous_candidates", "error_category": "ambiguous_mapping",
                        "error": f"Adapter found {len(adapter_candidates)} candidates for {gsm}; refusing to choose.",
                        "adapter_path": adapter_paths.get(adapter_name), "started_at": datetime.now(timezone.utc).replace(tzinfo=None)})
                continue
            candidate = adapter_candidates[0][1]
            started = datetime.now(timezone.utc).replace(tzinfo=None)
            try:
                adata = adapter.load(context, candidate)
                validate_adata(adata, schema)
                output_path = output_dir / f"{gsm}.h5ad"
                _write_sample(adata, output_path, schema)
                sample_results[gsm] = {"status": "success", "adapter_name": adapter_name,
                    "output_path": str(output_path), "n_obs": adata.n_obs, "n_vars": adata.n_vars}
                attempts.append({"source_path": str(candidate.source_path.resolve()),
                    "source_relative_path": candidate.source_relative_path, "gsm_id": gsm, "adapter_name": adapter_name,
                    "status": "success", "adapter_path": adapter_paths.get(adapter_name), "started_at": started})
                adapter_source_status.setdefault(str(candidate.source_path.resolve()), []).append({"status": "success", "adapter_name": adapter_name})
                succeeded = True
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                sample_results[gsm] = {"status": "failed", "adapter_name": adapter_name,
                    "error_category": "read_or_validation_error", "error": error}
                attempts.append({"source_path": str(candidate.source_path.resolve()),
                    "source_relative_path": candidate.source_relative_path, "gsm_id": gsm, "adapter_name": adapter_name,
                    "status": "failed", "error_category": "read_or_validation_error", "error": error,
                    "adapter_path": adapter_paths.get(adapter_name), "started_at": started})
                adapter_source_status.setdefault(str(candidate.source_path.resolve()), []).append({"status": "failed", "adapter_name": adapter_name, "error": error, "error_category": "read_or_validation_error"})
        if succeeded:
            continue
        if gsm not in sample_results:
            sample_results[gsm] = {"status": "needs_adapter", "error_category": "no_compatible_adapter",
                                   "error": "Available adapters did not produce a valid canonical AnnData object."}

    for path, meta in files_meta.items():
        if path not in adapter_source_status:
            adapter_source_status[path] = []
    result = persist_results(args.db, run_id, iteration, max_iterations, output_dir, files_meta,
                             attempts, sample_results, include_gsms, noneligible)
    result["schema_id"] = schema["schema_id"]
    result["schema_version"] = schema["schema_version"]
    result["schema_sha256"] = schema_hash
    result["adapter_discovery_errors"] = import_errors + discovery_errors
    result["attempted_adapters"] = [adapter.name for adapter in adapters]
    result["sample_results"] = sample_results
    result["attempts"] = [
        {**attempt, "started_at": attempt["started_at"].isoformat()}
        for attempt in attempts
    ]
    result["sample_qualifications"] = {gsm: decision for gsm, decision in noneligible.items()}
    return write_report(result, output_dir)


def write_report(result: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    result["report_path"] = str(output_dir / "anndata_import_report.json")
    result_path = Path(result["report_path"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def re_full_gse(value: str) -> bool:
    import re
    return re.fullmatch(r"GSE\d+", value) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="Load qualified GEO samples into canonical AnnData using registered adapters")
    parser.add_argument("--gse", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--download-root", type=Path, default=DEFAULT_DOWNLOAD_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--run-id", help="Continue an unfinished import run")
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--adapter", help="Restrict this iteration to a specific registered adapter")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        if not 1 <= args.max_iterations <= 10:
            raise ValueError("--max-iterations must be between 1 and 10")
        result = run(args)
        print(json.dumps({"tool": "geo_anndata_load", "version": VERSION, **result},
                         ensure_ascii=False, indent=2 if args.pretty else None))
        return 0 if result["status"] == "success" else 1
    except (OSError, ValueError, RuntimeError, duckdb.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"tool": "geo_anndata_load", "version": VERSION,
                          "status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
