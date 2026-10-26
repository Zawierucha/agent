#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import json
import re
import tarfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.2.0"
MAX_MEMBER_LIST = 20000
SAMPLE_BYTES = 65536
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".zip", ".7z", ".rar")

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def classify_name(name):
    n = name.lower().replace("\\", "/")
    base = n.rsplit("/", 1)[-1]
    if base in {"matrix.mtx", "matrix.mtx.gz"} or re.search(r"(^|[._-])matrix\.mtx(\.gz)?$", base):
        return "matrix_market_expression_matrix"
    if "barcodes" in base and base.endswith((".tsv", ".txt", ".tsv.gz", ".txt.gz")):
        return "cell_barcodes"
    if base.startswith(("features.tsv", "genes.tsv")) or base in {"genes.txt", "genes.txt.gz"}:
        return "gene_or_feature_annotations"
    if base.endswith(".h5ad"):
        return "anndata_h5ad"
    if base.endswith((".h5", ".hdf5")):
        if "filtered_feature_bc_matrix" in n or "raw_feature_bc_matrix" in n or "10x" in base or "cellranger" in n:
            return "possible_10x_hdf5_matrix"
        return "hdf5_unknown"
    if base.endswith(".loom"):
        return "loom_expression_matrix"
    if base.endswith((".rds", ".rdata", ".rda")):
        return "r_serialized_object"
    if base.endswith(".h5seurat"):
        return "seurat_hdf5_object"
    if base.endswith((".fastq", ".fq", ".fastq.gz", ".fq.gz")):
        return "sequencing_reads_fastq"
    if base.endswith((".mtx", ".mtx.gz")):
        return "matrix_market_file"
    if base.endswith((".csv", ".csv.gz")):
        return "delimited_table_csv"
    if base.endswith((".tsv", ".tsv.gz", ".txt", ".txt.gz")):
        return "delimited_text_or_table"
    if base.endswith(".parquet"):
        return "parquet_table"
    if base.endswith((".json", ".json.gz")):
        return "json_metadata"
    if base.endswith((".xml", ".xml.gz")):
        return "xml_metadata"
    if base.endswith(".pdf"):
        return "documentation_pdf"
    if base.endswith((".md", ".html", ".htm", ".readme")) or base.startswith("readme"):
        return "documentation_or_readme"
    if base.endswith((".gz", ".bz2", ".xz", ".zst")):
        return "compressed_unknown"
    return "unknown"

def sniff_magic(path):
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        return {"magic": None, "error": str(exc)}
    if head.startswith(b"\x1f\x8b"):
        magic = "gzip"
    elif head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        magic = "zip"
    elif head.startswith(b"BZh"):
        magic = "bzip2"
    elif head.startswith(b"\xfd7zXZ\x00"):
        magic = "xz"
    elif head.startswith(b"\x89HDF\r\n\x1a\n"):
        magic = "hdf5"
    elif head.startswith(b"ARROW1"):
        magic = "arrow_or_feather"
    elif head.startswith(b"PAR1"):
        magic = "parquet"
    elif head.startswith(b"SQLite format 3\x00"):
        magic = "sqlite"
    elif head.startswith(b"%%MatrixMarket"):
        magic = "matrix_market"
    elif head.startswith(b"%PDF-"):
        magic = "pdf"
    elif head.startswith(b"<?xml") or head.lstrip().startswith(b"<"):
        magic = "text_or_xml"
    else:
        magic = "unknown"
    return {"magic": magic, "header_hex": head.hex()}

def read_sample(path, limit=SAMPLE_BYTES):
    try:
        if path.name.lower().endswith(".gz"):
            with gzip.open(path, "rb") as handle:
                return handle.read(limit)
        with path.open("rb") as handle:
            return handle.read(limit)
    except (OSError, EOFError, gzip.BadGzipFile):
        return b""

def text_shape(path):
    data = read_sample(path)
    if not data:
        return {"readable_text_sample": False}
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"readable_text_sample": False}
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {"readable_text_sample": True, "nonempty_lines_sampled": 0}
    first = lines[0][:500]
    return {
        "readable_text_sample": True,
        "nonempty_lines_sampled": len(lines),
        "first_nonempty_line": first,
        "delimiter_counts_first_line": {d: first.count(d) for d in ("\t", ",", ";")},
        "matrix_market_header": first.startswith("%%MatrixMarket"),
    }

def is_archive_name(name):
    return name.lower().endswith(ARCHIVE_SUFFIXES)

