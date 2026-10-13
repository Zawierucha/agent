from __future__ import annotations

import json

import duckdb
import pytest

from tools.sc_dataset.geo_database import (
    catalog_report,
    get_connection,
    get_dataset,
    get_samples,
    save_characteristics,
    save_dataset,
    save_sample,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "geo.duckdb"

    connection = duckdb.connect(str(db_path))

    connection.execute(
        """
        CREATE TABLE geo_datasets (
            gse_id          VARCHAR PRIMARY KEY,
            title           VARCHAR,
            summary         VARCHAR,
            overall_design  VARCHAR,
            organism        VARCHAR,
            platform        VARCHAR,
            n_samples       INTEGER,
            discovered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE geo_samples (
            gsm_id           VARCHAR PRIMARY KEY,
            gse_id           VARCHAR NOT NULL,
            title            VARCHAR,
            summary          VARCHAR,
            source_name      VARCHAR,
            organism         VARCHAR,
            platform         VARCHAR,
            characteristics  JSON,
            patient_id       VARCHAR,
            timepoint        VARCHAR,
            timepoint_value  VARCHAR,
            timepoint_unit   VARCHAR,
            is_longitudinal  BOOLEAN,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE geo_sample_characteristics (
            gsm_id          VARCHAR NOT NULL,
            characteristic  VARCHAR NOT NULL,
            value           VARCHAR,
            raw_value       VARCHAR,
            PRIMARY KEY (gsm_id, characteristic)
        );

        CREATE TABLE geo_sample_metadata (
            gsm_id              VARCHAR PRIMARY KEY,
            disease             VARCHAR,
            health_state        VARCHAR,
            tissue              VARCHAR,
            cell_type           VARCHAR,
            age                 VARCHAR,
            sex                 VARCHAR,
            treatment           VARCHAR,
            treatment_response  VARCHAR
        );

        CREATE TABLE geo_qualification (
            gse_id           VARCHAR PRIMARY KEY,
            qualification    VARCHAR,
            is_human         BOOLEAN,
            is_single_cell   BOOLEAN,
            is_ibd_related   BOOLEAN,
            disease          VARCHAR,
            modality         VARCHAR,
            tissue           VARCHAR,
            confidence       VARCHAR,
            evidence         VARCHAR,
            uncertainties    VARCHAR,
            qualified_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    connection.close()

    return db_path


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