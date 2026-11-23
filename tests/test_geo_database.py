from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner
from conftest import (
    REPO_ROOT,
    _assert_database_unchanged,
    make_geo_db,
)
from tools.sc_dataset.geo_database import (
    catalog_report,
    cli,
    get_connection,
    get_dataset,
    get_samples,
    save_characteristics,
    save_dataset,
    save_sample,
)


@pytest.fixture
def db(geo_db: Path) -> Path:
    return geo_db


def test_save_and_get_dataset(db):
    connection = get_connection(db)

    save_dataset(
        connection,
        "GSE163314",
        "IBD single-cell dataset",
        "Test summary",
        "Single-cell analysis",
        "Homo sapiens",
        "GPL24676",
        10,
    )

    dataset = get_dataset(
        connection,
        "GSE163314",
    )

    connection.close()

    assert dataset is not None
    assert dataset[0] == "GSE163314"
    assert dataset[1] == "IBD single-cell dataset"
    assert dataset[2] == "Test summary"
    assert dataset[3] == "Single-cell analysis"
    assert dataset[4] == "Homo sapiens"
    assert dataset[5] == "GPL24676"
    assert dataset[6] == 10


def test_save_dataset_updates_existing_record(db):
    connection = get_connection(db)

    save_dataset(
        connection,
        "GSE163314",
        "Original title",
        None,
        None,
        "Homo sapiens",
        "GPL24676",
        10,
    )

    save_dataset(
        connection,
        "GSE163314",
        "Updated title",
        "Updated summary",
        "Updated design",
        "Homo sapiens",
        "GPL24676",
        20,
    )

    dataset = get_dataset(
        connection,
        "GSE163314",
    )

    connection.close()

    assert dataset is not None
    assert dataset[1] == "Updated title"
    assert dataset[2] == "Updated summary"
    assert dataset[3] == "Updated design"
    assert dataset[6] == 20


def test_save_sample_with_json_characteristics(db):
    connection = get_connection(db)

    save_dataset(
        connection,
        "GSE163314",
        "IBD dataset",
        None,
        None,
        "Homo sapiens",
        "GPL24676",
        1,
    )

    characteristics = {
        "disease": "Crohn disease",
        "tissue": "colon",
        "age": "42",
        "sex": "female",
        "patient": "P001",
        "timepoint": "baseline",
    }

    save_sample(
        connection,
        "GSM4976998",
        "GSE163314",
        "Sample 1",
        None,
        "colon",
        "Homo sapiens",
        "GPL24676",
        characteristics,
        "P001",
        "baseline",
        None,
        None,
        True,
    )

    samples = get_samples(
        connection,
        "GSE163314",
    )

    connection.close()

    assert len(samples) == 1

    sample = samples[0]

    assert sample[0] == "GSM4976998"
    assert sample[1] == "GSE163314"
    assert sample[2] == "Sample 1"
    assert sample[4] == "colon"
    assert sample[5] == "Homo sapiens"
    assert sample[8] == "P001"
    assert sample[9] == "baseline"
    assert sample[12] is True

    stored_characteristics = json.loads(
        sample[7]
    )

    assert stored_characteristics["disease"] == "Crohn disease"
    assert stored_characteristics["tissue"] == "colon"
    assert stored_characteristics["patient"] == "P001"


def test_save_sample_updates_existing_record(db):
    connection = get_connection(db)

    save_dataset(connection, "GSE001", None, None, None, None, None, None)

    save_sample(
        connection,
        "GSM001",
        "GSE001",
        "Original",
        None,
        None,
        "Homo sapiens",
        "GPL24676",
        {"disease": "Crohn disease"},
        "P001",
        "baseline",
        None,
        None,
        True,
    )

    connection.execute(
        "DELETE FROM geo_sample_metadata WHERE gsm_id = ?",
        ["GSM001"],
    )

    save_sample(
        connection,
        "GSM001",
        "GSE001",
        "Updated",
        "Updated summary",
        "colon",
        "Homo sapiens",
        "GPL24676",
        {"disease": "Ulcerative colitis"},
        "P001",
        "week 12",
        "12",
        "weeks",
        True,
    )

    samples = get_samples(
        connection,
        "GSE001",
    )

    connection.close()

    assert len(samples) == 1

    sample = samples[0]

    assert sample[2] == "Updated"
    assert sample[3] == "Updated summary"
    assert sample[4] == "colon"
    assert sample[9] == "week 12"
    assert sample[10] == "12"
    assert sample[11] == "weeks"
    assert sample[12] is True


