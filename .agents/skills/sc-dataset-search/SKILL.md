---
name: sc-dataset-search
description: Search NCBI GEO for candidate human single-cell RNA-seq datasets.
version: 0.5.0
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

Run through the `terminal` tool from the repository root:

`uv run python .agents/tools/sc_dataset/geo_search.py --query <Q1> --query <Q2> ... --pretty`

Pass the generated search queries to the tool. `--max-results` bounds one
page per query (1..10000, default 100); the tool has no `retstart`
pagination.

The tool is generic and does not contain IBD-specific search terms.

The tool is responsible for:

- querying NCBI GEO
- retrieving GEO records
- extracting GSE accessions
- normalizing results
- deduplicating GSE accessions
- bounded transient-failure handling (see Completeness safeguards)
- reporting per-query completeness in its output envelope

The tool must not contain hardcoded IBD terminology.

## Candidate filtering

After receiving the results:

1. Keep candidates identified as Homo sapiens.
2. Exclude candidates explicitly identified as non-human.
3. Do not infer that an unknown organism is Homo sapiens.
4. Report the organism information returned by GEO.

## Output

The tool prints one JSON envelope:

```
{
  "queries": [
    {"query": "...", "status": "success|partial|failed",
     "count": <int|null>, "retrieved": <int>, "error": <str|null>}
  ],
  "records": [ ...normalized candidate records... ],
  "dropped_no_gse": <int>
}
```

Field semantics:

- `count` — total results NCBI reported for the query; `null` when absent
  or unparsable. Never estimated.
- `retrieved` — IDs actually fetched from NCBI for that query, before
  deduplication and before dropping records without a GSE accession.
- `status` — `success` (query completed; zero results is a success with
  `count=0`, `retrieved=0`), `partial` (completed but `count` exceeds
  `retrieved`, i.e. the single page did not cover all matches), `failed`
  (query errored after bounded retries or on a permanent HTTP error;
  `error` names the exception and reason; other queries' results are kept).
- `dropped_no_gse` — normalized records removed because no GSE accession
  could be located in them.

Example envelope (constructed illustration, not actual NCBI output; run
with `--max-results 2` explains the third query's cap):

```
{
  "queries": [
    {"query": "Crohn disease single cell", "status": "success",
     "count": 4, "retrieved": 4, "error": null},
    {"query": "ulcerative colitis scRNA-seq", "status": "success",
     "count": 0, "retrieved": 0, "error": null},
    {"query": "IBD 10x Genomics", "status": "partial",
     "count": 5, "retrieved": 2, "error": null}
  ],
  "records": [
    {"gse_id": "GSE111", "title": "...", "summary": "...", "taxon": "Homo sapiens",
     "gds_type": "Expression profiling by high throughput sequencing",
     "organism": "Homo sapiens", "platform": "GPL24676", "n_samples": 18,
     "query": "Crohn disease single cell"},
    {"gse_id": "GSE222", "title": "...", "summary": "...", "taxon": "Homo sapiens",
     "gds_type": "Expression profiling by high throughput sequencing",
     "organism": "Homo sapiens", "platform": "GPL14951", "n_samples": 6,
     "query": "Crohn disease single cell"},
    {"gse_id": "GSE333", "title": "...", "summary": "...", "taxon": "Homo sapiens",
     "gds_type": "Expression profiling by high throughput sequencing",
     "organism": "Homo sapiens", "platform": "GPL20781", "n_samples": 24,
     "query": "Crohn disease single cell"},
    {"gse_id": "GSE444", "title": "...", "summary": "...", "taxon": "Homo sapiens",
     "gds_type": "Expression profiling by high throughput sequencing",
     "organism": "Homo sapiens", "platform": "GPL24676", "n_samples": 11,
     "query": "Crohn disease single cell; IBD 10x Genomics"}
  ],
  "dropped_no_gse": 1
}
```

Consistency of the example: 6 IDs retrieved before deduplication (4 + 0 + 2);
1 retrieved record dropped for a missing GSE (`dropped_no_gse`); 5 locatable
records with GSE444 found by two queries deduplicate into 4 candidates.

For every candidate record include, when available: GSE accession, title,
organism, summary, platform, number of samples, discovery query.

Do not perform biological qualification.

Do not claim that a candidate dataset is confirmed to be an IBD
single-cell dataset.

## Completeness safeguards

Act on the envelope; do not treat the search as complete unless every
query report justifies it:

1. Any `status: "failed"` — copy the query and its `error` string verbatim
   into the results summary handed to later stages; the missing coverage
   must be visible there, not only in logs.
2. `status: "partial"` — the page was capped: rerun that query with a
   higher `--max-results` (up to 10000); if `count` exceeds 10000, the
   query is incomplete by design until `retstart` pagination exists —
   state this explicitly and do not call the candidate set complete.
3. `count: null` — completeness is unverifiable for that query; say so
   instead of estimating.
4. `dropped_no_gse` greater than zero — report how many GEO records could
   not be located to a GSE series.
5. `retrieved` counts IDs fetched before deduplication and before dropping
   records without a GSE; never present it as the number of final candidate
   records.

HTTP transport handling belongs to the tool: retryable statuses (403, 429,
500, 502, 503, 504) and transient connection/timeout failures are retried
at most 4 attempts total with `Retry-After` or exponential backoff;
permanent errors such as HTTP 400 fail the query immediately and are
reported, not hidden. Only after the tool reports a query as failed may
the agent substitute a logically equivalent exact-phrase query for a
persistently failing unquoted expression; record both the original query
and the substitution in the results summary.

## Execution envelope emission

The tool additionally writes a run execution envelope when `--output-envelope <path>` is set (optional `--run-id`, `--skill-version`; `TOOL_VERSION 1.1.0`): execution identity with the search-scope fingerprint, per-query accounting including `not_run` entries for planned-but-unattempted queries, derived aggregate `status` (`COMPLETE` | `PARTIAL` | `FAILED`) and `finished_at_utc`. Default stdout keeps its existing contract unchanged; the envelope file is the artifact of record.

Handoff to qualification is built with `geo_envelope.py build-handoff`: every found GSE must be transferred or explicitly excluded with a control-flow reason (`already_registered`, `out_of_scope`, `duplicate_superseries`, `user_directive`, `other`) — a found candidate may never vanish between stages. Eligibility judgements belong to qualification, never to the handoff. The Stage gate below is evaluated from the envelope file, not from chat history.

## Stage gate

Two dimensions are separate and must not be conflated:

- **Execution completeness** — did every *planned* query in the run finish
  as reported in the envelope?
- **Strategy coverage** — did the planned query set span the required IBD
  and single-cell vocabulary?

The gate for starting qualification:

1. Proceed when every planned query has `status: "success"` — including
   `success` with `count=0`, which is a valid finished search, not a
   reason to retry.
2. `partial` means incomplete: first try to complete the query (higher
   `--max-results` up to 10000, or a justified supplementary query;
   `retstart` pagination is not available in this tool).
3. `failed` requires error handling: re-run, or substitute per the
   safeguard above; the error stays visible in the report.
4. `count=null` makes that query's completeness unverifiable: state it,
   never present the run as verified-complete.
5. If `partial`, `failed`, or `count=null` reports remain after justified
   remediation attempts, qualification may start only with the limitations
   explicitly presented and the user's acceptance of continuing.
6. When all planned queries succeeded, do NOT block qualification merely
   because the vocabulary could still be broadened. Strategy gaps are
   reported — list which required vocabulary combinations were not run —
   as an optional extension decision for the user, not as an execution
   failure.
