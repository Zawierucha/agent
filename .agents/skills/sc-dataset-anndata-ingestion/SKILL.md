---
name: sc-dataset-anndata-ingestion
version: 0.1.0
description: Load qualified GEO expression sources through bounded adapters and validate each AnnData object against the canonical contract.
---

# sc-dataset-anndata-ingestion

## Purpose

Turn downloaded, biologically qualified GEO sample data into AnnData objects that comply with the fixed contract in `data/anndata_schema.json`. Use `manifest.json` and the format inventory as input clues. The inventory is preliminary; adapter reads and canonical validation determine whether the data were actually loaded.

This skill does not search GEO, qualify datasets, download files, modify qualification decisions, infer undocumented matrix semantics, or normalize expression values. It creates one validated `.h5ad` per included GSM at `data/anndata/<GSE>/<GSM>.h5ad`. Samples are kept separate so features from incompatible sample matrices are not silently combined.

## Inputs and tools

Require:

- `data/downloads/<GSE>/manifest.json`
- `data/downloads/<GSE>/format_report.json`
- the live catalog `data/geo.duckdb`, including dataset and sample decisions
- the canonical contract `data/anndata_schema.json`

Use `.agents/tools/sc_dataset/geo_anndata_load.py` and adapters under `.agents/tools/sc_dataset/adapters/`. The loader uses the format report for initial routing, then reads and validates the source contents. It writes run, file, attempt, and GSM statuses to DuckDB and reports to `data/anndata/<GSE>/anndata_import_report.json`.

## Workflow

1. Confirm the dataset decision is `include`. Only GSM rows with a stored sample decision of `include` may be loaded. Keep excluded and uncertain GSMs out of AnnData and preserve their status in the import report/database.
2. Check that the manifest and format report refer to the requested GSE. Review the format inventory, archive members, candidate triplets, and candidate-to-GSM mapping. Do not treat a format guess or complete filename triplet as validation.
3. Start an import run. A nonzero exit code is expected while samples remain unresolved; read its JSON output and continue based on the reported status:

   ```bash
   uv run python .agents/tools/sc_dataset/geo_anndata_load.py \
     --gse GSE_ID --db data/geo.duckdb \
     --download-root data/downloads --output-root data/anndata \
     --schema data/anndata_schema.json --max-iterations 10 --pretty
   ```

4. If a registered adapter succeeds, review the validation summary and output files. The loader validates in memory, writes to a temporary H5AD, reopens it, validates it again, and atomically renames it only after both validations pass.
5. If the report returns `needs_adapter`, `partial`, or a recoverable adapter error, inspect the relevant source structure and diagnostics. If no existing adapter fits, create a focused adapter module in `.agents/tools/sc_dataset/adapters/` and retry the unresolved input with the returned `run_id` and the new adapter name:

   ```bash
   uv run python .agents/tools/sc_dataset/geo_anndata_load.py \
     --gse GSE_ID --run-id RUN_ID --adapter ADAPTER_NAME --pretty
   ```

6. A custom module must export `ADAPTER`, an instance implementing `discover(context)` and `load(context, candidate)`. Reuse the `AdapterContext` and `LoadCandidate` types from `adapters.base`. Discovery must map a source to a GSM only when the mapping is explicit and unambiguous. The loader imports project adapter modules; never execute code from downloaded files or archives.
7. Continue only while the recorded run has iterations remaining, up to ten total. Inspect each new error before changing code. Do not repeat an unchanged failed attempt. Preserve successful GSM outputs while resolving other samples.
8. When the run reaches `success`, report the `.h5ad` paths and schema version/hash. When it reaches `needs_manual_review`, stop, retain diagnostics and adapter attempt history, and explain which file or sample needs human review. Do not call the whole dataset intrinsically impossible to load.

## Adapter requirements

- Prefer existing format-specific readers and parsers. Extend the adapter registry only for a real input format not handled by a registered adapter.
- Keep adapters focused on source decoding and mapping. The common loader enforces the AnnData contract after adapter output.
- Never invent feature IDs, GSM mappings, donor metadata, or matrix semantics. Leave unknown metadata null and preserve unresolved information in provenance notes.
- Keep `obs.cell_type` null until a separate per-cell annotation step.
- `X` must be cells × features, CSR, `float32`, with `obs` and `var` in matching order.
- The exact `obs` and `var` columns and dtypes come from `data/anndata_schema.json`. Do not add source-specific columns; retain extra source fields in provenance or the catalog.
- Set `uns.matrix_semantics` to `raw counts`, `normalized`, or `unknown` only from evidence. Do not normalize during import. Populate `layers['counts']` only if raw counts are confirmed and the matrix aligns exactly with `X`.
- Use unique `cell_id` and `feature_id` indices. Prefix source barcodes with GSM when needed. Do not silently deduplicate repeated feature IDs or barcodes; record the failure for review.
- Preserve source files. Read archives without unsafe path extraction; avoid extracting downloaded members when a streaming reader is possible.

## Interpretation of statuses

- `success`: all included GSMs were mapped, loaded, validated against the canonical schema, written, and reopened successfully.
- `partial`: at least one included GSM succeeded and at least one remains unresolved.
- `needs_adapter`: no available adapter could load an eligible source yet; the bounded workflow may continue.
- `failed`: the run itself could not proceed, for example because required inputs or the catalog were unavailable.
- `needs_manual_review`: the iteration limit was reached without resolving every included GSM, or the source has an ambiguity that cannot be safely decided automatically.
- Individual files and GSMs have separate statuses. A failure for one source must not erase successful outputs or decisions for other samples.

## Boundaries

- Do not change the canonical schema while building a dataset. A contract change requires an explicit project-wide revision.
- Do not classify a parseable file as valid expression data until dimensions, axes, sample mapping, and identifiers pass checks.
- Unknown matrix semantics are acceptable for ingestion when values can be faithfully represented; retain `unknown` and do not run normalization.
- FASTQ and non-expression inputs cannot be converted into expression matrices without a declared upstream quantification workflow. Record them as requiring that workflow or manual review.
- This skill records technical importability, not biological qualification or readiness for downstream comparative analysis.
