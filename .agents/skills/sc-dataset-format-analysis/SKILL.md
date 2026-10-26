---
name: sc-dataset-format-analysis
version: 0.2.2
description: Analyze downloaded GEO files with a standalone tool and detect sample-prefixed 10x Matrix Market bundles.
---

# sc-dataset-format-analysis

## Purpose

Analyze files already downloaded by `sc-dataset-download`. Use only `.agents/tools/sc_dataset/geo_format_inspect.py`.

## Tool identity and protection

- Never use, overwrite, rename, edit, or invoke `.agents/tools/sc_dataset/geo_download.py` for format analysis.
- The downloader and its skill must remain unchanged.
- This package must not contain `geo_download.py`.

## Scope restrictions

- Inspect only files already present in the download directory.
- Do not rerun GEO search, metadata extraction, qualification, or cataloguing.
- Do not read from or write to DuckDB.
- Do not download, extract, convert, or modify source files.
- Do not create AnnData or run Scanpy.
- Do not decide biological sample eligibility or infer sample identity beyond reporting identifiers present in filenames.
- Do not assume one archive equals one sample or that every archive member is a separate sample.
- Report evidence and uncertainty rather than guessing.

## Inputs and outputs

Default input: `data/downloads/<GSE_ID>/supplementary/`

The tool optionally reads the existing `manifest.json` for context and writes a separate `format_report.json`. It must not overwrite the manifest or source files.

## Workflow

1. Identify the requested GSE accession.
2. Run:
   `uv run python .agents/tools/sc_dataset/geo_format_inspect.py --gse GSE_ID --download-root data/downloads --require-manifest`
3. Review the JSON summary and `format_report.json`.
4. Report file counts, archive members, format hints, candidate 10x bundles, and uncertainties.
5. Stop without starting another pipeline stage.

## Interpretation

- The tool groups common Matrix Market components by shared filename prefix, including GSM-prefixed names such as `GSM123_P1_matrix.mtx.gz`, `GSM123_P1_barcodes.tsv.gz`, and `GSM123_P1_features.tsv.gz`. A complete triplet is a candidate bundle, not validation.
- A typical bundle contains matrix, barcodes, and features/genes files in the same directory or archive prefix.
- HDF5 signatures alone do not prove a file is a 10x matrix; HDF5 internals are not inspected.
- Extensions such as `.h5ad`, `.loom`, `.rds`, `.RData`, `.fastq`, `.fq`, `.csv`, `.tsv`, and `.txt` are format hints, not proof of valid expression data.
- Archive contents are listed without extraction. Nested archives are not recursively inspected.
- Do not claim validation of sample identity, gene IDs, matrix dimensions, barcode alignment, or biological eligibility.
