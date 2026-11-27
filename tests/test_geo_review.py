from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner
from tools.sc_dataset.geo_review import cli, ensure_schema
from tools.sc_dataset.review_rules import (
    classify_modality,
    classify_tissue,
    clean_value,
    qualify_gsm,
    site_fields,
)

RID = "20261126T120000Z-1234abcd"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "review.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute((Path(__file__).parents[1] / "data/schema.sql").read_text())
    ensure_schema(connection)
    connection.execute("INSERT INTO geo_datasets (gse_id) VALUES ('GSE1'), ('GSE2')")
    connection.execute("INSERT INTO geo_samples (gsm_id, gse_id) VALUES ('GSM1','GSE1'), ('GSM2','GSE1'), ('GSM3','GSE2')")
    connection.execute("INSERT INTO geo_qualification (gse_id, qualification) VALUES ('GSE1','uncertain'), ('GSE2','include')")
    connection.execute("INSERT INTO geo_sample_qualification (gsm_id, qualification) VALUES ('GSM1','uncertain'), ('GSM2','include'), ('GSM3','exclude')")
    connection.close()
    return path


def draft(*decisions: dict) -> dict:
    return {"decisions": list(decisions)}


def write_input(tmp_path: Path, value: dict, name: str = "input.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return path


def invoke(db: Path, *args: str):
    return CliRunner().invoke(cli, ["--db", str(db), *args])


def result_json(result):
    assert result.output.strip(), result.output
    return json.loads(result.output)


def test_uncertain_gse_can_be_reviewed_to_include(db, tmp_path):
    source = write_input(tmp_path, draft({"level":"gse", "gse_id":"GSE1", "reviewed_status":"include", "reason":"Evidence supports inclusion", "sources":["paper"]}))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 0, result.output
    assert result_json(result)["changed"] == 1


def test_uncertain_can_be_reviewed_to_exclude(db, tmp_path):
    source = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"exclude", "reason":"Non-colonic tissue", "sources":["GEO sample fields"]}))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 0, result.output
    assert result_json(result)["changed"] == 1


def test_uncertain_requires_missing_info(db, tmp_path):
    source = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"uncertain", "reason":"Still unresolved", "sources":["GEO"], "missing_info":"Need cell-type annotation"}))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 0, result.output


def test_mixed_gsm_review_rows_are_saved(db, tmp_path):
    decisions = [
        {"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"include", "reason":"colon GEX", "sources":["GEO"]},
        {"level":"gsm", "gsm_id":"GSM2", "gse_id":"GSE1", "reviewed_status":"exclude", "reason":"bulk", "sources":["GEO"]},
    ]
    source = write_input(tmp_path, draft(*decisions))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 0, result.output
    assert result_json(result)["inserted"] == 2


@pytest.mark.parametrize("source_list,reason", [([], "because"), (["source"], "")])
def test_changed_review_without_reason_or_sources_is_atomic(db, tmp_path, source_list, reason):
    source = write_input(tmp_path, draft({"level":"gse", "gse_id":"GSE1", "reviewed_status":"include", "reason":reason, "sources":source_list}))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 1
    assert result_json(result)["errors"]
    connection = duckdb.connect(str(db), read_only=True)
    assert connection.execute("SELECT count(*) FROM geo_review_runs").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM geo_review_decisions").fetchone()[0] == 0
    connection.close()


def test_unknown_gsm_is_refused_before_fk_write(db, tmp_path):
    source = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM_UNKNOWN", "gse_id":"GSE1", "reviewed_status":"include", "reason":"x", "sources":["y"]}))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 1
    assert "unknown_accession" in json.dumps(result_json(result))


def test_duplicate_input_accession_is_refused_atomically(db, tmp_path):
    item = {"level":"gse", "gse_id":"GSE1", "reviewed_status":"uncertain", "reason":"open", "sources":["GEO"], "missing_info":"detail"}
    source = write_input(tmp_path, draft(item, item))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 1
    assert "duplicate_accession" in json.dumps(result_json(result))
    connection = duckdb.connect(str(db), read_only=True)
    assert connection.execute("SELECT count(*) FROM geo_review_runs").fetchone()[0] == 0
    connection.close()


def test_explicit_original_status_mismatch_is_refused(db, tmp_path):
    source = write_input(tmp_path, draft({"level":"gse", "gse_id":"GSE1", "original_status":"include", "reviewed_status":"include", "reason":"same", "sources":["GEO"]}))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 1
    assert result_json(result)["errors"][0]["error_class"] == "original_mismatch"


def test_missing_original_qualification_is_refused(db, tmp_path):
    connection = duckdb.connect(str(db))
    connection.execute("DELETE FROM geo_sample_qualification WHERE gsm_id='GSM1'")
    connection.close()
    source = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"include", "reason":"x", "sources":["y"]}))
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert result.exit_code == 1
    assert result_json(result)["errors"][0]["error_class"] == "qualification_missing"


