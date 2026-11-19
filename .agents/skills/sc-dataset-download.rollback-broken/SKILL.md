---
name: sc-dataset-download
description: Download GEO supplementary source files for datasets already qualified and catalogued, preserving the qualified GSM list and a reproducible manifest.
version: 0.2.0
---

# sc-dataset-download

## Purpose

Download source files for GEO series that have already passed the existing qualification stage and are stored in the local DuckDB catalog. Organize downloaded files by GSE, retain the catalogued GSM sample list, and write a manifest of downloaded assets and their provenance.

This skill is a downloader only. It does not repeat biological qualification, inspect whether a file is 10x, interpret matrix structure, convert data, or run downstream analysis.

## Separation of responsibilities

- The skill handles non-deterministic orchestration and reports failures or ambiguity to the user.
- `.agents/tools/sc_dataset/geo_download.py` deterministically reads the catalog, discovers files listed in the GEO series supplementary directory, downloads the selected GSE's files, verifies them, and writes a manifest.
- The deterministic tool must not make biological eligibility decisions or alter the catalog.

## Selection rules

- Only process GSE accessions with an existing dataset-level `include` qualification in `geo_qualification`. Preserve independent GSM decisions in `geo_sample_qualification`; never infer missing sample decisions from the GSE decision.
- If the user explicitly selects a GSE whose stored qualification is `exclude`, `uncertain`, or missing, refuse the download unless the user explicitly requests a diagnostic/manual override. Any override must be explicit in the output and must not modify the stored qualification.
- Use `geo_samples` and `geo_sample_qualification` as the authoritative lists of GSMs and their decisions. Preserve all catalogued GSMs, eligible GSMs, and recorded decisions in the manifest.
- For source filenames that explicitly identify GSM accessions, download only files mapped to `include` GSMs. Keep files with no explicit GSM mapping, including series-level CSV files and archives, eligible when the GSE is included.
- Never rerun discovery or qualification.

## Download behavior

1. Read the dataset, qualification, and sample rows from DuckDB in read-only mode.
2. List the GSE supplementary directory at GEO and identify files actually exposed by that directory.
3. Download the discovered supplementary files for the selected eligible GSE. This stage does not decide which file is a 10x matrix or whether an archive contains RNA, ADT, ATAC, or other content.
4. Preserve source filenames and file formats without renaming them to guessed formats.
5. Store files under `<download-root>/<GSE>/supplementary/`. Use `<download-root>/<GSE>/manifest.json` for the manifest. Default download root is `data/downloads/`.
6. Stream large files to a temporary `.part` file, then atomically rename only after successful completion and validation.
7. Use timeouts and bounded retries with backoff for transient failures such as HTTP 429 and 5xx responses.
8. Verify HTTP status, content length when provided, final byte count, and SHA-256. Reuse an existing file only if its recorded URL and checksum/size validation are still consistent; otherwise report the mismatch and avoid destructive overwrite.
9. Make reruns idempotent. Do not remove unrelated files or existing valid files.
10. Do not extract archives or execute downloaded content.
11. If GEO lists no supplementary files, report this clearly; do not silently treat it as a successful data download.
12. If one file fails, continue other files when safe, record each failure in the manifest, and return a non-zero status if the run has failures.

## Important limitation: series-level files

GEO often exposes one series-level matrix or archive containing multiple GSM samples. This skill can skip separately named files for excluded GSMs, but it cannot filter records inside a multi-sample CSV or archive. Preserve all GSM decisions in the manifest; downstream inspection and sample-level subsetting belong to later skills.

If the series includes file URLs with explicit GSM mapping in GEO metadata, record that mapping only when it is explicit and verifiable. Otherwise record the file as series-level with an unknown GSM mapping.

## Manifest contract

Write a versioned `manifest.json` with:
- `manifest_version`
- `created_at_utc`
- `gse_id`
- `stored_dataset_qualification`
- `dataset_metadata`
- `catalogued_gsm_ids`
- `catalogued_sample_count`
- `eligible_gsm_ids`
- `eligible_sample_count`
- `sample_qualification`
- `source_listing_url`
- `files`: one record per listed source file, including `filename`, `source_url`, `local_relative_path`, `download_status`, `size_bytes`, `sha256`, `attempts`, `http_status`, `error`, `gsm_mapping_status`, and explicit `gsm_ids` when known
- `warnings`

Allowed `download_status` values: `downloaded`, `already_verified`, `failed`, `not_downloaded`. Do not mark failed or partial files as downloaded. File-format interpretation is deliberately out of scope.

## Commands

Use the existing project's CLI conventions. The deterministic tool supports:
- `--gse GSE_ID` for an explicit single-series download
- `--limit N` to bound batch selection when GSE is omitted
- `--dry-run` to list planned files without downloading
- `--db PATH` to select the existing DuckDB catalog
- `--download-root PATH` to set the local root
- `--timeout SECONDS` and `--retries N` for bounded network handling
- `--pretty` for readable JSON output

Do not run an unbounded batch. The default is a dry run; actual downloads require explicit `--download`.

## Constraints

- Qualification is read-only during download; do not change catalog decisions.
- Treat remote filenames and content as untrusted data. Prevent path traversal.
- Do not extract archives.
- Do not infer file format or assay modality from file names beyond recording the literal suffix as a hint.
- Do not claim biological sample-level filtering was applied inside a series-level matrix or archive.