def test_save_arbitrary_characteristics(db):
    connection = get_connection(db)

    save_dataset(connection, "GSE001", None, None, None, None, None, None)
    save_sample(
        connection,
        "GSM001",
        "GSE001",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )

    save_characteristics(
        connection,
        "GSM001",
        {
            "disease": "Crohn disease",
            "tissue": "ileum",
            "response": "responder",
            "custom_field": "some value",
        },
    )

    rows = connection.execute(
        """
        SELECT
            characteristic,
            value,
            raw_value
        FROM geo_sample_characteristics
        WHERE gsm_id = ?
        ORDER BY characteristic
        """,
        ["GSM001"],
    ).fetchall()

    connection.close()

    assert len(rows) == 4

    values = {
        characteristic: value
        for characteristic, value, _ in rows
    }

    assert values["disease"] == "Crohn disease"
    assert values["tissue"] == "ileum"
    assert values["response"] == "responder"
    assert values["custom_field"] == "some value"


def test_save_characteristics_replaces_existing_values(db):
    connection = get_connection(db)

    save_dataset(connection, "GSE001", None, None, None, None, None, None)
    save_sample(
        connection,
        "GSM001",
        "GSE001",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )

    save_characteristics(
        connection,
        "GSM001",
        {
            "disease": "Crohn disease",
            "tissue": "colon",
        },
    )

    save_characteristics(
        connection,
        "GSM001",
        {
            "disease": "Ulcerative colitis",
            "tissue": "ileum",
            "new_field": "new value",
        },
    )

    rows = connection.execute(
        """
        SELECT
            characteristic,
            value
        FROM geo_sample_characteristics
        WHERE gsm_id = ?
        ORDER BY characteristic
        """,
        ["GSM001"],
    ).fetchall()

    connection.close()

    assert rows == [
        ("disease", "Ulcerative colitis"),
        ("new_field", "new value"),
        ("tissue", "ileum"),
    ]


def test_longitudinal_samples(db):
    connection = get_connection(db)

    save_dataset(connection, "GSE001", None, None, None, None, None, None)

    samples_to_save = [
        (
            "GSM001",
            "baseline",
            None,
            None,
        ),
        (
            "GSM002",
            "week 12",
            "12",
            "weeks",
        ),
        (
            "GSM003",
            "week 24",
            "24",
            "weeks",
        ),
    ]

    for (
        gsm_id,
        timepoint,
        timepoint_value,
        timepoint_unit,
    ) in samples_to_save:
        save_sample(
            connection,
            gsm_id,
            "GSE001",
            f"Sample {gsm_id}",
            None,
            "colon",
            "Homo sapiens",
            "GPL24676",
            {
                "patient": "P001",
                "timepoint": timepoint,
            },
            "P001",
            timepoint,
            timepoint_value,
            timepoint_unit,
            True,
        )

    samples = get_samples(
        connection,
        "GSE001",
    )

    connection.close()

    assert len(samples) == 3
    assert all(
        sample[8] == "P001"
        for sample in samples
    )
    assert all(
        sample[12] is True
        for sample in samples
    )


def test_catalog_report(db):
    connection = get_connection(db)

    save_dataset(
        connection,
        "GSE001",
        "Dataset 1",
        None,
        None,
        "Homo sapiens",
        "GPL24676",
        5,
    )

    save_dataset(
        connection,
        "GSE002",
        "Dataset 2",
        None,
        None,
        "Homo sapiens",
        "GPL24676",
        10,
    )

    connection.execute(
        """
        INSERT INTO geo_qualification (
            gse_id,
            qualification,
            is_human,
            is_single_cell,
            is_ibd_related
        )
        VALUES
            ('GSE001', 'include', TRUE, TRUE, TRUE),
            ('GSE002', 'uncertain', TRUE, TRUE, FALSE)
        """
    )

    report = catalog_report(connection)

    connection.close()

    assert report["total_datasets"] == 2
    assert report["qualifications"]["include"] == 1
    assert report["qualifications"]["uncertain"] == 1

def test_make_geo_db_uses_production_schema(tmp_path: Path) -> None:
    db_path = make_geo_db(tmp_path / "schema-check.duckdb")
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SHOW TABLES").fetchall()
        }
    finally:
        connection.close()

    assert "geo_sample_qualification" in tables


