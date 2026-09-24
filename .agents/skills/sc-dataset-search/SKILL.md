---
name: sc-dataset-search
description: Search NCBI GEO for candidate single-cell RNA-seq datasets.
version: 0.1.0
author: Piotr Zawierucha
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [bioinformatics, single-cell, GEO, datasets]
    category: research
---

# Single-Cell Dataset Search

Search NCBI GEO for candidate single-cell RNA-seq datasets.

## Goal

Discover GEO Series (GSE) datasets that may contain relevant
single-cell RNA-seq data for downstream qualification.

This skill performs **candidate discovery only**.

It must not decide whether a dataset is a true IBD dataset.

That qualification step is performed separately.

## Search strategy

For IBD-related dataset discovery, search using multiple queries:

- inflammatory bowel disease
- IBD
- Crohn disease
- Crohn's disease
- ulcerative colitis
- ulcerative colitis single cell
- Crohn disease single cell
- inflammatory bowel disease single cell

Run each query separately.

Merge duplicate GEO Series accessions (`GSE`).

## Tool

Use:

```text
tools/sc_dataset/geo_search.py