def inspect_archive(path):
    result = {"archive": True, "members": [], "member_count": 0, "truncated_member_listing": False, "archive_error": None}
    members = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                result["member_count"] = len([i for i in infos if not i.is_dir()])
                for info in infos[:MAX_MEMBER_LIST]:
                    if info.is_dir():
                        continue
                    members.append({"name": info.filename, "size_bytes": info.file_size, "compressed_size_bytes": info.compress_size, "encrypted": bool(info.flag_bits & 1), "format_guess": classify_name(info.filename), "nested_archive_candidate": is_archive_name(info.filename)})
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                count = 0
                for member in archive:
                    if not member.isfile():
                        continue
                    count += 1
                    if len(members) < MAX_MEMBER_LIST:
                        members.append({"name": member.name, "size_bytes": member.size, "format_guess": classify_name(member.name), "nested_archive_candidate": is_archive_name(member.name)})
                result["member_count"] = count
        else:
            result["archive"] = False
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        result["archive_error"] = str(exc)
    result["members"] = members
    result["truncated_member_listing"] = result["member_count"] > MAX_MEMBER_LIST
    if result["archive"]:
        counts = defaultdict(int)
        for member in members:
            counts[member["format_guess"]] += 1
        result["member_format_counts"] = dict(sorted(counts.items()))
    return result

def group_10x_candidates(items):
    groups = defaultdict(dict)
    suffix_patterns = (
        ("matrix", re.compile(r"^(?P<prefix>.+?)[._-]matrix\.mtx(?:\.gz)?$", re.IGNORECASE)),
        ("barcodes", re.compile(r"^(?P<prefix>.+?)[._-]barcodes\.tsv(?:\.gz)?$", re.IGNORECASE)),
        ("features", re.compile(r"^(?P<prefix>.+?)[._-](?:features|genes)\.tsv(?:\.gz)?$", re.IGNORECASE)),
    )
    for item in items:
        name = item.get("name", "")
        normalized = name.replace("\\", "/")
        if "::" in normalized:
            archive_name, member_name = normalized.split("::", 1)
            path_name = member_name
            origin = archive_name
        else:
            path_name = normalized
            origin = ""
        path_name = path_name.strip("/")
        parent = path_name.rsplit("/", 1)[0] if "/" in path_name else "."
        base = path_name.rsplit("/", 1)[-1]
        for kind, pattern in suffix_patterns:
            match = pattern.match(base)
            if not match:
                continue
            prefix = match.group("prefix")
            group_key = (origin, parent, prefix)
            groups[group_key][kind] = name
            break
    result = []
    for (origin, parent, prefix), parts in sorted(groups.items()):
        missing = sorted({"matrix", "barcodes", "features"} - set(parts))
        group_path = "/".join(part for part in (origin, parent if parent != "." else "", prefix) if part)
        result.append({
            "directory_or_archive_prefix": group_path or prefix,
            "sample_or_bundle_prefix": prefix,
            "members": parts,
            "detected_components": sorted(parts),
            "missing_components": missing,
            "complete_standard_10x_triplet": not missing,
            "interpretation": "candidate_standard_10x_matrix_market_bundle" if not missing else "partial_10x_like_bundle",
            "confidence": "high" if not missing else "moderate",
        })
    return result

def find_input_root(download_root, gse, explicit):
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Input directory does not exist or is not a directory: {root}")
        return root
    gse_dir = Path(download_root).expanduser().resolve() / gse
    if not gse_dir.exists():
        raise ValueError(f"Dataset download directory does not exist: {gse_dir}")
    supplementary = gse_dir / "supplementary"
    return supplementary if supplementary.exists() else gse_dir

def load_manifest(gse_dir):
    path = gse_dir / "manifest.json"
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read manifest {path}: {exc}") from exc

def inspect_file(path, root, skip_hash):
    stat = path.stat()
    result = {
        "name": path.name,
        "relative_path": str(path.relative_to(root)),
        "size_bytes": stat.st_size,
        "modified_time_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "format_guess": classify_name(path.name),
        "magic": sniff_magic(path),
    }
    if not skip_hash:
        result["sha256"] = sha256_file(path)
    if path.name.lower().endswith(".gz") and not is_archive_name(path.name):
        result["compressed_content_sample"] = text_shape(path)
    if is_archive_name(path.name) or result["magic"].get("magic") in {"zip", "gzip", "bzip2", "xz"}:
        archive = inspect_archive(path)
        result["archive_inspection"] = archive
        if archive.get("members"):
            result["10x_candidate_groups"] = group_10x_candidates(archive["members"])
    elif result["format_guess"] in {"matrix_market_expression_matrix", "matrix_market_file", "cell_barcodes", "gene_or_feature_annotations", "delimited_table_csv", "delimited_text_or_table", "json_metadata", "xml_metadata"}:
        result["content_sample"] = text_shape(path)
    return result