def test_cli_default_database_is_created_in_isolated_directory(
    geo_db: Path,
    tmp_path: Path,
) -> None:
    del geo_db
    assert Path.cwd() == tmp_path
    production_db = REPO_ROOT / "data" / "geo.duckdb"
    before_stat = production_db.stat()
    before_md5 = hashlib.md5(production_db.read_bytes()).hexdigest()

    result = CliRunner().invoke(cli, ["check", "GSE_TRIPWIRE"])

    isolated_default_db = tmp_path / "data" / "geo.duckdb"
    assert result.exit_code != 0
    assert isolated_default_db.exists()
    after_stat = production_db.stat()
    after_md5 = hashlib.md5(production_db.read_bytes()).hexdigest()
    assert (after_stat.st_size, after_stat.st_mtime_ns, after_md5) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
        before_md5,
    )


def test_production_database_sentinel_detects_a_new_file(tmp_path: Path) -> None:
    absent_database = tmp_path / "data" / "geo.duckdb"
    absent_database.parent.mkdir()
    absent_state = None
    absent_database.write_bytes(b"unexpected")

    with pytest.raises(AssertionError, match="changed or appeared"):
        _assert_database_unchanged(absent_database, absent_state)


def _grant_test_artifacts(monkeypatch, db: Path, tmp_path: Path) -> tuple[Path, Path]:
    import gzip
    from unittest.mock import Mock

    from tools.sc_dataset.geo_metadata import main as metadata_main
    from tools.sc_dataset.geo_metadata import preflight_registration

    soft = b"^SERIES = GSE123456\n^SAMPLE = GSM1\n!Sample_characteristics_ch1 = tissue: colon\n"
    response = Mock(content=gzip.compress(soft), status_code=200, headers={})
    response.raise_for_status.return_value = None
    monkeypatch.setattr("tools.sc_dataset.geo_metadata.requests.get", Mock(return_value=response))
    run_dir = tmp_path / "run-grant"
    fetched = CliRunner().invoke(
        metadata_main, ["GSE123456", "--run-dir", str(run_dir), "--run-id", "run-123"]
    )
    assert fetched.exit_code == 0, fetched.output
    issued = CliRunner().invoke(
        preflight_registration, [str(run_dir), "GSE123456", "--db", str(db)]
    )
    assert issued.exit_code == 0, issued.output
    return run_dir, run_dir / "preflight/GSE123456.grant.json"


def _gated_args(command: str, grant: Path | None = None) -> list[str]:
    if command == "save-dataset":
        args = [command, "GSE123456", "--title", "gated title"]
    elif command == "save-sample":
        args = [command, "GSM1", "--gse-id", "GSE123456", "--title", "gated sample"]
    else:
        args = [command, "GSM1", "--gse-id", "GSE123456", "--characteristics", '{"tissue":"colon"}']
    if grant is not None:
        args.extend(["--preflight", str(grant)])
    return args


def test_all_registration_cli_writes_refuse_missing_preflight_before_connect(db, monkeypatch):
    original = db.read_bytes()
    before_digest = hashlib.sha256(original).hexdigest()
    before_state = _db_snapshot(db)
    before_datasets = before_state[1]["geo_datasets"]
    before_samples = before_state[1]["geo_samples"]
    before_characteristics = before_state[1]["geo_sample_characteristics"]

    def forbidden_connect(_path):
        pytest.fail("missing preflight must refuse before opening a database connection")

    monkeypatch.setattr("tools.sc_dataset.geo_database.get_connection", forbidden_connect)
    for command in ("save-dataset", "save-sample", "save-characteristics"):
        result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args(command)])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error_class"] == "preflight_required"
        assert db.read_bytes() == original
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before_digest
    connection = duckdb.connect(str(db), read_only=True)
    assert connection.execute("SELECT * FROM geo_datasets ORDER BY gse_id").fetchall() == before_datasets
    assert connection.execute("SELECT * FROM geo_samples ORDER BY gsm_id").fetchall() == before_samples
    assert connection.execute(
        "SELECT * FROM geo_sample_characteristics ORDER BY gsm_id, characteristic"
    ).fetchall() == before_characteristics
    connection.close()


def _db_snapshot(db: Path) -> tuple[str, dict[str, list[tuple]]]:
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    connection = duckdb.connect(str(db), read_only=True)
    try:
        tables = {}
        for table, order in (
            ("geo_datasets", "gse_id"),
            ("geo_samples", "gsm_id"),
            ("geo_sample_characteristics", "gsm_id, characteristic"),
            ("geo_sample_metadata", "gsm_id"),
            ("geo_qualification", "gse_id"),
            ("geo_sample_qualification", "gsm_id"),
        ):
            tables[table] = connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        return digest, tables
    finally:
        connection.close()


