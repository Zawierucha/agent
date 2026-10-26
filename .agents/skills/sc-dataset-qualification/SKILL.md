---
name: sc-dataset-qualification
description: Qualify human GEO single-cell and single-nucleus RNA-seq datasets for IBD research using disease, assay modality, and sample-level tissue evidence.
version: 0.2.0
author: Piotr Zawierucha
license: MIT
platforms:
  - linux
metadata:
  hermes:
    tags:
      - bioinformatics
      - single-cell
      - GEO
      - IBD
      - qualification
    category: research
---

# sc-dataset-qualification

## Purpose

Assess whether GEO datasets and their individual GSM samples are suitable for human inflammatory bowel disease (IBD) single-cell transcriptomics research.

This skill makes two independent decisions: whether the GEO series is relevant as a dataset, and whether each GSM sample is eligible. It does not classify source file formats or require a particular matrix format for biological inclusion. CSV, HDF5, 10x, and other sources remain subject to later inspection and handling.

## Qualification criteria

### Organism

- Include only samples from *Homo sapiens*.
- Exclude samples explicitly identified as non-human.
- Mark samples as `uncertain` when the organism cannot be established from available evidence.

### Disease relevance

Relevant disease groups include:
- Crohn's disease
- Ulcerative colitis
- Inflammatory bowel disease (IBD)

Appropriate non-IBD controls may be included when they belong to a relevant study and meet the other criteria.

Use sample-level evidence to distinguish affected samples, unaffected tissue, and non-IBD controls where possible. Do not infer an individual sample's disease status solely from the dataset title.

### Assay modality

- Include single-cell RNA sequencing (scRNA-seq) and single-nucleus RNA sequencing (snRNA-seq).
- Spatial transcriptomics alone does not establish eligibility for scRNA-seq or snRNA-seq.
- ADT-only, protein-only, and TCR/VDJ-only data do not qualify as RNA-expression assays.
- For multimodal datasets, assess each sample or assay separately when the evidence permits.
- Use series-level assay descriptions when sample-level modality evidence is unavailable, and record this limitation.

### Tissue source

Eligible tissue must originate from the colon or a clearly identified colonic segment, including the rectum and rectal mucosa.

- Include samples with explicit evidence of eligible colonic or rectal origin.
- Do not include blood, PBMC, or other non-colonic tissues solely because the donor has IBD.
- Do not assume that a sample is eligible because the dataset concerns Crohn's disease or ulcerative colitis.
- Treat ambiguous anatomical descriptions, including `ileocolonic`, according to the available sample-level and study-design evidence. Do not infer colon involvement without supporting evidence.
- Mark tissue as `uncertain` when the available evidence cannot establish eligible or ineligible origin.

## Evidence and decisions

Evaluate each GSM sample individually. Use the available series and sample metadata, including:

- Organism
- Sample title and summary
- Source name
- Sample characteristics
- Disease status
- Assay modality
- Series summary and overall design, when needed to interpret sample metadata

Record the evidence supporting each decision. Distinguish explicit evidence from inference, and record missing or conflicting information.

Allowed decisions:

- `include`: all required eligibility criteria are supported by evidence.
- `exclude`: at least one required criterion is explicitly not met.
- `uncertain`: evidence is insufficient or conflicting to make a reliable decision.

Do not silently convert missing evidence into eligibility.

## Output

Return a structured qualification result for the dataset and a separate result for every assessed GSM sample.

Dataset-level fields:

- `gse_id`
- `decision`
- `organism`
- `disease_relevance`
- `assay_modality`
- `eligible_tissue_evidence`
- `included_sample_count`
- `excluded_sample_count`
- `uncertain_sample_count`
- `evidence`
- `uncertainties`
- `exclusion_reasons`

Sample-level fields:

- `gsm_id`
- `tissue_source`
- `disease_status`
- `modality_evidence`
- `decision`
- `evidence`
- `uncertainties`

The dataset decision must reflect the evidence and sample-level results. Do not assume that every sample in a series has the same eligibility.

Keep the dataset decision distinct from GSM decisions. A dataset may be `include` when series metadata establishes that it is a relevant human IBD single-cell/nucleus study even if some GSMs are excluded or uncertain. Include/exclude samples individually using their metadata. Do not downgrade a biologically relevant dataset merely because its supplementary source is CSV or another non-10x format. If the series-level assay or tissue evidence is insufficient, use `uncertain` rather than guessing.

## Scope boundaries

For persistence, provide one complete sample result for every GSM registered under the GSE. Use the catalog tool's atomic dataset-plus-sample save operation; do not save the dataset aggregate while omitting GSM decisions. The tool should reject missing or duplicate GSM IDs.

This skill is responsible only for qualification decisions and their supporting evidence.

Dataset discovery, metadata retrieval implementation, catalog operations, persistence, file downloads, and downstream expression analysis are outside its scope. Use the metadata supplied by the calling workflow and report when it is insufficient for a reliable decision.
