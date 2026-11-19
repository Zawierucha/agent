from __future__ import annotations


import json
import re

from pathlib import Path


import click

import duckdb

DEFAULT_DB = Path("data/geo.duckdb")

DEFAULT_SCHEMA = Path("data/schema.sql")


def get_connection(db_path: Path) -> duckdb.DuckDBPyConnection:

    db_path.parent.mkdir(parents=True, exist_ok=True)

    return duckdb.connect(str(db_path))


def load_schema(
    connection: duckdb.DuckDBPyConnection,
    schema_path: Path,
) -> None:

    sql = schema_path.read_text(encoding="utf-8")

    connection.execute(sql)


def init_database(
    db_path: Path,
    schema_path: Path,
) -> None:

    connection = get_connection(db_path)

    try:

        load_schema(connection, schema_path)

    finally:

        connection.close()


def dataset_exists(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str,
) -> bool:

    result = connection.execute(
        """

        SELECT EXISTS (

            SELECT 1

            FROM geo_datasets

            WHERE gse_id = ?

        )

        """,
        [gse_id],
    ).fetchone()

    return bool(result[0])


def sample_exists(
    connection: duckdb.DuckDBPyConnection,
    gsm_id: str,
) -> bool:

    result = connection.execute(
        """

        SELECT EXISTS (

            SELECT 1

            FROM geo_samples

            WHERE gsm_id = ?

        )

        """,
        [gsm_id],
    ).fetchone()

    return bool(result[0])


_SAMPLE_METADATA_ALIASES = {
    "disease": {
        "disease", "disease status", "diagnosis", "diagnostic group", "condition",
        "disease state", "subject status", "clinical diagnosis", "phenotype", "disease group",
    },
    "health_state": {
        "health state", "inflammation status", "inflammatory status", "inflammatory state",
        "tissue status", "disease activity", "activity status", "inflammation", "activity",
    },
    "tissue": {
        "tissue", "tissue type", "tissue source", "sample source", "organ", "organism part",
        "tissue cell type", "anatomical site", "anatomic site", "biopsy site", "specimen", "tissue origin",
    },
    "cell_type": {
        "cell type", "cell types", "cell population", "cell subset", "cell population type",
    },
    "age": {"age", "age years", "age at collection", "age at biopsy", "donor age", "patient age"},
    "sex": {"sex", "gender", "biological sex"},
    "treatment": {"treatment", "therapy", "medication", "drug", "treatment status"},
    "treatment_response": {
        "treatment response", "response to treatment", "response", "responder status",
        "response group", "therapy response",
    },
}