def _invoke_gated(db: Path, command: str, grant: Path, *, gse: str = "GSE123456", gsm: str = "GSM1"):
    args = _gated_args(command, grant)
    if command == "save-dataset":
        args[1] = gse
    else:
        args[1] = gsm
        args[args.index("--gse-id") + 1] = gse
    return CliRunner().invoke(cli, ["--db", str(db), *args])


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema", "grant-v2"),
        ("gse_id", "GSE999999"),
        ("run_id", "tampered-run"),
        ("expected_gsm_ids", ["GSM999"]),
        ("parsed_sha256", "0" * 64),
        ("raw_sha256", "1" * 64),
        ("issued_at_utc", "2026-01-01T00:00:00Z"),
        ("grant_sha256", "2" * 64),
    ],
)
@pytest.mark.parametrize("command", ["save-dataset", "save-sample", "save-characteristics"])
def test_tampering_any_grant_field_is_invalid_without_database_change(
    field, replacement, command, db, monkeypatch, tmp_path
):
    _run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    doc = json.loads(grant.read_text())
    doc[field] = replacement
    grant.write_text(json.dumps(doc))
    before = _db_snapshot(db)
    result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args(command, grant)])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["error_class"] == "grant_invalid"
    assert report["detail"]
    assert _db_snapshot(db) == before


def test_wrong_gse_and_unexpected_gsm_refuse_without_database_change(db, monkeypatch, tmp_path):
    _run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    before = _db_snapshot(db)
    for command in ("save-dataset", "save-sample", "save-characteristics"):
        result = _invoke_gated(db, command, grant, gse="GSE999999")
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error_class"] == "grant_gse_mismatch"
        assert _db_snapshot(db) == before
    result = _invoke_gated(db, "save-sample", grant, gsm="GSM999")
    assert result.exit_code == 1
    assert json.loads(result.output)["error_class"] == "gsm_not_expected"
    result = _invoke_gated(db, "save-characteristics", grant, gsm="GSM999")
    assert result.exit_code == 1
    assert json.loads(result.output)["error_class"] == "gsm_not_expected"
    assert _db_snapshot(db) == before


@pytest.mark.parametrize("artifact", ["parsed", "raw"])
def test_stale_artifact_refuses_all_gated_writes_without_database_change(artifact, db, monkeypatch, tmp_path):
    run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    path = (
        run_dir / "metadata_parsed/GSE123456.json"
        if artifact == "parsed"
        else run_dir / "metadata_raw/GSE123456.soft.gz"
    )
    with path.open("ab") as stream:
        stream.write(b"stale")
    before = _db_snapshot(db)
    for command in ("save-dataset", "save-sample", "save-characteristics"):
        result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args(command, grant)])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error_class"] == "grant_stale_artifact"
        assert _db_snapshot(db) == before


def test_foreign_gsm_ownership_blocks_sample_and_characteristics_without_writes(db, monkeypatch, tmp_path):
    _run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    connection = duckdb.connect(str(db))
    save_dataset(connection, "GSEOTHER", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSEOTHER", None, None, None, None, None, {}, None, None, None, None, None)
    connection.close()
    before = _db_snapshot(db)
    for command in ("save-sample", "save-characteristics"):
        result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args(command, grant)])
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error_class"] == "ownership_conflict"
        assert _db_snapshot(db) == before


def test_dataset_transaction_rechecks_foreign_owner_and_rolls_back_dataset(db, monkeypatch, tmp_path):
    _run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    connection = duckdb.connect(str(db))
    save_dataset(connection, "GSEOTHER", None, None, None, None, None, 1)
    connection.execute("INSERT INTO geo_samples (gsm_id, gse_id) VALUES ('GSM1', 'GSEOTHER')")
    connection.close()
    before = _db_snapshot(db)

    result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args("save-dataset", grant)])

    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error_class"] == "ownership_conflict"
    assert _db_snapshot(db) == before
    connection = duckdb.connect(str(db), read_only=True)
    assert connection.execute("SELECT gse_id FROM geo_datasets WHERE gse_id='GSE123456'").fetchone() is None
    connection.close()


