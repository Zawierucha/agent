# GEO -> AnnData Conversion Stage: Audit and Extension Spec (2026-11-25)

Status: APPROVED SCOPE (master task 2026-11-25 covers audit, design, implementation, tests, controlled run)
Branch baseline: data_download_and-_anndata_preparation @ 441e017, suite 442 passed.

## 1. Audit: actual state (verified against live repo)

The conversion stage ALREADY EXISTS end-to-end and has run on real data:

- Tools: `geo_format_inspect.py` (inventory + format hints, no extraction, v0.2.0),
  `geo_anndata_prepare.py` (schema-route report, v0.3.0), `geo_anndata_load.py`
  (adapter-driven loader v0.1.0), adapters package (`base`, `tenx_mtx`,
  `dense_dge_tar`, `nested_tar_expression`) with auto-registering registry
  (`load_adapters` imports every module exporting `ADAPTER`).
- Canonical contract: `data/anndata_schema.json` v1.1 (status `canonical`),
  cells x features CSR float32, fixed obs/var columns, null discipline,
  `uns.matrix_semantics` evidence rule, `layers['counts']` only when confirmed,
  `cell_type` null until annotation stage.
- DuckDB: `geo_anndata_import_{runs,files,attempts,samples}` in `data/schema.sql`,
  FK-bound; run resume with iteration cap (max 10); schema-change mid-run refused.
- Loader behaviors present: include-only eligibility from catalog decisions,
  per-GSM error isolation, ambiguous-candidate refusal, validate -> atomic temp
  write -> reopen -> validate -> os.replace, resume skips re-validated successes,
  JSON report per GSE.
- Real-data evidence: 8 GSE import runs all `success` in prod DB
  (101 loaded GSMs + not_eligible records), .h5ad outputs under `data/anndata/`.
- Skills: `sc-dataset-format-analysis` 0.2.2, `sc-dataset-anndata-preparation`
  0.3.0, `sc-dataset-anndata-ingestion` 0.1.0 (repo `.agents/skills/`).
- Approved upstream: `sc-dataset-download` contract (manifest.json, .part atomic,
  bounded retries) is NOT touched by this stage.

## 2. Gaps found (audit conclusions)

G1 Test coverage for the load stage is 3 unit tests total; none of the required
   scenarios (standard 10x e2e, multi-sample, missing metadata, ambiguous,
   corrupt file, unconvertible, failure isolation, resume-no-reprocess) exist.
G2 No reader for 10x HDF5 (`*.h5`, CellRanger `/matrix`, `/raw`, `/filtered`
   layouts) although the inspector flags `possible_10x_hdf5_matrix`.
G3 No passthrough adapter for existing H5AD files (`anndata_h5ad` hint exists).
G4 `tenx_mtx.discover` walks only `*.tar` archives; triplets inside
   `.tar.gz`/`.tgz`/`.zip` are never discovered, and nested_tar outer glob is
   restricted to `*_RAW.tar`.
G5 No generic dense CSV/TSV reader (only two GSE-hardcoded dense_dge_tar
   special cases).
G6 `nested_tar_expression` reads whole inner archive members into memory with
   no size bound (unbounded RAM on multi-GB members).
G7 Stale skill text: preparation SKILL says "no AnnData builder stage exists"
   (contradicted by ingestion). Orchestration skills never state the
   post-seal stage order download -> inspect -> prepare -> load.
G8 Project context mentions Nextflow DSL2, but the repo contains no `.nf`
   pipeline at all; orchestration is Python CLIs + gated skills. Out of scope
   here; recorded as a discrepancy for the user.

## 3. Format decision matrix

Deterministic (implement now):
- 10x MTX triplet: loose, inside .tar/.tar.gz/.tgz/.zip (streaming reads, no
  disk extraction; unsafe member paths skipped) [G1/G4]
- 10x HDF5: CellRanger v2/v3 layout verified structurally (barcodes,
  data/indices/indptr, features/name/feature_type); integer matrix ->
  "raw counts" + counts layer; non-integer -> "unknown" [G2]
- H5AD passthrough: object must reopen; contract columns mapped by name,
  source-extra columns are NOT dropped silently - names recorded in
  uns.harmonization_notes; layers['counts'] kept only when integer-valued,
  finite, same shape/order as X; semantics integer->"raw counts" else "unknown" [G3]
- Dense gene-by-cell delimited (loose .tsv/.csv, .gz): admitted ONLY with
  explicit evidence: header[0] in gene/genes/gene.name/featureid/feature_id,
  >=3 columns, all values numeric, non-negative, finite, GSM-prefixed filename.
  Orientation treated as features x cells; ambiguous or row-wise headers ->
  no candidate (needs_manual_review), never a shape-based guess. [G5]
