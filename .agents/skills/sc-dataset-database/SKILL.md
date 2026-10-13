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

Before qualification, run `check GSE...` for every selected candidate. Save included dataset and sample records with the tool's database functions so that characteristics are normalized into `geo_sample_characteristics` and derived fields are stored in `geo_sample_metadata`. Store qualification decisions in `geo_qualification`; a dataset row must exist first because of the foreign key.

After writes, query the live database and verify exact counts for datasets, GSM samples, characteristics, metadata rows, and longitudinal records before reporting success.
