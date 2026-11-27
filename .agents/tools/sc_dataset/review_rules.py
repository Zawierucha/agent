from __future__ import annotations

import re

_SPACED_WORDS = re.compile(r"(?:\b\w\s){2,}")


def clean_value(v: str) -> str:
    """Lowercase text and join runs of character-spaced words.

    Matching callers normalize an additional copy by replacing underscores
    with spaces, which preserves the original returned value.
    """
    cleaned = re.sub(
        r"(?:\b\w\s){2,}\w\b",
        lambda match: match.group(0).replace(" ", ""),
        v,
    )
    return cleaned.lower()


def _normalized(text: str) -> str:
    return re.sub(r"_+", " ", clean_value(text))


def site_fields(chars: dict) -> list[str]:
    """Return values attached to any anatomy/site-like characteristic key."""
    needles = ("tissue", "site", "location", "region", "organ", "anatom")
    return [str(value) for key, value in chars.items() if any(needle in str(key).casefold() for needle in needles)]


_COLON_RE = re.compile(
    r"\b(?:colon(?:ic)?|rectum|rectal|cecum|caecum|cecal|sigmoid|ascending|transverse|"
    r"descending colon|large intestine|colonoid)s?\b",
    re.IGNORECASE,
)
_NONCOLON_RE = re.compile(
    r"\b(?:ileum|ileal|small intestine|small bowel|jejunum|duodenum|blood|pbmc|"
    r"bone marrow|spleen|thymus|tonsil|lymph node|mesenteric|mesentery|liver|lung|"
    r"brain|frontal|temporal|retina|macula|appendix|synovial|dental|shed|umbilical|"
    r"wharton|adipose|creeping fat|stool|fecal|oral|tongue|perianal|fistula|pouch)s?\b",
    re.IGNORECASE,
)
_AMBIGUOUS_TISSUE_RE = re.compile(
    r"\b(?:gut|intestin(?:e|al)|biopsy|mucosa|lamina propria|organoid)\b",
    re.IGNORECASE,
)


def classify_tissue(text: str) -> str:
    """Classify anatomical origin, favoring anatomy nouns over site modifiers.

    A colon modifier plus a non-colonic anatomical noun (for example,
    ``colonic mesenteric lymph nodes``) is noncolon. Independent colon and
    noncolon origins are ambiguous, as is the compound ``ileocolonic``.
    """
    value = _normalized(text)
    if re.search(r"\bileocolonic\b|\b(?:ileo|ilio)[- ]?cecal\b|\bileocecal\b|\bileocolic\b", value):
        return "ambiguous"
    colon_hits = list(_COLON_RE.finditer(value))
    noncolon_hits = list(_NONCOLON_RE.finditer(value))
    if colon_hits and noncolon_hits:
        # A preceding colon adjective can modify a later non-colonic anatomy noun;
        # independent terms in either order remain an ambiguous mixed specimen.
        first_colon = colon_hits[0]
        if first_colon.group(0).casefold() == "colonic" and all(
            hit.start() > first_colon.start() for hit in noncolon_hits
        ):
            return "noncolon"
        return "ambiguous"
    if noncolon_hits:
        return "noncolon"
    if colon_hits:
        return "colon"
    if _AMBIGUOUS_TISSUE_RE.search(value):
        return "ambiguous"
    return "unknown"


_GEX_RE = re.compile(
    r"\b(?:10x|chromium|feature_bc_matrix|barcodes\.tsv|5['’]?\s*gene expression)\b",
    re.IGNORECASE,
)
_SINGLE_CELL_RE = re.compile(
    r"\b(?:scrna|sc rna|single[- ]cell rna|single[- ]cell(?: transcriptom\w*| atlas| analysis| profiling| sequencing| suspend\w*)?)\b",
    re.IGNORECASE,
)
_SNRNA_RE = re.compile(r"\b(?:single[- ]nucleus|snrna|sn[- ]seq)\b", re.IGNORECASE)
_SPATIAL_RE = re.compile(
    r"\b(?:visium|xenium|cosmx|geomx|seqfish|merfish|slide[- ]seq|rnascope|digital spatial|"
    r"in situ|spatial(?: transcriptom\w*| profil\w*))\b",
    re.IGNORECASE,
)
_REPERTOIRE_RE = re.compile(r"\b(?:tcr|bcr|vdj|scbcr|sctcr)\b", re.IGNORECASE)
_SINGLE_CELL_TOKEN_RE = re.compile(r"\b(?:scrna|sc rna|single[- ]cell|single[- ]nucleus|snrna|sn[- ]seq)\b", re.IGNORECASE)
_BULK_RE = re.compile(r"\b(?:bulk rna|total rna)\b", re.IGNORECASE)
_PROTEIN_RE = re.compile(r"\b(?:adt|cytof|mass cytometry|proteomic\w*)\b", re.IGNORECASE)


def classify_modality(text: str) -> dict[str, bool]:
    """Return modality evidence flags with explicit precedence.

    A single-cell RNA token is suppressed for spatial-only records unless a
    distinct GEX marker (10x/Chromium or a GEX matrix artifact) is present.
    Repertoire-only and protein-only require no GEX marker; bulk RNA is
    suppressed by any single-cell token. Thus ``Xenium In Situ Analyzer`` is
    spatial, not scRNA, while ``Single cells processed through 10X Chromium``
    has a GEX marker and remains scRNA even in a spatially mixed description.
    """
    value = _normalized(text)
    spatial = bool(_SPATIAL_RE.search(value))
    gex = bool(_GEX_RE.search(value))
    single_cell = bool(_SINGLE_CELL_RE.search(value))
    scrna = bool(gex or (single_cell and (not spatial or gex)))
    snrna = bool(_SNRNA_RE.search(value))
    repertoire_only = bool(_REPERTOIRE_RE.search(value) and not gex)
    bulk = bool(_BULK_RE.search(value) and not _SINGLE_CELL_TOKEN_RE.search(value))
    bulk = bulk or bool(
        re.search(r"\b(?:truseq|small rna)\b", value, re.IGNORECASE)
        and not _SINGLE_CELL_TOKEN_RE.search(value)
    )
    protein_only = bool(_PROTEIN_RE.search(value) and not gex)
    return {
        "scrna": scrna,
        "snrna": snrna,
        "spatial": spatial,
        "repertoire_only": repertoire_only,
        "bulk": bulk,
        "protein_only": protein_only,
    }


def qualify_gsm(site_texts: list[str], flags: dict, human: bool, ibd_context: bool) -> str:
    """Apply conservative GSM rules: unresolved anatomy stays uncertain."""
    classes = [classify_tissue(text) for text in site_texts]
    best_site = "colon" if "colon" in classes else "noncolon" if "noncolon" in classes else "ambiguous"
    if human is False or best_site == "noncolon":
        return "exclude"
    has_gex = bool(flags.get("scrna") or flags.get("snrna"))
    if (
        (flags.get("repertoire_only") or flags.get("spatial") or flags.get("bulk") or flags.get("protein_only"))
        and not has_gex
    ):
        return "exclude"
    if human and best_site == "colon" and has_gex and ibd_context:
        return "include"
    return "uncertain"
