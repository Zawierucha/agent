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