def _metadata_key(value: str) -> str:
    key = re.sub(r"\bch\d+\b", " ", value.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", key).split())


def _metadata_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def extract_sample_metadata(
    characteristics: dict | list | str | None,
    source_name: str | None = None,
) -> dict[str, str | None]:
    """Map explicit GEO characteristic keys to the fixed sample metadata columns."""
    if isinstance(characteristics, str):
        try:
            characteristics = json.loads(characteristics)
        except json.JSONDecodeError:
            characteristics = {}
    if not isinstance(characteristics, dict):
        characteristics = {}

    aliases = {
        target: {_metadata_key(alias) for alias in names}
        for target, names in _SAMPLE_METADATA_ALIASES.items()
    }
    result: dict[str, str | None] = {name: None for name in _SAMPLE_METADATA_ALIASES}
    for key, value in characteristics.items():
        normalized_key = _metadata_key(str(key))
        raw_value = _metadata_value(value)
        if normalized_key in {"disease state", "subject status", "health state", "inflammation status"} and raw_value:
            state_match = re.search(
                r"\b(uninflamed|inflamed|inactive|active|remission|quiescent)\b",
                raw_value,
                re.IGNORECASE,
            )
            if state_match and result["health_state"] is None:
                result["health_state"] = state_match.group(1).casefold()
        for target, target_aliases in aliases.items():
            if normalized_key in target_aliases and result[target] is None:
                result[target] = raw_value
                break

    # GEO source_name_ch1 is an explicit sample-origin field. Use it as tissue
    # only when it clearly names an anatomical source, not a generic cell sort.
    tissue_cues = re.compile(
        r"\b(colon\w*|rect\w*|ileum\w*|ileal\w*|intestin\w*|gut|mucosa\w*|lamina propria|"
        r"pouch|biopsy|tissue|blood|pbmc|ileocolon\w*|duoden\w*|stomach|gastric|liver|skin)\b",
        re.IGNORECASE,
    )
    if result["tissue"] is None and source_name and tissue_cues.search(source_name):
        result["tissue"] = source_name.strip()
    return result


def _refresh_sample_metadata(connection: duckdb.DuckDBPyConnection, gsm_id: str) -> None:
    row = connection.execute(
        "SELECT characteristics, source_name FROM geo_samples WHERE gsm_id=?", [gsm_id]
    ).fetchone()
    if row is None:
        return
    fields = extract_sample_metadata(row[0], row[1])
    qualification = connection.execute(
        "SELECT disease_status, tissue_source FROM geo_sample_qualification WHERE gsm_id=?",
        [gsm_id],
    ).fetchone()
    if qualification:
        fields["disease"] = fields["disease"] or _metadata_value(qualification[0])
        fields["tissue"] = fields["tissue"] or _metadata_value(qualification[1])
    save_metadata(
        connection, gsm_id, fields["disease"], fields["health_state"], fields["tissue"],
        fields["cell_type"], fields["age"], fields["sex"], fields["treatment"],
        fields["treatment_response"],
    )


def backfill_sample_metadata(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str | None = None,
) -> int:
    query = "SELECT gsm_id FROM geo_samples"
    parameters: list[str] = []
    if gse_id:
        query += " WHERE gse_id=?"
        parameters.append(gse_id)
    rows = connection.execute(query + " ORDER BY gsm_id", parameters).fetchall()
    for (gsm_id,) in rows:
        _refresh_sample_metadata(connection, gsm_id)
    return len(rows)


def save_dataset(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str,
    title: str | None,
    summary: str | None,
    overall_design: str | None,
    organism: str | None,
    platform: str | None,
    n_samples: int | None,
) -> None:

    connection.execute(
        """

        INSERT INTO geo_datasets (

            gse_id,

            title,

            summary,

            overall_design,

            organism,

            platform,

            n_samples

        )

        VALUES (?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT (gse_id)

        DO UPDATE SET

            title = EXCLUDED.title,

            summary = EXCLUDED.summary,

            overall_design = EXCLUDED.overall_design,

            organism = EXCLUDED.organism,

            platform = EXCLUDED.platform,

            n_samples = EXCLUDED.n_samples,

            updated_at = now()

        """,
        [
            gse_id,
            title,
            summary,
            overall_design,
            organism,
            platform,
            n_samples,
        ],
    )


def save_sample(
    connection: duckdb.DuckDBPyConnection,
    gsm_id: str,
    gse_id: str,
    title: str | None,
    summary: str | None,
    source_name: str | None,
    organism: str | None,
    platform: str | None,
    characteristics: dict | list | str | None,
    patient_id: str | None,
    timepoint: str | None,
    timepoint_value: str | None,
    timepoint_unit: str | None,
    is_longitudinal: bool | None,
) -> None:

    if characteristics is None:

        characteristics_json = None

    elif isinstance(characteristics, str):

        characteristics_json = characteristics

    else:

        characteristics_json = json.dumps(
            characteristics,
            ensure_ascii=False,
        )

    connection.execute(
        """

        INSERT INTO geo_samples (

            gsm_id,

            gse_id,

            title,

            summary,

            source_name,

            organism,

            platform,

            characteristics,

            patient_id,

            timepoint,

            timepoint_value,

            timepoint_unit,

            is_longitudinal

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT (gsm_id)

        DO UPDATE SET

            gse_id = EXCLUDED.gse_id,

            title = EXCLUDED.title,

            summary = EXCLUDED.summary,

            source_name = EXCLUDED.source_name,

            organism = EXCLUDED.organism,

            platform = EXCLUDED.platform,

            characteristics = EXCLUDED.characteristics,

            patient_id = EXCLUDED.patient_id,

            timepoint = EXCLUDED.timepoint,

            timepoint_value = EXCLUDED.timepoint_value,

            timepoint_unit = EXCLUDED.timepoint_unit,

            is_longitudinal = EXCLUDED.is_longitudinal,

            updated_at = now()

        """,
        [
            gsm_id,
            gse_id,
            title,
            summary,
            source_name,
            organism,
            platform,
            characteristics_json,
            patient_id,
            timepoint,
            timepoint_value,
            timepoint_unit,
            is_longitudinal,
        ],
    )

    _refresh_sample_metadata(connection, gsm_id)


def save_characteristics(
    connection: duckdb.DuckDBPyConnection,
    gsm_id: str,
    characteristics: dict,
) -> None:

    connection.execute(
        """

        DELETE FROM geo_sample_characteristics

        WHERE gsm_id = ?

        """,
        [gsm_id],
    )

    rows = []

    for characteristic, value in characteristics.items():

        if isinstance(value, (dict, list)):

            value_string = json.dumps(
                value,
                ensure_ascii=False,
            )

        elif value is None:

            value_string = None

        else:

            value_string = str(value)

        rows.append(
            (
                gsm_id,
                str(characteristic),
                value_string,
                value_string,
            )
        )

    if rows:

        connection.executemany(
            """

            INSERT INTO geo_sample_characteristics (

                gsm_id,

                characteristic,

                value,

                raw_value

            )

            VALUES (?, ?, ?, ?)

            """,
            rows,
        )


def save_metadata(
    connection: duckdb.DuckDBPyConnection,
    gsm_id: str,
    disease: str | None,
    health_state: str | None,
    tissue: str | None,
    cell_type: str | None,
    age: str | None,
    sex: str | None,
    treatment: str | None,
    treatment_response: str | None,
) -> None:

    connection.execute(
        """

        INSERT INTO geo_sample_metadata (

            gsm_id,

            disease,

            health_state,

            tissue,

            cell_type,

            age,

            sex,

            treatment,

            treatment_response

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT (gsm_id)

        DO UPDATE SET

            disease = EXCLUDED.disease,

            health_state = EXCLUDED.health_state,

            tissue = EXCLUDED.tissue,

            cell_type = EXCLUDED.cell_type,

            age = EXCLUDED.age,

            sex = EXCLUDED.sex,

            treatment = EXCLUDED.treatment,

            treatment_response = EXCLUDED.treatment_response

        """,
        [
            gsm_id,
            disease,
            health_state,
            tissue,
            cell_type,
            age,
            sex,
            treatment,
            treatment_response,
        ],
    )


def get_dataset(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str,
) -> tuple | None:

    return connection.execute(
        """

        SELECT

            gse_id,

            title,

            summary,

            overall_design,

            organism,

            platform,

            n_samples,

            discovered_at,

            updated_at

        FROM geo_datasets

        WHERE gse_id = ?

        """,
        [gse_id],
    ).fetchone()


def get_samples(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str,
) -> list[tuple]:

    return connection.execute(
        """

        SELECT

            gsm_id,

            gse_id,

            title,

            summary,

            source_name,

            organism,

            platform,

            characteristics,

            patient_id,

            timepoint,

            timepoint_value,

            timepoint_unit,

            is_longitudinal,

            updated_at

        FROM geo_samples

        WHERE gse_id = ?

        ORDER BY gsm_id

        """,
        [gse_id],
    ).fetchall()


def _serialize_text(value: str | list | dict | None) -> str | None:
    """Convert evidence/uncertainty payloads to strings for VARCHAR columns."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def save_qualification(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str,
    qualification: str,
    is_human: bool | None,
    is_single_cell: bool | None,
    is_ibd_related: bool | None,
    disease: str | None,
    modality: str | None,
    tissue: str | None,
    confidence: str | None,
    evidence: str | list | dict | None,
    uncertainties: str | list | dict | None,
) -> None:
    """Insert or update the qualification decision for an existing GSE."""
    allowed_qualifications = {"include", "exclude", "uncertain"}
    if qualification not in allowed_qualifications:
        raise ValueError(
            f"Invalid qualification {qualification!r}; expected one of "
            f"{sorted(allowed_qualifications)}"
        )

    if not dataset_exists(connection, gse_id):
        raise ValueError(
            f"Cannot save qualification for {gse_id}: dataset is not registered "
            "in geo_datasets. Save the dataset first."
        )

    connection.execute(
        """
        INSERT INTO geo_qualification (
            gse_id, qualification, is_human, is_single_cell, is_ibd_related,
            disease, modality, tissue, confidence, evidence, uncertainties,
            qualified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (gse_id)
        DO UPDATE SET
            qualification = EXCLUDED.qualification,
            is_human = EXCLUDED.is_human,
            is_single_cell = EXCLUDED.is_single_cell,
            is_ibd_related = EXCLUDED.is_ibd_related,
            disease = EXCLUDED.disease,
            modality = EXCLUDED.modality,
            tissue = EXCLUDED.tissue,
            confidence = EXCLUDED.confidence,
            evidence = EXCLUDED.evidence,
            uncertainties = EXCLUDED.uncertainties,
            qualified_at = now()
        """,
        [
            gse_id,
            qualification,
            is_human,
            is_single_cell,
            is_ibd_related,
            disease,
            modality,
            tissue,
            confidence,
            _serialize_text(evidence),
            _serialize_text(uncertainties),
        ],
    )


def get_qualification(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str,
) -> tuple | None:
    """Return the stored qualification row for a GSE, or None if absent."""
    return connection.execute(
        """
        SELECT gse_id, qualification, is_human, is_single_cell, is_ibd_related,
               disease, modality, tissue, confidence, evidence, uncertainties,
               qualified_at
        FROM geo_qualification
        WHERE gse_id = ?
        """,
        [gse_id],
    ).fetchone()


def save_sample_qualification(
    connection: duckdb.DuckDBPyConnection,
    gsm_id: str,
    qualification: str,
    tissue_source: str | None = None,
    disease_status: str | None = None,
    modality_evidence: str | None = None,
    evidence: str | list | dict | None = None,
    uncertainties: str | list | dict | None = None,
) -> None:
    """Insert or update a qualification decision for a registered GSM."""
    allowed = {"include", "exclude", "uncertain"}
    qualification = qualification.lower()
    if qualification not in allowed:
        raise ValueError(f"Invalid sample qualification {qualification!r}; expected one of {sorted(allowed)}")
    if not sample_exists(connection, gsm_id):
        raise ValueError(f"Cannot qualify {gsm_id}: sample is not registered in geo_samples")
    connection.execute(
        """
        INSERT INTO geo_sample_qualification (
            gsm_id, qualification, tissue_source, disease_status,
            modality_evidence, evidence, uncertainties, qualified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (gsm_id) DO UPDATE SET
            qualification = EXCLUDED.qualification,
            tissue_source = EXCLUDED.tissue_source,
            disease_status = EXCLUDED.disease_status,
            modality_evidence = EXCLUDED.modality_evidence,
            evidence = EXCLUDED.evidence,
            uncertainties = EXCLUDED.uncertainties,
            qualified_at = now()
        """,
        [gsm_id, qualification, tissue_source, disease_status, modality_evidence,
         _serialize_text(evidence), _serialize_text(uncertainties)],
    )
    _refresh_sample_metadata(connection, gsm_id)


def get_sample_qualifications(connection: duckdb.DuckDBPyConnection, gse_id: str) -> list[tuple]:
    return connection.execute(
        """
        SELECT q.gsm_id, s.gse_id, q.qualification, q.tissue_source,
               q.disease_status, q.modality_evidence, q.evidence,
               q.uncertainties, q.qualified_at
        FROM geo_sample_qualification q
        JOIN geo_samples s USING (gsm_id)
        WHERE s.gse_id = ?
        ORDER BY q.gsm_id
        """,
        [gse_id],
    ).fetchall()


def save_qualification_batch(
    connection: duckdb.DuckDBPyConnection,
    gse_id: str,
    dataset: dict,
    samples: list[dict],
) -> None:
    """Save dataset and GSM decisions together, rolling back incomplete batches."""
    connection.execute("BEGIN TRANSACTION")
    try:
        save_qualification(
            connection, gse_id, dataset["decision"], dataset.get("is_human"),
            dataset.get("is_single_cell"), dataset.get("is_ibd_related"),
            dataset.get("disease"), dataset.get("modality"), dataset.get("tissue"),
            dataset.get("confidence"), dataset.get("evidence"), dataset.get("uncertainties"),
        )
        expected = {row[0] for row in connection.execute(
            "SELECT gsm_id FROM geo_samples WHERE gse_id = ?", [gse_id]
        ).fetchall()}
        observed = [str(item.get("gsm_id", "")) for item in samples]
        if len(observed) != len(set(observed)):
            raise ValueError("Sample qualification payload contains duplicate gsm_id values")
        unknown = set(observed) - expected
        if unknown:
            raise ValueError(f"Sample qualification includes GSMs not registered under {gse_id}: {sorted(unknown)}")
        missing = expected - set(observed)
        if missing:
            raise ValueError(f"Sample qualification is incomplete for {gse_id}; missing GSMs: {sorted(missing)}")
        for item in samples:
            save_sample_qualification(
                connection, item["gsm_id"], item["decision"], item.get("tissue_source"),
                item.get("disease_status"), item.get("modality_evidence"),
                item.get("evidence"), item.get("uncertainties"),
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def catalog_report(
    connection: duckdb.DuckDBPyConnection,
) -> dict:

    total = connection.execute("""

        SELECT COUNT(*)

        FROM geo_datasets

        """).fetchone()[0]

    qualification_rows = connection.execute("""

        SELECT

            qualification,

            COUNT(*)

        FROM geo_qualification

        GROUP BY qualification

        ORDER BY qualification

        """).fetchall()

    qualifications = {
        qualification: count for qualification, count in qualification_rows
    }

    sample_qualification_rows = connection.execute("""
        SELECT qualification, COUNT(*)
        FROM geo_sample_qualification
        GROUP BY qualification
        ORDER BY qualification
    """).fetchall()
    sample_qualifications = {qualification: count for qualification, count in sample_qualification_rows}
    unqualified_samples = connection.execute("""
        SELECT COUNT(*)
        FROM geo_samples s
        LEFT JOIN geo_sample_qualification q USING (gsm_id)
        WHERE q.gsm_id IS NULL
    """).fetchone()[0]

    return {
        "total_datasets": total,
        "qualifications": qualifications,
        "sample_qualifications": sample_qualifications,
        "unqualified_sample_count": unqualified_samples,
    }


@click.group()
@click.option(
    "--db",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB,
    show_default=True,
)
@click.pass_context
def cli(
    ctx: click.Context,
    db: Path,
) -> None:

    ctx.ensure_object(dict)

    ctx.obj["db"] = db


@cli.command("init")
@click.option(
    "--schema",
    "schema_path",
    type=click.Path(
        exists=True,
        path_type=Path,
    ),
    default=DEFAULT_SCHEMA,
    show_default=True,
)
@click.pass_context
def init_command(
    ctx: click.Context,
    schema_path: Path,
) -> None:

    init_database(
        ctx.obj["db"],
        schema_path,
    )

    click.echo(f"Database initialized: {ctx.obj['db']}")


@cli.command("save-dataset")
@click.argument("gse_id")
@click.option("--title")
@click.option("--summary")
@click.option("--overall-design")
@click.option("--organism")
@click.option("--platform")
@click.option("--n-samples", type=int)
@click.pass_context
def save_dataset_command(
    ctx: click.Context,
    gse_id: str,
    title: str | None,
    summary: str | None,
    overall_design: str | None,
    organism: str | None,
    platform: str | None,
    n_samples: int | None,
) -> None:

    connection = get_connection(ctx.obj["db"])

    try:

        save_dataset(
            connection,
            gse_id,
            title,
            summary,
            overall_design,
            organism,
            platform,
            n_samples,
        )

    finally:

        connection.close()

    click.echo(f"Saved dataset: {gse_id}")


@cli.command("save-sample")
@click.argument("gsm_id")
@click.option("--gse-id", required=True)
@click.option("--title")
@click.option("--summary")
@click.option("--source-name")
@click.option("--organism")
@click.option("--platform")
@click.option("--characteristics")
@click.option("--patient-id")
@click.option("--timepoint")
@click.option("--timepoint-value")
@click.option("--timepoint-unit")
@click.option(
    "--is-longitudinal",
    type=bool,
)
@click.pass_context
def save_sample_command(
    ctx: click.Context,
    gsm_id: str,
    gse_id: str,
    title: str | None,
    summary: str | None,
    source_name: str | None,
    organism: str | None,
    platform: str | None,
    characteristics: str | None,
    patient_id: str | None,
    timepoint: str | None,
    timepoint_value: str | None,
    timepoint_unit: str | None,
    is_longitudinal: bool | None,
) -> None:

    connection = get_connection(ctx.obj["db"])

    try:

        parsed_characteristics = None

        if characteristics:

            try:

                parsed_characteristics = json.loads(characteristics)

            except json.JSONDecodeError:

                parsed_characteristics = characteristics

        save_sample(
            connection,
            gsm_id,
            gse_id,
            title,
            summary,
            source_name,
            organism,
            platform,
            parsed_characteristics,
            patient_id,
            timepoint,
            timepoint_value,
            timepoint_unit,
            is_longitudinal,
        )

        if isinstance(parsed_characteristics, dict):

            save_characteristics(
                connection,
                gsm_id,
                parsed_characteristics,
            )

    finally:

        connection.close()

    click.echo(f"Saved sample: {gsm_id}")


@cli.command("get-dataset")
@click.argument("gse_id")
@click.pass_context
def get_dataset_command(
    ctx: click.Context,
    gse_id: str,
) -> None:

    connection = get_connection(ctx.obj["db"])

    try:

        dataset = get_dataset(
            connection,
            gse_id,
        )

    finally:

        connection.close()

    if dataset is None:

        click.echo(f"Dataset not found: {gse_id}")

        raise click.exceptions.Exit(1)

    click.echo(
        json.dumps(
            {
                "gse_id": dataset[0],
                "title": dataset[1],
                "summary": dataset[2],
                "overall_design": dataset[3],
                "organism": dataset[4],
                "platform": dataset[5],
                "n_samples": dataset[6],
                "discovered_at": str(dataset[7]),
                "updated_at": str(dataset[8]),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@cli.command("get-samples")
@click.argument("gse_id")
@click.pass_context
def get_samples_command(
    ctx: click.Context,
    gse_id: str,
) -> None:

    connection = get_connection(ctx.obj["db"])

    try:

        samples = get_samples(
            connection,
            gse_id,
        )

    finally:

        connection.close()

    result = []

    for sample in samples:

        result.append(
            {
                "gsm_id": sample[0],
                "gse_id": sample[1],
                "title": sample[2],
                "summary": sample[3],
                "source_name": sample[4],
                "organism": sample[5],
                "platform": sample[6],
                "characteristics": sample[7],
                "patient_id": sample[8],
                "timepoint": sample[9],
                "timepoint_value": sample[10],
                "timepoint_unit": sample[11],
                "is_longitudinal": sample[12],
                "updated_at": str(sample[13]),
            }
        )

    click.echo(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )


@cli.command("check")
@click.argument("gse_id")
@click.pass_context
def check_command(
    ctx: click.Context,
    gse_id: str,
) -> None:

    connection = get_connection(ctx.obj["db"])

    try:

        exists = dataset_exists(
            connection,
            gse_id,
        )

    finally:

        connection.close()

    click.echo(
        json.dumps(
            {
                "gse_id": gse_id,
                "exists": exists,
            },
            indent=2,
        )
    )


def _parse_optional_bool(value: str) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


@cli.command("save-qualification")
@click.argument("gse_id")
@click.option(
    "--qualification",
    type=click.Choice(["include", "exclude", "uncertain"], case_sensitive=False),
    required=True,
)
@click.option(
    "--is-human",
    type=click.Choice(["true", "false", "unknown"], case_sensitive=False),
    default="unknown",
    show_default=True,
)
@click.option(
    "--is-single-cell",
    type=click.Choice(["true", "false", "unknown"], case_sensitive=False),
    default="unknown",
    show_default=True,
)
@click.option(
    "--is-ibd-related",
    type=click.Choice(["true", "false", "unknown"], case_sensitive=False),
    default="unknown",
    show_default=True,
)
@click.option("--disease")
@click.option("--modality")
@click.option("--tissue")
@click.option("--confidence")
@click.option("--evidence")
@click.option("--uncertainties")
@click.option("--sample-results-file", type=click.Path(exists=True, path_type=Path),
              help="JSON array with one decision per registered GSM; saves dataset and samples atomically")
@click.pass_context
def save_qualification_command(
    ctx: click.Context,
    gse_id: str,
    qualification: str,
    is_human: str,
    is_single_cell: str,
    is_ibd_related: str,
    disease: str | None,
    modality: str | None,
    tissue: str | None,
    confidence: str | None,
    evidence: str | None,
    uncertainties: str | None,
    sample_results_file: Path | None,
) -> None:
    """Save or update a qualification decision for a registered GSE."""
    connection = get_connection(ctx.obj["db"])
    try:
        if sample_results_file:
            try:
                sample_results = json.loads(sample_results_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Cannot read sample results JSON: {exc}") from exc
            if not isinstance(sample_results, list):
                raise ValueError("Sample results JSON must be an array")
            save_qualification_batch(
                connection, gse_id,
                {"decision": qualification.lower(), "is_human": _parse_optional_bool(is_human.lower()),
                 "is_single_cell": _parse_optional_bool(is_single_cell.lower()),
                 "is_ibd_related": _parse_optional_bool(is_ibd_related.lower()),
                 "disease": disease, "modality": modality, "tissue": tissue,
                 "confidence": confidence, "evidence": evidence, "uncertainties": uncertainties},
                sample_results,
            )
        else:
            save_qualification(
                connection, gse_id, qualification.lower(),
                _parse_optional_bool(is_human.lower()),
                _parse_optional_bool(is_single_cell.lower()),
                _parse_optional_bool(is_ibd_related.lower()), disease, modality, tissue,
                confidence, evidence, uncertainties,
            )
        saved = get_qualification(connection, gse_id)
        if saved is None:
            raise click.ClickException(
                f"Qualification write could not be verified for {gse_id}"
            )
        sample_qualification_count = len(get_sample_qualifications(connection, gse_id))
    except (ValueError, duckdb.Error) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        connection.close()

    click.echo(
        json.dumps(
            {
                "gse_id": saved[0],
                "qualification": saved[1],
                "verified": True,
                "sample_qualification_count": sample_qualification_count,
                "qualified_at": str(saved[11]),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@cli.command("get-sample-qualifications")
@click.argument("gse_id")
@click.pass_context
def get_sample_qualifications_command(ctx: click.Context, gse_id: str) -> None:
    """List stored sample-level decisions for a GSE."""
    connection = get_connection(ctx.obj["db"])
    try:
        rows = get_sample_qualifications(connection, gse_id)
    finally:
        connection.close()
    keys = ("gsm_id", "gse_id", "decision", "tissue_source", "disease_status",
            "modality_evidence", "evidence", "uncertainties", "qualified_at")
    click.echo(json.dumps([dict(zip(keys, row)) for row in rows], indent=2, ensure_ascii=False, default=str))


@cli.command("get-qualification")
@click.argument("gse_id")
@click.pass_context
def get_qualification_command(ctx: click.Context, gse_id: str) -> None:
    """Read a stored qualification decision for a GSE."""
    connection = get_connection(ctx.obj["db"])
    try:
        row = get_qualification(connection, gse_id)
    finally:
        connection.close()

    if row is None:
        click.echo(json.dumps({"gse_id": gse_id, "found": False}, indent=2))
        raise click.exceptions.Exit(1)

    click.echo(
        json.dumps(
            {
                "gse_id": row[0],
                "qualification": row[1],
                "is_human": row[2],
                "is_single_cell": row[3],
                "is_ibd_related": row[4],
                "disease": row[5],
                "modality": row[6],
                "tissue": row[7],
                "confidence": row[8],
                "evidence": row[9],
                "uncertainties": row[10],
                "qualified_at": str(row[11]),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@cli.command("report")
@click.pass_context
def report_command(
    ctx: click.Context,
) -> None:

    connection = get_connection(ctx.obj["db"])

    try:

        report_data = catalog_report(connection)

    finally:

        connection.close()

    click.echo(
        json.dumps(
            report_data,
            indent=2,
            ensure_ascii=False,
        )
    )


@cli.command("backfill-sample-metadata")
@click.option("--gse", "gse_id", help="Limit the backfill to one GEO Series accession")
@click.pass_context
def backfill_sample_metadata_command(ctx: click.Context, gse_id: str | None) -> None:
    """Populate normalized sample metadata from stored GEO fields and GSM decisions."""
    connection = get_connection(ctx.obj["db"])
    try:
        processed = backfill_sample_metadata(connection, gse_id.upper() if gse_id else None)
        where_sql = " WHERE s.gse_id=?" if gse_id else ""
        params = [gse_id.upper()] if gse_id else []
        populated = connection.execute(
            f"""SELECT COUNT(*) FROM geo_sample_metadata m
                JOIN geo_samples s USING (gsm_id){where_sql}
                AND ({' OR '.join(f'm.{column} IS NOT NULL' for column in (
                    'disease', 'health_state', 'tissue', 'cell_type', 'age', 'sex',
                    'treatment', 'treatment_response'))})""" if where_sql else
            """SELECT COUNT(*) FROM geo_sample_metadata m
                WHERE disease IS NOT NULL OR health_state IS NOT NULL OR tissue IS NOT NULL
                   OR cell_type IS NOT NULL OR age IS NOT NULL OR sex IS NOT NULL
                   OR treatment IS NOT NULL OR treatment_response IS NOT NULL""",
            params,
        ).fetchone()[0]
        table_rows = connection.execute(
            "SELECT COUNT(*) FROM geo_sample_metadata m JOIN geo_samples s USING (gsm_id)" + where_sql,
            params,
        ).fetchone()[0]
    finally:
        connection.close()
    click.echo(json.dumps({
        "processed_samples": processed,
        "metadata_rows": table_rows,
        "samples_with_at_least_one_normalized_field": populated,
        "gse_id": gse_id.upper() if gse_id else None,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":

    cli()