def analyze(args):
    input_root = find_input_root(args.download_root, args.gse, args.input_dir)
    gse_dir = input_root.parent if input_root.name == "supplementary" else input_root
    manifest, manifest_path = load_manifest(gse_dir)
    if args.require_manifest and manifest is None:
        raise ValueError(f"Required manifest.json not found under {gse_dir}")
    disk_files = sorted((p for p in input_root.rglob("*") if p.is_file() and not p.name.endswith(".part")), key=lambda p: str(p).lower())
    inspected = [inspect_file(path, input_root, args.skip_hash) for path in disk_files]
    inventory = []
    for item in inspected:
        inventory.append({"name": item["name"], "relative_path": item["relative_path"], "format_guess": item["format_guess"]})
        for member in item.get("archive_inspection", {}).get("members", []):
            inventory.append({"name": member["name"], "relative_path": item["relative_path"] + "::" + member["name"], "format_guess": member["format_guess"], "archive_member": True, "size_bytes": member.get("size_bytes")})
    candidates = group_10x_candidates(inventory)
    counts = defaultdict(int)
    for item in inventory:
        counts[item["format_guess"]] += 1
    report = {
        "report_version": VERSION,
        "generated_at_utc": utc_now(),
        "gse_id": args.gse,
        "input_root": str(input_root),
        "manifest_path": str(manifest_path) if manifest_path else None,
        "manifest_found": manifest is not None,
        "catalogued_gsm_ids": (manifest or {}).get("catalogued_gsm_ids"),
        "qualification": (manifest or {}).get("qualification"),
        "scope": "file inventory and evidence-based format hints only; no extraction, conversion, sample-level biological interpretation, or database writes",
        "summary": {
            "files_on_disk": len(inspected),
            "archive_members_listed": sum(len(item.get("archive_inspection", {}).get("members", [])) for item in inspected),
            "format_guess_counts_including_listed_archive_members": dict(sorted(counts.items())),
            "complete_standard_10x_triplet_candidates": sum(1 for item in candidates if item["complete_standard_10x_triplet"]),
            "partial_10x_like_candidates": sum(1 for item in candidates if not item["complete_standard_10x_triplet"]),
        },
        "files": inspected,
        "10x_candidate_groups": candidates,
        "limitations": [
            "Extensions and signatures are clues, not definitive validation.",
            "HDF5 internals are not inspected; an HDF5 file is not automatically considered a 10x matrix.",
            "Archives are listed but never extracted.",
            "Candidate 10x triplets are inferred from names and paths; matrix dimensions and alignment are not validated.",
            "Nested archives are flagged but not recursively inspected.",
            "Sample inclusion and sample identity are not inferred from file contents.",
        ],
    }
    output = Path(args.output).expanduser().resolve() if args.output else gse_dir / "format_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "success",
        "gse_id": args.gse,
        "input_root": str(input_root),
        "files_on_disk": len(inspected),
        "archive_members_listed": report["summary"]["archive_members_listed"],
        "format_guess_counts": report["summary"]["format_guess_counts_including_listed_archive_members"],
        "complete_10x_triplet_candidates": report["summary"]["complete_standard_10x_triplet_candidates"],
        "partial_10x_candidates": report["summary"]["partial_10x_like_candidates"],
        "report_path": str(output),
        "manifest_found": manifest is not None,
    }, indent=2))
    return 0

def main():
    parser = argparse.ArgumentParser(description="Inventory downloaded GEO files and infer likely formats without extracting or converting them.")
    parser.add_argument("--gse", required=True, help="GEO series accession, e.g. GSE346721")
    parser.add_argument("--download-root", default="data/downloads", help="Root directory used by geo_download.py")
    parser.add_argument("--input-dir", help="Optional explicit directory to inspect instead of <download-root>/<GSE>/supplementary")
    parser.add_argument("--output", help="Optional report path; defaults to <GSE directory>/format_report.json")
    parser.add_argument("--require-manifest", action="store_true", help="Fail if manifest.json is absent")
    parser.add_argument("--skip-hash", action="store_true", help="Skip SHA-256 calculation for files on disk")
    args = parser.parse_args()
    try:
        return analyze(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")

if __name__ == "__main__":
    raise SystemExit(main())
