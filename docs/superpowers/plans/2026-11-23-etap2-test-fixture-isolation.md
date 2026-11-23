# ETAP 2 – Test Fixture DB & Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task-by-task with strict TDD (superpowers:test-driven-development). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the DuckDB test fixture use the production `data/schema.sql`, structurally prevent tests from touching the production database, and fix the `test_geo_download.py` monkeypatch signature drift.

**Architecture:** A new `tests/conftest.py` owns all test-DB construction (schema-driven factory + `geo_db` fixture that also chdirs into `tmp_path` so the tools' relative `DEFAULT_DB`/`DEFAULT_SCHEMA` resolve inside the sandbox) plus a session-scoped sentinel that fails the run if the production `data/geo.duckdb` bytes changed. `tests/test_geo_database.py` deletes its inline DDL and consumes the fixture; tests gain the parent rows the real FK constraints require. No production tool is modified.

**Tech Stack:** pytest 9, duckdb 1.5, click CliRunner, Python 3.13 (`/workspace/.venv/bin/python`).

**Spec:** ETAP 2 approval message (2026-11-23) + ETAP 1 audit findings F-2, F-3, F-8.

## Global Constraints

- Never open `/workspace/data/geo.duckdb` in write mode; no DROP/DELETE/ALTER on it. Read-only hashing for the sentinel is allowed.
- Do not change production tools (`.agents/tools/**`), business qualification logic, `pyproject.toml`, skills, or add dependencies.
- Do not touch pre-existing dirty paths: `M .agents/skills/sc-dataset-database/SKILL.md`, `?? .agents/skills/sc-dataset-database.rollback-broken/`.
- F-6 stays open: `tests/test_geo_metadata.py::test_get_soft_url_rejects_invalid_accession[GSE12]` is EXPECTED to remain failing; do not edit that test or `geo_metadata.py`.
- Do not delete tests or weaken assertions to get green; adding prerequisite parent rows required by production FKs is allowed and expected.
- Run pytest from `/workspace` (testpaths=`tests`, pythonpath=`.agents`).
- Repo files must be written via `terminal` + python (the `write_file` tool is blocked under `/workspace` in this sandbox).
- Baseline to beat: `6 failed, 132 passed`. Target: exactly `1 failed, 137+ passed` (only the GSE12 case).

## Review Focus

- A test omits `--db` and relies on relative `DEFAULT_DB` → must land in `tmp_path`, never the repo (covered by tripwire test in Task 2).
- Fixture DB missing a table/constraint that production has → tests must fail loudly (covered by schema.sql-driven fixture + new FK failures in Task 1).
- Sentinel silently skipping when the production DB is absent on a fresh clone → sentinel records "absent" state and still fails if the file APPEARS modified content... it must treat absent-at-start + created-during-run as a violation too (Task 2).
- chdir in a fixture leaking into other tests → `monkeypatch.chdir` is function-scoped and reverted automatically; tripwire asserts `Path.cwd()` back at repo root after fixture use (Task 2).

---

### Task 1: Schema-driven fixture (F-2)

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/test_geo_database.py` (fixture + data setup only, not assertions)
- Test: `tests/test_geo_database.py`

**Interfaces:**
- Produces: `REPO_ROOT: Path`, `SCHEMA_PATH: Path`, `make_geo_db(path: Path) -> Path` (module-level in `tests/conftest.py`); fixture `geo_db(tmp_path, monkeypatch) -> Path` returning an isolated DB path built from `data/schema.sql`, with CWD set to `tmp_path`.
- Consumes: `/workspace/data/schema.sql` verbatim.

- [ ] **Step 1: RED (existing)** — Run `pytest tests/test_geo_database.py -q`; observe 4 failures with `CatalogException: Table with name geo_sample_qualification does not exist`. Record output.
- [ ] **Step 2:** Write `tests/conftest.py`: module constants `REPO_ROOT = Path(__file__).resolve().parents[1]`, `SCHEMA_PATH = REPO_ROOT / "data" / "schema.sql"`; `make_geo_db(db_path)` executing `SCHEMA_PATH.read_text()` via `duckdb.connect` then closing; fixture `geo_db(tmp_path, monkeypatch)` that `monkeypatch.chdir(tmp_path)`, builds `tmp_path / "geo.duckdb"` and returns it.
- [ ] **Step 3:** Replace the inline-DDL `db` fixture body in `tests/test_geo_database.py` with `return geo_db` (parametrize via the shared fixture); delete the hand-copied CREATE TABLE block.
- [ ] **Step 4: Verify intermediate RED** — run `pytest tests/test_geo_database.py -q`; expect NEW FK-violation failures (e.g. `Constraint Error` on `geo_samples.gse_id` FK for `GSE001` without dataset row, and `geo_sample_characteristics.gsm_id` FK for `GSM001` without sample row). This proves production constraints are now enforced.
- [ ] **Step 5: GREEN** — In `test_save_sample_updates_existing_record`, `test_longitudinal_samples`, `test_save_arbitrary_characteristics`, `test_save_characteristics_replaces_existing_values` (and any other test the FK failures name): add minimal prerequisite `save_dataset(...)`/`save_sample(...)` calls BEFORE the child inserts. Do not remove or relax any existing assertion.
- [ ] **Step 6:** Run `pytest tests/test_geo_database.py -q` → all pass. Run full `pytest -q` → GSE12 still the only metadata failure; `test_geo_download` F-8 failure still present (fixed in Task 3).
- [ ] **Step 7:** Commit is NOT allowed (user gate). Skip.

### Task 2: Structural isolation + sentinel + tripwire (F-3)

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_geo_database.py` (append tripwire test) or `tests/conftest`-adjacent test file of author's choice inside `tests/`

**Interfaces:**
- Consumes: fixture `geo_db` from Task 1.
- Produces: autouse session fixture `production_db_untouched` that hashes `/workspace/data/geo.duckdb` (size, mtime_ns, md5; handle absent) before/after the session and `pytest.fail`s with a clear message if it changed or appeared.

- [ ] **Step 1: RED** — write the tripwire test first: using CliRunner, invoke `geo_database.cli` with `["check", "GSE_TRIPWIRE"]` and NO `--db`, inside the `geo_db` fixture (CWD = tmp). Assert: exit code != 0 or CatalogException-style failure (fresh file has no tables), AND `(tmp_path / "data" / "geo.duckdb").exists()`, AND the repo `data/geo.duckdb` mtime/md5 unchanged, AND after exiting fixture context `Path.cwd() == REPO_ROOT` is NOT asserted inside the test itself (revert is handled by monkeypatch; verify in Step 3 run-from-repo still passes everything).
- [ ] **Step 2:** Run it; it must pass the "lands in tmp" assertion only once Task 1's chdir is in place — if chdir already exists, RED is demonstrated by the absence of the session sentinel: add a guard test asserting `make_geo_db` output contains table `geo_sample_qualification` (should pass) — keep it.
- [ ] **Step 3:** Implement the session-scoped autouse sentinel in `tests/conftest.py` per interface spec (md5 via `hashlib`, read-only open, tolerant of missing file: record `None` and fail if a file with different-than-recorded state appears).
- [ ] **Step 4: Verify** — full `pytest -q` passes except GSE12; then prove the sentinel works: in a throwaway scratch run OUTSIDE the repo (`cd /tmp && ... pytest` is not used — instead temporarily append a sabotaging test via a file in `tests/`, run only that file, confirm sentinel failure, delete the sabotage file and re-run clean). Record both outputs.

### Task 3: Fix F-8 monkeypatch signature

**Files:**
- Modify: `tests/test_geo_download.py:157`

- [ ] **Step 1: RED (existing)** — `pytest tests/test_geo_download.py -q` fails with `TypeError: <lambda>() takes 2 positional arguments but 3 were given` (production `list_files(gse_id, timeout, retries=3)`, `geo_download.py:120`, call site `:239`).
- [ ] **Step 2:** Change the monkeypatched lambda to accept `(gse, timeout, retries)` and return the same tuple. Do NOT change production code.
- [ ] **Step 3:** Run `pytest tests/test_geo_download.py -q` → pass. Full `pytest -q` → exactly 1 failure (GSE12).

### Task 4: Quality gates + regression evidence

- [ ] `uvx ruff@0.17.5 check tests/conftest.py tests/test_geo_database.py tests/test_geo_download.py` → clean.
- [ ] Per-file isolation: run each touched test file individually (`pytest tests/test_geo_database.py -q`, then `-p no:cacheprovider` variant, then reversed order file list) → identical results.
- [ ] `md5sum /workspace/data/geo.duckdb` equals `a830293f03f13b2e28396b5360ebc0f5` and mtime equals `2026-11-05 12:22:45.679939187 +0000`.
- [ ] `git status --porcelain` shows only: pre-existing dirty entries + new/modified `tests/` files + this plan doc. Nothing else.
- [ ] Report: paste real pytest outputs (before/after), list of changed files, any deviations.

## Self-review notes

- Spec coverage: F-2 → Task 1; F-3 → Task 2; F-8 → Task 3; gates → Task 4. F-6/F-4/F-7 explicitly out of scope per approval.
- Type consistency: `make_geo_db`/`geo_db` names used identically in Tasks 1-2.
- No step asks the implementer to change production behavior.