def test_dry_run_counts_and_never_changes_database(db, tmp_path):
    source = write_input(tmp_path, draft(
        {"level":"gse", "gse_id":"GSE1", "reviewed_status":"include", "reason":"changed", "sources":["paper"]},
        {"level":"gsm", "gsm_id":"GSM2", "gse_id":"GSE1", "reviewed_status":"include", "reason":"same", "sources":["GEO"]},
    ))
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source), "--dry-run")
    assert result.exit_code == 0, result.output
    assert result_json(result) == {"dry_run":True, "would_insert":2, "would_change":1, "errors":[]}
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    connection = duckdb.connect(str(db), read_only=True)
    assert connection.execute("SELECT count(*) FROM geo_review_runs").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM geo_review_decisions").fetchone()[0] == 0
    connection.close()


def test_identical_review_rerun_is_idempotent(db, tmp_path):
    source = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"exclude", "reason":"non-colon", "sources":["GEO"]}))
    first = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    second = invoke(db, "save-review", "--review-run-id", RID, "--input", str(source))
    assert first.exit_code == second.exit_code == 0, second.output
    connection = duckdb.connect(str(db), read_only=True)
    assert connection.execute("SELECT count(*) FROM geo_review_runs").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM geo_review_decisions").fetchone()[0] == 1
    connection.close()


def test_conflicting_review_run_id_is_refused(db, tmp_path):
    first_input = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"exclude", "reason":"first", "sources":["GEO"]}), "first.json")
    second_input = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"include", "reason":"second", "sources":["paper"]}), "second.json")
    assert invoke(db, "save-review", "--review-run-id", RID, "--input", str(first_input)).exit_code == 0
    result = invoke(db, "save-review", "--review-run-id", RID, "--input", str(second_input))
    assert result.exit_code == 1
    assert result_json(result)["error_class"] == "review_run_conflict"


