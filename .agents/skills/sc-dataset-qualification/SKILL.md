---
name: sc-dataset-qualification
description: Qualify candidate human GEO datasets for IBD single-cell analysis using GEO metadata evidence.
version: 0.1.0
author: Piotr Zawierucha
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [bioinformatics, single-cell, GEO, IBD, datasets]
    category: research
---

# Single-Cell Dataset Qualification

Qualify candidate GEO Series identified by the
`sc-dataset-search` skill.

This skill evaluates whether a candidate dataset contains
relevant human single-cell RNA-seq data for downstream IBD
analysis.

Qualification must be based on evidence retrieved from NCBI GEO.

Do not invent information that is not supported by GEO metadata.

## Goal

For each candidate GSE determine whether it should proceed to
the downstream pipeline.

Evaluate the following dimensions independently:

- species
- disease relevance
- sequencing modality
- tissue
- study design

## Species

The downstream pipeline requires human datasets.

Accept:

- Homo sapiens
- Human

Exclude datasets explicitly identified as another organism.

For mixed-organism datasets, report the mixed species and determine
whether human samples are actually present.

If organism information is unavailable, mark it as uncertain.

Do not assume an unknown organism is human.

## Disease relevance

Look for explicit evidence of:

- inflammatory bowel disease
- IBD
- Crohn disease
- Crohn's disease
- ulcerative colitis

Also consider clearly related study terminology when supported by
the metadata.

Do not classify a dataset as IBD-related merely because the original
GEO search query contained an IBD term.

The classification must be supported by the dataset metadata.

## Single-cell modality

Determine whether the dataset contains:

- single-cell RNA sequencing
- scRNA-seq
- single-nucleus RNA sequencing
- snRNA-seq

Distinguish these from:

- bulk RNA-seq
- microarray
- other bulk transcriptomics

If the modality cannot be determined reliably, mark it as uncertain.

## Tissue

Extract tissue information when available.

Pay particular attention to:

- colon
- ileum
- small intestine
- intestine
- intestinal mucosa
- intestinal tissue
- biopsy

Do not automatically exclude other tissues.

Report the actual tissue evidence.

## Study design

Extract information relevant to downstream analysis, including when
available:

- disease state
- healthy/control state
- treatment
- inflammation state
- tissue
- patient/sample information
- disease subtype

## Evidence

Base qualification on evidence from GEO.

Prefer evidence in this order:

1. GEO Series title
2. GEO Series summary
3. GEO overall design
4. GEO sample metadata
5. organism/platform metadata

Clearly distinguish:

- observed evidence
- interpretation
- uncertainty

Do not present an interpretation as an observed GEO fact.

## Qualification

Return one of:

- `include`
- `exclude`
- `uncertain`

Use `uncertain` when the available metadata does not provide enough
evidence for a reliable decision.

A dataset should generally be considered for inclusion when:

- human samples are present
- single-cell or single-nucleus transcriptomics is supported
- IBD relevance is supported by the metadata

Do not require every metadata field to be populated.

## Complete sample metadata

Qualify against the full Series and all GSM metadata; never inspect only a small sample subset. Verify that the retrieved GSM count equals the Series sample-reference count.

If `geo_metadata.py` returns fewer GSM records than the Series declares, retrieve the NCBI GEO MINiML family archive from:

```text
https://ftp.ncbi.nlm.nih.gov/geo/series/<GSE-prefix>nnn/<GSE>/miniml/<GSE>_family.xml.tgz
```

Parse every `<Sample>` element and verify that all `<Sample-Ref>` values resolve. MINiML is authoritative for sample characteristics, protocols, assay-library distinctions, patient clues, and longitudinal timepoints.

## Output

For every GSE return:

- `gse_id`
- `qualification`
- `is_human`
- `is_single_cell`
- `is_ibd_related`
- `disease`
- `modality`
- `tissue`
- `study_design`
- `confidence`
- `evidence`
- `uncertainties`

Evidence should explain why the qualification was reached.

## Important

This skill performs dataset-level qualification only.

It does not:

- download supplementary files
- inspect expression matrices
- perform QC
- normalize expression data
- annotate cells
- perform differential expression