from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import click
import duckdb

_TOOL_IMPORT_ROOT = Path(__file__).resolve().parents[2]
if not any((_candidate / "tools").is_dir() for _candidate in map(Path, sys.path)):
    sys.path.insert(0, str(_TOOL_IMPORT_ROOT))

DEFAULT_DB = Path("data/geo.duckdb")
STATUSES = {"include", "exclude", "uncertain"}
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
REVIEW_SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS geo_review_runs (
  review_run_id VARCHAR PRIMARY KEY, source_run_id VARCHAR, reviewer VARCHAR,
  reviewer_skill_sha256 VARCHAR, started_at_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notes VARCHAR);
CREATE TABLE IF NOT EXISTS geo_review_decisions (
  review_run_id VARCHAR NOT NULL REFERENCES geo_review_runs(review_run_id),
  level VARCHAR NOT NULL CHECK (level IN ('gse','gsm')),
  gse_id VARCHAR NOT NULL REFERENCES geo_datasets(gse_id),
  gsm_id VARCHAR,
  accession VARCHAR NOT NULL,
  original_status VARCHAR NOT NULL, reviewed_status VARCHAR NOT NULL CHECK (reviewed_status IN ('include','exclude','uncertain')),
  changed BOOLEAN NOT NULL, reason VARCHAR NOT NULL, sources VARCHAR NOT NULL,
  evidence_quality VARCHAR, missing_info VARCHAR,
  created_at_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (review_run_id, level, accession));