- Nested GSM-labelled tar triplets (existing) + bounded member sizes [G6].

Rule-based / already special-cased (keep, do not generalize silently):
- GSE150115 / GSE162335 dense DGE adapters (approved mechanism, unchanged).

Manual analysis required (report, never force):
- Loom (would need loompy: no new dependency without a decision), RDS/RData
  (declared R conversion workflow), FASTQ (upstream quantification), unknown
  HDF5 internals, series-level single file mixing many GSMs without an explicit
  mapping column, multimodal matrices without clear modality labeling.

## 4. Architecture

Keep the existing responsibility split; extend, do not rebuild:
discovery/inventory (download+inspect) -> routing report (prepare) ->
format-specific readers (adapter registry) -> loader (contract validation,
write, persist, per-GSM isolation, resume/recovery, reports). Registry stays
drop-in: new adapters export `ADAPTER`, no loader edits required. No archive
extraction to disk anywhere; streaming members only. No new third-party
dependency beyond what the venv already has (h5py via anndata is available).

Decisions log (OD): OD-A zip/tgz walk=implement; OD-B h5ad semantics
evidence rule=integer->raw counts else unknown; OD-C generic dense gated by
header evidence, refuse otherwise; OD-D loom/rds/fastq/unknown=manual;
OD-E schema v1.1 unchanged; OD-F Nextflow absence reported, not built;
OD-G hardcoded dense special cases preserved unchanged.

## 5. Test contract (ETAP 6)

Unit (new): tests/test_tenx_hdf5.py (standard layout loads, unknown layout
refused, non-integer -> unknown); tests/test_h5ad_passthrough.py (contract
mapping, counts layer only when integer, extras preserved in notes);
tests/test_tenx_archive_walk.py (tar.gz and zip triplets discovered, unsafe
member paths skipped); tests/test_dense_gene_cell.py (evidence gate admits,
ambiguous header refuses); nested size-bound test.

Integration (new): tests/test_geo_anndata_load_integration.py driving
geo_anndata_load.run() against tmp DB (schema.sql via conftest.make_geo_db,
catalog rows seeded by direct SQL) and synthetic download dirs:
test_standard_10x_gse_end_to_end, test_multi_sample_gse_loads_each_gsm,
test_other_supported_format_loads, test_missing_metadata_columns_remain_null,
test_ambiguous_candidates_refused, test_corrupt_source_file_fails_in_isolation,
test_unconvertible_format_needs_adapter, test_failure_isolation_preserves_successes,
test_resume_does_not_reprocess_successes, test_h5ad_roundtrip_preserves_ids_and_shape,
test_dataset_requires_include_decision, test_schema_change_refuses_resume,
test_not_eligible_samples_recorded.

Controlled e2e: isolated run dir under data/test_runs/, separate DuckDB,
no network, prod DB untouched (md5 a830293f03f13b2e28396b5360ebc0f5 re-checked
afterwards).

## 6. Generality audit addendum (stage 2, 2026-11-25)

Classification (A = generic format, B = generic structure, C = justified
dataset-specific exception, D = overly dataset-bound):
- tenx_matrix_market (tenx_mtx.py): A. Triplet + archive walk is a format
  convention. Defect: tar/zip member-walk duplicated ~40 lines per branch.
- tenx_hdf5.py: A. Structural layout recognition; v2/v3/raw conventions.
- h5ad_passthrough.py: A. Contract-name mapping.
- dense_gene_cell.py: B. Evidence-gated dense structure (header tokens are
  format conventions, not accessions).
- nested_tar_expression.py: B/C. Structure-generic; exact inner signatures
  (10x triplet OR genes.tsv+barcode.tsv+matrix.tsv with fixed headers) are
  deterministic format marks. Style defect: 1-space compressed body.
- dense_dge_tar.py: C (justified exception), NOT D: it encodes dataset-level
  facts not derivable from the format — GSE150115 members are raw counts on
  hg19; GSE162335 members are Seurat LogNormalize values on GRCh38 whose
  per-cell QC columns come from a separate series-level metadata table joined
  by exact sample-label/barcode-order equality. Absorbing it into the
  evidence-gated dense path would silently downgrade semantics and QC for
  these datasets; generalization is therefore REJECTED, the exception stays
  and is documented. Only internal duplication (typed frame, member stream,
  provenance block) may be shared.
- Shared across all adapters (to extract, plain functions, no classes):
  typed-frame construction, canonical obs assembly, provenance/uns block,
  manifest sha lookup, GSM_RE, archive member walk.
Preserved unchanged: registry auto-load, ambiguity refusal, atomic write,
resume/terminal semantics, AnnData v1.1, download stage.