def test_report_counts_and_original_tables_intact(db, tmp_path):
    source = write_input(tmp_path, draft(
        {"level":"gse", "gse_id":"GSE1", "reviewed_status":"include", "reason":"change", "sources":["paper"]},
        {"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"exclude", "reason":"change", "sources":["GEO"]},
        {"level":"gsm", "gsm_id":"GSM2", "gse_id":"GSE1", "reviewed_status":"uncertain", "reason":"open", "sources":["GEO"], "missing_info":"protocol"},
    ))
    assert invoke(db, "save-review", "--review-run-id", RID, "--input", str(source)).exit_code == 0
    result = invoke(db, "report", "--review-run-id", RID)
    assert result.exit_code == 0, result.output
    report = result_json(result)
    assert report == {"gse_reviewed":1, "gsm_reviewed":2, "to_include":1, "to_exclude":1, "to_uncertain":1, "kept":0, "original_tables_intact":True}


def test_get_reviews_latest_wins_per_accession(db, tmp_path):
    for run_id, status in ((RID, "exclude"), ("20261126T120001Z-1234abcd", "include")):
        source = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":status, "reason":status, "sources":["GEO"]}), f"{status}.json")
        assert invoke(db, "save-review", "--review-run-id", run_id, "--input", str(source)).exit_code == 0
    result = invoke(db, "get-reviews", "--gse", "GSE1", "--level", "gsm")
    assert result.exit_code == 0, result.output
    rows = result_json(result)
    assert len(rows) == 1 and rows[0]["reviewed_status"] == "include"


def test_validate_clean_then_detects_direct_sql_tampering(db, tmp_path):
    source = write_input(tmp_path, draft({"level":"gsm", "gsm_id":"GSM1", "gse_id":"GSE1", "reviewed_status":"exclude", "reason":"non-colon", "sources":["GEO"]}))
    assert invoke(db, "save-review", "--review-run-id", RID, "--input", str(source)).exit_code == 0
    clean = invoke(db, "validate", "--review-run-id", RID)
    assert clean.exit_code == 0 and result_json(clean) == []
    connection = duckdb.connect(str(db))
    connection.execute("UPDATE geo_review_decisions SET changed=FALSE WHERE review_run_id=?", [RID])
    connection.close()
    dirty = invoke(db, "validate", "--review-run-id", RID)
    assert dirty.exit_code == 1
    assert result_json(dirty)


def test_invalid_run_id_refused(db, tmp_path):
    source = write_input(tmp_path, draft())
    result = invoke(db, "save-review", "--review-run-id", "bad", "--input", str(source))
    assert result.exit_code == 1 and result_json(result)["error_class"] == "bad_run_id"


def test_ensure_schema_refuses_absent_database(tmp_path):
    result = invoke(tmp_path / "missing.duckdb", "ensure-schema")
    assert result.exit_code == 1
    assert result_json(result)["error_class"] == "db_missing"


# Rule regressions name the real production datasets whose stored source text
# exposed each prior classifier gap.
def test_review_rule_regressions():
    # GSE152321: spaces inside words must not hide the ileum anatomy term.
    assert "colon" in clean_value("C o l o n  Colon")
    # GSE266616: the site field and underscore variant carry ascending-colon.
    assert "Rectum" in site_fields({"site":"Rectum", "tissue":"gut"})
    expected = {
        # GSE284775: colonic modifies lymph nodes, not colon tissue.
        "colonic mesenteric lymph nodes":"noncolon",
        # GSE331003: compound ileocolonic origin is not a single-site verdict.
        "ileocolonic resection":"ambiguous",
        # GSE266546: independent ileum and colon terms are mixed.
        "terminal ileum and ascending colon":"ambiguous",
        "Lamina propria of the small intestine":"noncolon",
        # GSE266616: underscore-delimited colon is still a colon site.
        "ascending_colon":"colon",
        "Lamina propria":"ambiguous",
        # GSE152321: char-spaced Ileum annotation must be recognized.
        "I l e u m RNA from human intestinal biopsy":"noncolon",
        # GSE327417 / GSE284191: appendix is excluded anatomy.
        "appendix":"noncolon",
        # GSE255722: mesentery is not colon tissue.
        "Mesentery":"noncolon",
        # GSE240441 / GSE176367: tonsil sites are non-colonic.
        "Tonsil":"noncolon",
    }
    assert {text: classify_tissue(text) for text in expected} == expected
    xenium = classify_modality("Xenium In Situ Analyzer")
    assert xenium["spatial"] and not xenium["scrna"]
    assert classify_modality("scBCR library")["repertoire_only"]
    assert classify_modality("Single cells processed through 10X Chromium")["scrna"]
    total = classify_modality("RNA-seq analysis of total RNA")
    assert total["bulk"] and not total["scrna"]
    assert classify_modality("CosMx spatial profiler FFPE")["spatial"]
    assert qualify_gsm(["colon"], {"scrna":True}, True, True) == "include"
    assert qualify_gsm(["Lamina propria"], {}, True, True) == "uncertain"
    assert qualify_gsm(["PBMC"], {"scrna":True}, True, True) == "exclude"
    assert qualify_gsm(["colon"], {"spatial":True}, True, True) == "exclude"
    assert qualify_gsm(["colon"], {"scrna":True}, True, False) == "uncertain"
    assert qualify_gsm(["colon"], {"scrna":True}, False, True) == "exclude"


def test_tissue_cecal_variant_regression_gse210955():
    """Regresses GSE210955 readme tissue 'ilio-cecal sectomy': ileocecal compound
    is ambiguous (ileum+cecum), bare 'cecal' is a colon segment."""
    assert classify_tissue("cecal mucosa") == "colon"
    assert classify_tissue("ilio-cecal sectomy") == "ambiguous"
    assert classify_tissue("ileocecal resection") == "ambiguous"


def test_tissue_plural_forms_regression_audit():
    """Regresses GSE329527 'PBMCs' and GSE284775 'mesenteric lymph nodes' plural gaps."""
    assert classify_tissue("PBMCs") == "noncolon"
    assert classify_tissue("colonic mesenteric lymph nodes") == "noncolon"
    assert classify_tissue("Rectum") == "colon"