"""


class ReviewRefusal(Exception):
    def __init__(self, error_class: str, detail: str | None = None):
        self.error_class = error_class
        self.detail = detail
        super().__init__(error_class)


def _emit(value: object) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, default=str))


def _refuse(error_class: str, detail: str | None = None) -> None:
    payload = {"error_class": error_class}
    if detail:
        payload["detail"] = detail
    _emit(payload)
    raise click.exceptions.Exit(1)


def _connect(db: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db))


def ensure_schema(db: duckdb.DuckDBPyConnection) -> None:
    """Create review tables on an existing GEO database, without changing base DDL."""
    present = {row[0] for row in db.execute("SHOW TABLES").fetchall()}
    if {"geo_review_runs", "geo_review_decisions"}.issubset(present):
        return
    statements = REVIEW_SCHEMA_SQL.split(";\n")
    for statement, table in zip(statements, ("geo_review_runs", "geo_review_decisions")):
        if table not in present:
            db.execute(statement)


def _ensure_existing_schema(db_path: Path) -> duckdb.DuckDBPyConnection:
    if not db_path.is_file():
        raise ReviewRefusal("db_missing", str(db_path))
    connection = _connect(db_path)
    try:
        ensure_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _count(connection: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewRefusal("input_invalid", str(exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        raise ReviewRefusal("input_invalid", "Input must be an object with a decisions array")
    return payload


def _db_original(connection: duckdb.DuckDBPyConnection, level: str, accession: str) -> str | None:
    table, key = ("geo_qualification", "gse_id") if level == "gse" else ("geo_sample_qualification", "gsm_id")
    row = connection.execute(f"SELECT qualification FROM {table} WHERE {key}=?", [accession]).fetchone()
    return row[0] if row else None


def _validation_errors(connection: duckdb.DuckDBPyConnection, decisions: list) -> tuple[list[dict], list[dict]]:
    errors: list[dict] = []
    normalized: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def issue(error_class: str, index: int, accession: str | None = None, detail: str | None = None) -> None:
        row = {"error_class": error_class, "index": index}
        if accession:
            row["accession"] = accession
        if detail:
            row["detail"] = detail
        errors.append(row)

    for index, entry in enumerate(decisions):
        if not isinstance(entry, dict):
            issue("decision_invalid", index, detail="Decision must be an object")
            continue
        level = entry.get("level")
        if level not in {"gse", "gsm"}:
            issue("invalid_level", index)
            continue
        gse_id = entry.get("gse_id")
        gsm_id = entry.get("gsm_id")
        accession = gse_id if level == "gse" else gsm_id
        if not isinstance(accession, str) or not accession.strip():
            issue("accession_missing", index)
            continue
        key = (level, accession)
        if key in seen:
            issue("duplicate_accession", index, accession)
        seen.add(key)
        if not isinstance(gse_id, str) or not gse_id:
            issue("gse_id_missing", index, accession)
        else:
            exists = connection.execute("SELECT 1 FROM geo_datasets WHERE gse_id=?", [gse_id]).fetchone()
            if not exists:
                issue("unknown_accession", index, accession, "GSE is not registered")
        if level == "gsm":
            sample = connection.execute("SELECT gse_id FROM geo_samples WHERE gsm_id=?", [gsm_id]).fetchone()
            if sample is None:
                issue("unknown_accession", index, accession, "GSM is not registered")
            elif sample[0] != gse_id:
                issue("owner_mismatch", index, accession, f"GSM belongs to {sample[0]}")
        reviewed = entry.get("reviewed_status")
        if reviewed not in STATUSES:
            issue("invalid_status", index, accession)
        original = _db_original(connection, level, accession)
        if original is None:
            issue("qualification_missing", index, accession)
        elif "original_status" in entry and entry["original_status"] != original:
            issue("original_mismatch", index, accession, f"DB has {original}")
        changed = original != reviewed
        reason = entry.get("reason")
        sources = entry.get("sources")
        if changed and (not isinstance(reason, str) or not reason.strip()):
            issue("reason_required", index, accession)
        if changed and (not isinstance(sources, list) or not sources or not any(str(item).strip() for item in sources)):
            issue("sources_required", index, accession)
        if reviewed == "uncertain" and not (isinstance(entry.get("missing_info"), str) and entry["missing_info"].strip()):
            issue("missing_info_required", index, accession)
        normalized.append({
            "level": level,
            "gse_id": gse_id,
            "gsm_id": gsm_id if level == "gsm" else None,
            "accession": accession,
            "original_status": original,
            "reviewed_status": reviewed,
            "changed": bool(changed),
            "reason": reason if isinstance(reason, str) else "",
            "sources": json.dumps(sources, ensure_ascii=False, sort_keys=True) if isinstance(sources, list) else "[]",
            "evidence_quality": entry.get("evidence_quality"),
            "missing_info": entry.get("missing_info"),
        })
    return errors, normalized


def _canonical_decisions(rows: list[dict]) -> str:
    return json.dumps(sorted(rows, key=lambda row: (row["level"], row["accession"])), ensure_ascii=False, sort_keys=True)


@click.group()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
@click.pass_context
def cli(ctx: click.Context, db: Path) -> None:
    ctx.ensure_object(dict)
    ctx.obj["db"] = db


@cli.command("ensure-schema")
@click.pass_context
def ensure_schema_command(ctx: click.Context) -> None:
    connection = None
    try:
        connection = _ensure_existing_schema(ctx.obj["db"])
        _emit({"schema": "geo_review", "ensured": True})
    except ReviewRefusal as exc:
        _refuse(exc.error_class, exc.detail)
    except Exception as exc:  # noqa: BLE001 - convert all CLI/runtime failures to JSON
        _refuse("schema_error", str(exc))
    finally:
        if connection is not None:
            connection.close()


@cli.command("backup")
@click.option("--to", "target", type=click.Path(path_type=Path), default=None)
@click.pass_context
def backup_command(ctx: click.Context, target: Path | None) -> None:
    source: Path = ctx.obj["db"]
    if not source.is_file():
        _refuse("db_missing", str(source))
    if target is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = Path("data/backups") / f"geo_pre_review_{stamp}.duckdb"
    if target.exists():
        _refuse("backup_exists", str(target))
    try:
        probe = _connect(source)
        probe.execute("BEGIN TRANSACTION")
        probe.execute("ROLLBACK")
        probe.close()
    except Exception as exc:  # noqa: BLE001 - convert all CLI/runtime failures to JSON
        _refuse("db_locked", str(exc))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb"):
            pass
        shutil.copy2(source, target)
        wal = Path(f"{source}.wal")
        if wal.is_file() and wal.stat().st_size:
            shutil.copy2(wal, Path(f"{target}.wal"))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        _emit({"path": str(target), "size": target.stat().st_size, "sha256": digest})
    except FileExistsError:
        _refuse("backup_exists", str(target))
    except Exception as exc:  # noqa: BLE001 - convert all CLI/runtime failures to JSON
        _refuse("backup_failed", str(exc))


@cli.command("save-review")
@click.option("--review-run-id", required=True)
@click.option("--input", "input_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--source-run-id")
@click.option("--reviewer", default="sc-dataset-qualification-review/0.1.0", show_default=True)
@click.option("--skill-md", type=click.Path(path_type=Path, exists=True))
@click.option("--dry-run", is_flag=True)
@click.pass_context
def save_review_command(ctx: click.Context, review_run_id: str, input_path: Path, source_run_id: str | None,
                       reviewer: str, skill_md: Path | None, dry_run: bool) -> None:
    if not RUN_ID_RE.fullmatch(review_run_id):
        _refuse("bad_run_id", review_run_id)
    try:
        payload = _load_json(input_path)
    except ReviewRefusal as exc:
        _refuse(exc.error_class, exc.detail)
    connection = None
    try:
        connection = _ensure_existing_schema(ctx.obj["db"])
        connection.execute("BEGIN TRANSACTION")
        errors, rows = _validation_errors(connection, payload["decisions"])
        if errors:
            connection.execute("ROLLBACK")
            _emit({"errors": errors})
            raise click.exceptions.Exit(1)
        counts = {
            "geo_qualification": _count(connection, "geo_qualification"),
            "geo_sample_qualification": _count(connection, "geo_sample_qualification"),
        }
        notes = json.dumps({"snapshot_counts": counts}, ensure_ascii=False, sort_keys=True)
        skill_hash = hashlib.sha256(skill_md.read_bytes()).hexdigest() if skill_md else None
        existing = connection.execute(
            "SELECT source_run_id, reviewer, reviewer_skill_sha256, notes FROM geo_review_runs WHERE review_run_id=?",
            [review_run_id],
        ).fetchone()
        old_rows = connection.execute(
            "SELECT level,gse_id,gsm_id,accession,original_status,reviewed_status,changed,reason,sources,evidence_quality,missing_info "
            "FROM geo_review_decisions WHERE review_run_id=?", [review_run_id]
        ).fetchall()
        old_payload = [{
            "level": row[0], "gse_id": row[1], "gsm_id": row[2], "accession": row[3],
            "original_status": row[4], "reviewed_status": row[5], "changed": row[6],
            "reason": row[7], "sources": row[8], "evidence_quality": row[9], "missing_info": row[10],
        } for row in old_rows]
        if existing and (
            existing != (source_run_id, reviewer, skill_hash, notes)
            or _canonical_decisions(old_payload) != _canonical_decisions(rows)
        ):
            connection.execute("ROLLBACK")
            _refuse("review_run_conflict", review_run_id)
        if dry_run:
            _emit({"dry_run": True, "would_insert": len(rows), "would_change": sum(row["changed"] for row in rows), "errors": []})
            connection.execute("ROLLBACK")
            return
        if existing:
            connection.execute("ROLLBACK")
            _emit({"review_run_id": review_run_id, "inserted": len(rows), "changed": sum(row["changed"] for row in rows)})
            return
        connection.execute(
            "INSERT INTO geo_review_runs (review_run_id,source_run_id,reviewer,reviewer_skill_sha256,notes) VALUES (?,?,?,?,?)",
            [review_run_id, source_run_id, reviewer, skill_hash, notes],
        )
        if rows:
            connection.executemany(
                "INSERT INTO geo_review_decisions (review_run_id,level,gse_id,gsm_id,accession,original_status,reviewed_status,changed,reason,sources,evidence_quality,missing_info) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [[review_run_id, row["level"], row["gse_id"], row["gsm_id"], row["accession"], row["original_status"],
                  row["reviewed_status"], row["changed"], row["reason"], row["sources"], row["evidence_quality"], row["missing_info"]]
                 for row in rows],
            )
        connection.execute("COMMIT")
        _emit({"review_run_id": review_run_id, "inserted": len(rows), "changed": sum(row["changed"] for row in rows)})
    except ReviewRefusal as exc:
        if connection is not None:
            with suppress(duckdb.Error):
                connection.execute("ROLLBACK")
        _refuse(exc.error_class, exc.detail)
    except click.exceptions.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - convert all CLI/runtime failures to JSON
        if connection is not None:
            with suppress(duckdb.Error):
                connection.execute("ROLLBACK")
        _refuse("review_write_failed", str(exc))
    finally:
        if connection is not None:
            connection.close()


def _decision_rows(connection: duckdb.DuckDBPyConnection, review_run_id: str | None = None,
                   gse: str | None = None, level: str | None = None, latest: bool = False) -> list[dict]:
    where, params = [], []
    if review_run_id:
        where.append("review_run_id=?")
        params.append(review_run_id)
    if gse:
        where.append("gse_id=?")
        params.append(gse)
    if level:
        where.append("level=?")
        params.append(level)
    predicate = " WHERE " + " AND ".join(where) if where else ""
    sql = "SELECT review_run_id,level,gse_id,gsm_id,accession,original_status,reviewed_status,changed,reason,sources,evidence_quality,missing_info,created_at_utc FROM geo_review_decisions" + predicate
    if latest and not review_run_id:
        sql = "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY level,accession ORDER BY created_at_utc DESC,review_run_id DESC) AS _rn FROM (" + sql + ") q) latest_rows WHERE _rn=1"
    else:
        sql += " ORDER BY created_at_utc DESC, review_run_id DESC, level, accession"
    raw = connection.execute(sql, params).fetchall()
    names = ["review_run_id", "level", "gse_id", "gsm_id", "accession", "original_status", "reviewed_status", "changed", "reason", "sources", "evidence_quality", "missing_info", "created_at_utc"]
    return [dict(zip(names, row[:13])) for row in raw]


@cli.command("get-reviews")
@click.option("--review-run-id")
@click.option("--gse")
@click.option("--level", type=click.Choice(["gse", "gsm"]))
@click.pass_context
def get_reviews_command(ctx: click.Context, review_run_id: str | None, gse: str | None, level: str | None) -> None:
    connection = None
    try:
        connection = _ensure_existing_schema(ctx.obj["db"])
        _emit(_decision_rows(connection, review_run_id, gse, level, latest=True))
    except ReviewRefusal as exc:
        _refuse(exc.error_class, exc.detail)
    except Exception as exc:  # noqa: BLE001 - convert all CLI/runtime failures to JSON
        _refuse("review_read_failed", str(exc))
    finally:
        if connection is not None:
            connection.close()


@cli.command("report")
@click.option("--review-run-id", required=True)
@click.pass_context
def report_command(ctx: click.Context, review_run_id: str) -> None:
    connection = None
    try:
        connection = _ensure_existing_schema(ctx.obj["db"])
        run = connection.execute("SELECT notes FROM geo_review_runs WHERE review_run_id=?", [review_run_id]).fetchone()
        if run is None:
            _refuse("review_run_missing", review_run_id)
        rows = _decision_rows(connection, review_run_id)
        statuses = [row["reviewed_status"] for row in rows]
        snapshot = json.loads(run[0]).get("snapshot_counts", {}) if run[0] else {}
        current = {
            "geo_qualification": _count(connection, "geo_qualification"),
            "geo_sample_qualification": _count(connection, "geo_sample_qualification"),
        }
        _emit({
            "gse_reviewed": sum(row["level"] == "gse" for row in rows),
            "gsm_reviewed": sum(row["level"] == "gsm" for row in rows),
            "to_include": statuses.count("include"),
            "to_exclude": statuses.count("exclude"),
            "to_uncertain": statuses.count("uncertain"),
            "kept": sum(not row["changed"] for row in rows),
            "original_tables_intact": current == snapshot,
        })
    except ReviewRefusal as exc:
        _refuse(exc.error_class, exc.detail)
    except Exception as exc:  # noqa: BLE001 - convert all CLI/runtime failures to JSON
        _refuse("report_failed", str(exc))
    finally:
        if connection is not None:
            connection.close()


def _validate_rows(connection: duckdb.DuckDBPyConnection, review_run_id: str | None) -> list[dict]:
    where = " WHERE review_run_id=?" if review_run_id else ""
    params = [review_run_id] if review_run_id else []
    rows = connection.execute(
        "SELECT review_run_id,level,gse_id,gsm_id,accession,original_status,reviewed_status,changed,reason,sources,missing_info FROM geo_review_decisions" + where,
        params,
    ).fetchall()
    issues: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        run_id, level, gse, gsm, accession, original, reviewed, changed, reason, sources, missing = row
        key = (run_id, level, accession)
        if key in seen:
            issues.append({"error_class": "duplicate_accession", "review_run_id": run_id, "accession": accession})
        seen.add(key)
        if level not in {"gse", "gsm"}:
            issues.append({"error_class": "invalid_level", "accession": accession})
        if connection.execute("SELECT 1 FROM geo_datasets WHERE gse_id=?", [gse]).fetchone() is None:
            issues.append({"error_class": "unknown_gse", "accession": accession})
        if gsm is not None:
            sample = connection.execute("SELECT gse_id FROM geo_samples WHERE gsm_id=?", [gsm]).fetchone()
            if sample is None or sample[0] != gse:
                issues.append({"error_class": "gsm_owner_invalid", "accession": accession})
        if reviewed not in STATUSES or original not in STATUSES:
            issues.append({"error_class": "invalid_status", "accession": accession})
        if not isinstance(reason, str) or (changed and not reason.strip()):
            issues.append({"error_class": "reason_invalid", "accession": accession})
        try:
            parsed_sources = json.loads(sources)
            sources_ok = isinstance(parsed_sources, list) and (not changed or bool(parsed_sources) and any(str(v).strip() for v in parsed_sources))
        except (TypeError, json.JSONDecodeError):
            sources_ok = False
        if not sources_ok:
            issues.append({"error_class": "sources_invalid", "accession": accession})
        if reviewed == "uncertain" and not (isinstance(missing, str) and missing.strip()):
            issues.append({"error_class": "missing_info_invalid", "accession": accession})
        if changed != (original != reviewed):
            issues.append({"error_class": "changed_mismatch", "accession": accession})
    return issues


@cli.command("validate")
@click.option("--review-run-id")
@click.pass_context
def validate_command(ctx: click.Context, review_run_id: str | None) -> None:
    connection = None
    try:
        connection = _ensure_existing_schema(ctx.obj["db"])
        issues = _validate_rows(connection, review_run_id)
        _emit(issues)
        if issues:
            raise click.exceptions.Exit(1)
    except ReviewRefusal as exc:
        _refuse(exc.error_class, exc.detail)
    except click.exceptions.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - convert all CLI/runtime failures to JSON
        _refuse("validation_failed", str(exc))
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    cli()