def test_same_gse_registration_resume_succeeds_with_valid_grant(db, monkeypatch, tmp_path):
    _run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    connection = duckdb.connect(str(db))
    connection.execute("INSERT INTO geo_datasets (gse_id, title, n_samples) VALUES ('GSE123456', 'existing', 1)")
    connection.execute("INSERT INTO geo_samples (gsm_id, gse_id, title) VALUES ('GSM1', 'GSE123456', 'existing sample')")
    connection.close()

    dataset = _invoke_gated(db, "save-dataset", grant)
    sample = _invoke_gated(db, "save-sample", grant)
    characteristics = _invoke_gated(db, "save-characteristics", grant)

    assert dataset.exit_code == 0, dataset.output
    assert sample.exit_code == 0, sample.output
    assert characteristics.exit_code == 0, characteristics.output
    connection = duckdb.connect(str(db), read_only=True)
    assert connection.execute("SELECT title FROM geo_datasets WHERE gse_id='GSE123456'").fetchone() == ("gated title",)
    assert connection.execute("SELECT title FROM geo_samples WHERE gsm_id='GSM1'").fetchone() == ("gated sample",)
    assert connection.execute("SELECT value FROM geo_sample_characteristics WHERE gsm_id='GSM1' AND characteristic='tissue'").fetchone() == ("colon",)
    connection.close()


def test_qualification_and_backfill_cli_remain_ungated(db):
    connection = duckdb.connect(str(db))
    save_dataset(connection, "GSE123456", None, None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", None, None, None, None, None, {"tissue": "colon"}, None, None, None, None, None)
    connection.close()

    qualification = CliRunner().invoke(
        cli, ["--db", str(db), "save-qualification", "GSE123456", "--qualification", "include"]
    )
    backfill = CliRunner().invoke(cli, ["--db", str(db), "backfill-sample-metadata", "--gse", "GSE123456"])

    assert qualification.exit_code == 0, qualification.output
    assert backfill.exit_code == 0, backfill.output
    assert json.loads(backfill.output)["processed_samples"] == 1


def test_characteristics_requires_owned_sample_row_even_with_grant(db, monkeypatch, tmp_path):
    _run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    before = _db_snapshot(db)

    result = _invoke_gated(db, "save-characteristics", grant)

    assert result.exit_code == 1
    assert json.loads(result.output)["error_class"] == "sample_missing"
    assert _db_snapshot(db) == before


def test_unreadable_grant_is_invalid_without_database_change(db, tmp_path):
    grant = tmp_path / "missing.grant.json"
    before = _db_snapshot(db)
    for command in ("save-dataset", "save-sample", "save-characteristics"):
        result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args(command, grant)])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error_class"] == "grant_invalid"
        assert _db_snapshot(db) == before


def test_artifact_change_after_db_connect_is_rechecked_before_write(db, monkeypatch, tmp_path):
    run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    before = _db_snapshot(db)
    original_connect = get_connection

    def mutate_after_connect(path):
        connection = original_connect(path)
        with (run_dir / "metadata_parsed/GSE123456.json").open("ab") as stream:
            stream.write(b"raced")
        return connection

    monkeypatch.setattr("tools.sc_dataset.geo_database.get_connection", mutate_after_connect)
    result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args("save-dataset", grant)])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error_class"] == "grant_stale_artifact"
    assert _db_snapshot(db) == before


def test_characteristics_artifact_change_after_db_connect_is_rechecked_before_write(db, monkeypatch, tmp_path):
    run_dir, grant = _grant_test_artifacts(monkeypatch, db, tmp_path)
    connection = get_connection(db)
    save_dataset(connection, "GSE123456", "before", None, None, None, None, 1)
    save_sample(connection, "GSM1", "GSE123456", "before", None, None, None, None, {}, None, None, None, None, None)
    connection.close()
    before = _db_snapshot(db)
    original_connect = get_connection

    def mutate_after_connect(path):
        connection = original_connect(path)
        with (run_dir / "metadata_parsed/GSE123456.json").open("ab") as stream:
            stream.write(b"raced")
        return connection

    monkeypatch.setattr("tools.sc_dataset.geo_database.get_connection", mutate_after_connect)
    result = CliRunner().invoke(cli, ["--db", str(db), *_gated_args("save-characteristics", grant)])

    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error_class"] == "grant_stale_artifact"
    assert _db_snapshot(db) == before


@pytest.mark.parametrize("tool_name", ["geo_database.py", "geo_metadata.py", "geo_envelope.py", "geo_search.py"])
def test_tool_cli_help_works_without_pythonpath(tool_name):
    import os
    import subprocess
    import sys

    script = REPO_ROOT / ".agents" / "tools" / "sc_dataset" / tool_name
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run([sys.executable, str(script), "--help"], check=False, cwd=REPO_ROOT, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
