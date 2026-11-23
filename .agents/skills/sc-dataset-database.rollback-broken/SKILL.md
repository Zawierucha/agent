---
name: sc-dataset-catalog
description: Store, retrieve, and summarize discovered and qualified GEO single-cell datasets in the project DuckDB catalog.
version: 0.1.0
author: Piotr Zawierucha
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [bioinformatics, single-cell, GEO, DuckDB, datasets]
    category: research
---

# Single-Cell Dataset Catalog

Maintain the persistent catalog of GEO single-cell datasets.

## Purpose

Use the project DuckDB database to:

- determine whether a GSE has already been discovered
- store GEO dataset metadata
- store dataset qualification results
- store independent qualification results for every GSM sample
- retrieve previously stored datasets
- produce catalog statistics

The catalog is persistent state for the single-cell dataset discovery
and qualification workflow.

## Tool

Use:

```text
/workspace/.agents/tools/sc_dataset/geo_database.py
```

Use the live catalog directly at:

```text
/workspace/data/geo.duckdb
```

Never create a snapshot or copy unless the user explicitly requests one.

Before qualification, run `check GSE...` for every selected candidate. Save dataset and GSM metadata so that characteristics are normalized into `geo_sample_characteristics` and derived fields are stored in `geo_sample_metadata`. A dataset row and its GSM rows must exist before qualification.

Persist the aggregate GSE decision in `geo_qualification` and a separate row for every GSM in `geo_sample_qualification`. Use `save-qualification GSE... --sample-results-file <JSON>` to store the dataset result and the complete GSM result array in one transaction. The JSON array uses `gsm_id`, `decision` (`include`, `exclude`, or `uncertain`), `tissue_source`, `disease_status`, `modality_evidence`, `evidence`, and `uncertainties`. The command rejects omitted or duplicate registered GSMs and unknown GSMs. Use `get-sample-qualifications GSE...` to verify decisions and completeness. Never infer a GSM decision from the GSE aggregate.

After writes, query the live database and verify exact counts for datasets, GSM samples, sample qualifications, characteristics, metadata rows, and longitudinal records before reporting success.
