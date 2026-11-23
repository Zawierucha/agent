# ETAP 3 - GSE12 _get_soft_url Contract Resolution Plan

**Goal:** Encode the approved contract "GSE12 (GSE<1000) is a valid GEO Series accession; _get_soft_url must accept it and build the GSEnnn bucket URL" in tests only.

**Diagnosis:** Implementation already satisfies the contract (re.fullmatch GSE\d+; digits[:-3] -> GSEnnn for <1000). Live evidence: HEAD https://ftp.ncbi.nlm.nih.gov/geo/series/GSEnnn/GSE12/soft/GSE12_family.soft.gz -> HTTP 200 (2026-11-23). Stale test parametrized GSE12 as invalid.

**Decision (user-approved):** Change tests only; no production edits; no GSE12 in production DB; no network in tests.

**Tasks:**
1. tests/test_geo_metadata.py: remove "GSE12" from reject-parametrize (GSE, GSM123, abc, GSE12x retained).
2. Add parametrized exact-URL acceptance test: GSE12/GSEnnn, GSE9686/GSE9nnn, GSE1000/GSE1nnn, GSE10191/GSE10nnn, GSE346721/GSE346nnn.
3. Gates: targeted pytest, full pytest -q, uvx ruff check, DB md5+mtime unchanged, git scope check.

**Out of scope:** pre-existing ruff I001 in this file's import block (exists at HEAD; separate decision), F-4, F-7, geo_metadata.py, DB, conftest.py.
