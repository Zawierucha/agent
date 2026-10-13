---
name: sc-dataset-search
description: Search NCBI GEO for candidate human single-cell RNA-seq datasets.
version: 0.3.0
author: Piotr Zawierucha
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [bioinformatics, single-cell, GEO, datasets]
    category: research
---

# Single-Cell Dataset Search

Search NCBI GEO for candidate **Homo sapiens single-cell RNA-seq datasets**.

## Goal

Discover GEO Series (GSE) datasets that may contain relevant
single-cell RNA-seq data from **Homo sapiens** for downstream
qualification.

This skill performs **candidate discovery only**.

It must not decide whether a dataset is a true IBD dataset.

That qualification step is performed separately.

## Species constraint

Only consider datasets from:

- Homo sapiens
- Human

The search must be restricted to Homo sapiens whenever NCBI GEO
supports a species/organism search field.

Do not intentionally search for or return datasets whose organism
is another species.

If a GEO result has no reliable organism information, it may be
returned as a candidate but must be marked as having an unknown
organism rather than being assumed to be human.

## Search strategy

For IBD-related dataset discovery, construct multiple independent
search queries covering:

- disease names
- disease abbreviations
- disease synonyms
- single-cell terminology
- relevant combinations of disease and single-cell terminology

The species constraint is mandatory for every search.

### Initial IBD vocabulary

- inflammatory bowel disease
- IBD
- Crohn disease
- Crohn's disease
- ulcerative colitis

### Initial single-cell vocabulary

- single cell
- single-cell
- scRNA-seq
- single-cell RNA sequencing
- 10x Genomics

Combine these terms into multiple useful queries.

Examples:

- inflammatory bowel disease single cell
- IBD single cell
- Crohn disease single cell
- Crohn's disease single cell
- ulcerative colitis single cell
- Crohn disease scRNA-seq
- ulcerative colitis scRNA-seq
- inflammatory bowel disease 10x Genomics

Do not rely on a single query.

The vocabulary may be extended when relevant terminology is discovered
during dataset qualification.

## Query generation

Before calling the tool:

1. Construct a diverse set of search queries from the available
   disease and single-cell vocabulary.
2. Avoid exact duplicate queries.
3. Prefer queries that cover different terminology variants.
4. Apply the **Homo sapiens** constraint to every search.
5. Pass the complete query list to the GEO search tool.

## Tool

Use:

/workspace/.agents/tools/sc_dataset/geo_search.py

Pass the generated search queries to the tool.

The tool is generic and does not contain IBD-specific search terms.

The tool is responsible for:

- querying NCBI GEO
- retrieving GEO records
- extracting GSE accessions
- normalizing results
- deduplicating GSE accessions

The tool must not contain hardcoded IBD terminology.

## Candidate filtering

After receiving the results:

1. Keep candidates identified as Homo sapiens.
2. Exclude candidates explicitly identified as non-human.
3. Do not infer that an unknown organism is Homo sapiens.
4. Report the organism information returned by GEO.

## Output

Return candidate GSE datasets with their available GEO metadata.

For every candidate include, when available:

- GSE accession
- title
- organism
- summary
- platform
- number of samples
- search query that discovered the dataset

Do not perform biological qualification.

Do not claim that a candidate dataset is confirmed to be an IBD
single-cell dataset.