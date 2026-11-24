---
name: sc-dataset-qualification
description: Qualify human GEO single-cell and single-nucleus RNA-seq datasets for IBD research using disease, assay modality, and sample-level tissue evidence.
version: 0.3.0
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

## Input contract (Execution Envelope) — REQUIRED

Qualification never starts from a bare GSE list, stdout notes, or conversation memory. The only valid input is a run envelope admitted by the envelope tool (contract: `docs/superpowers/specs/2026-11-23-execution-envelope-design.md`).

Mandatory sequence before the first biological assessment:

1. `uv run python .agents/tools/sc_dataset/geo_envelope.py validate <envelope.json> --stage handoff`
2. `uv run python .agents/tools/sc_dataset/geo_envelope.py admit <envelope.json>` — proceed only on `ADMIT`.
3. On anything else, record the refusal (`refuse <envelope.json> --stage qualification --status BLOCKED_* --reason "<error code + message>"`) and stop.

Refusal conditions (fail-closed; every one has a validator error code):

- Envelope missing, unparseable, or lacking the `search.status` field -> `BLOCKED_INVALID_ENVELOPE`. Absence of a status is never treated as success.
- `search.status = FAILED` -> `BLOCKED_UPSTREAM`. GSE ids salvaged from a failed run are not a handoff.
- `search.status = PARTIAL` without `allowed_to_proceed = partial_accepted` plus a complete recorded acceptance block -> `BLOCKED_UPSTREAM`.
- Handoff reconciliation violation (found GSEs not fully accounted, duplicates, transferred/excluded overlap, foreign ids, missing exclusion reason fields, hash mismatch) -> `BLOCKED_INVALID_ENVELOPE`.
- No handoff section at all (search-stage artifact) -> `BLOCKED_INVALID_ENVELOPE` (`E_HANDOFF_MISSING`).
- `allowed_to_proceed = blocked` -> `BLOCKED_UPSTREAM`.

Closed rationalizations — these are observed baseline failures, not hypotheticals:

- "The documents do not require a status field" — this section requires it; run the validator even when the input "looks fine".
- "The user approved it live in chat" — a live chat message is not an envelope acceptance block. Ask the orchestrator to record the acceptance in the envelope first; until then, refuse.
- "A failed search only corrupts coverage claims, not per-dataset evidence" — assessing salvaged evidence from a failed run is forbidden; the admitted handoff alone defines the workload.

Never hand-set envelope statuses: they are derived. Write your results only through `geo_envelope.py write-qualification <envelope.json> <qual.json> --skill-md <path to this SKILL.md>` (it records the criteria fingerprint incl. this file's sha256) and persist with `geo_database.py save-qualification` as before. The envelope is the execution record; DuckDB remains the catalog; qualification completeness is computed against the handoff list, not against what happens to be mentioned in the report.

## Scope boundaries

For persistence, provide one complete sample result for every GSM registered under the GSE. Use the catalog tool's atomic dataset-plus-sample save operation; do not save the dataset aggregate while omitting GSM decisions. The tool should reject missing or duplicate GSM IDs.

This skill is responsible only for qualification decisions and their supporting evidence.

Dataset discovery, metadata retrieval implementation, catalog operations, persistence, file downloads, and downstream expression analysis are outside its scope. Use the metadata supplied by the calling workflow and report when it is insufficient for a reliable decision.
