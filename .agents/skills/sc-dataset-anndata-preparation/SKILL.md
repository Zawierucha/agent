---
name: sc-dataset-anndata-preparation
version: 0.3.0
description: Profile inspected GEO source formats and map every dataset to the project's fixed canonical AnnData schema.
---

# sc-dataset-anndata-preparation

## Purpose

Apply the project's fixed AnnData contract to every dataset that will later enter shared analysis. The sole schema source of truth is `data/anndata_schema.json`; do not propose, infer, or create a dataset-specific schema. This stage starts after GEO files have been downloaded and the format inspector has inventoried them. It reports whether 10x Matrix Market bundles are present, how those bundles map to catalogued GSM samples, which non-10x formats are also present, and what format-specific reader or conversion step is needed for each before mapping into the same contract.

This stage reports the canonical schema and the source-to-schema requirements. It does not construct, merge, normalize, or filter an AnnData object and does not modify the catalog or source files. A future AnnData builder must validate its output against `data/anndata_schema.json` before saving it.

Use only `.agents/tools/sc_dataset/geo_anndata_prepare.py` for the deterministic report.

## Required inputs

- `<download-root>/<GSE>/manifest.json`, produced by `sc-dataset-download`.
- `<download-root>/<GSE>/format_report.json`, produced by `sc-dataset-format-analysis`.
- The read-only DuckDB catalog at `data/geo.duckdb`.
- The canonical contract at `data/anndata_schema.json`.

Do not repeat GEO search, qualification, downloading, or format inspection. Carry forward dataset-level and GSM-level qualification from the catalog; do not revise biological decisions here.

## Workflow

Run:

```bash
uv run python .agents/tools/sc_dataset/geo_anndata_prepare.py \
  --gse GSE_ID --db data/geo.duckdb --download-root data/downloads \
  --schema data/anndata_schema.json
```

Review `anndata_schema_report.json`, including `canonical_schema_id`, `canonical_schema_version`, `canonical_schema_sha256`, `format_summary`, `sample_format_alignment`, `canonical_schema`, `format_specific_handling`, and `limitations`. All datasets must report the same schema ID, version, and hash. Report which samples have complete 10x candidates, which lack a 10x candidate, and what evidence or reader is needed to map each source into the canonical structure. Do not describe a candidate triplet as validated expression data.

## Fixed AnnData contract

- `X`, `layers`, `obs`, `var`, and `uns` must follow `data/anndata_schema.json` exactly.
- Do not omit declared `obs` or `var` columns or add dataset-specific columns. Keep unavailable values as null.
- Keep `obs.cell_type` null until per-cell annotation is performed; never copy a sample-level cell label into it.
- Use the fixed dtypes and index rules in the contract. Do not infer matrix semantics; record `raw counts`, `normalized`, or `unknown` in `uns` based on evidence.
- Keep `layers['counts']` optional and populate it only when raw counts are confirmed and aligned to `X`.

The canonical contract is versioned and mandatory. Change it only through an explicit project-wide schema revision, not while processing an individual dataset.

## Format-specific handling

- **10x Matrix Market:** map a complete matrix/barcodes/features triplet only after checking dimensions, orientation, identifiers, and the GSM-to-file mapping.
- **Possible 10x HDF5 or unknown HDF5:** a file signature or extension alone does not establish its structure. Inspect internal groups and datasets with the matching reader first.
- **Existing H5AD:** inspect `X`, `obs`, `var`, `layers`, and `uns`; map without silently discarding source annotations.
- **Loom:** inspect row and column attributes and matrix orientation before mapping cells to `obs` and features to `var`.
- **CSV, TSV, and Parquet:** identify cell/feature axes and identifier columns from explicit evidence or a user-provided mapping. Do not guess orientation from shape alone; map the result to the canonical columns, leaving unavailable metadata null.
- **RDS/RData:** use an explicit conversion workflow and inspect the converted structure.
- **FASTQ:** these are raw sequencing reads, not an expression matrix; quantify upstream before AnnData.
- **Unknown or incomplete formats:** describe what is known and request the appropriate reader, conversion, or source documentation. Do not force these files into the 10x path.

## Cross-dataset consistency

- Preserve source feature identifiers and their namespace; do not merge by display names alone.
- Align features only after checking genome builds and identifier namespaces.
- Keep modality-specific features labeled and do not mix RNA, antibody, ATAC, or other feature matrices without an explicit multimodal design.
- Record transformations, source hashes, and unresolved mappings in provenance.
- The catalog stores the aggregate decision in `geo_qualification` and each GSM decision in `geo_sample_qualification`. Report both alongside format alignment; future AnnData construction should include only GSMs marked `include` unless the user explicitly overrides that choice.
- Do not require 10x or any other specific source format for biological inclusion. A CSV, HDF5, R object, or other source still follows its format-specific mapping route.

## Limits

The inspector report contains clues and candidate groups, not proof of matrix contents or sample identity. This tool reads those reports and the GSM catalog and embeds the canonical contract and its hash; it does not inspect matrix values, load H5AD/HDF5 internals, perform conversions, or write AnnData. Physical schema enforcement on `.h5ad` outputs belongs in the future builder/validator stage. Non-10x files need format-specific inspection before they can be mapped to the same contract.
