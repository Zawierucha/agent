#!/usr/bin/env python3
"""Validate project skill directories under .agents/skills/.

Checks SKILL.md structure against this repository's conventions: frontmatter
delimiters and YAML validity, required `name`/`description`, name/directory
consistency, description length, body size, backticked relative file
references (references/, templates/, scripts/), and machine-local paths.
Findings are errors (exit 1) or warnings (exit stays 0).

Usage:
    uv run python .agents/skills/agent-development/scripts/validate_skill.py \
        .agents/skills/<name> [more skill dirs ...]
    uv run python ... --root .agents/skills

Exit codes: 0 = no errors, 1 = at least one error, 2 = usage/environment.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    print(
        "PyYAML is required; run this script via `uv run python ...`.",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_NAME_LENGTH = 64
DESCRIPTION_SOFT_LIMIT = 60
DESCRIPTION_HARD_LIMIT = 1024
CONTENT_SOFT_LIMIT = 20_000
CONTENT_HARD_LIMIT = 100_000
LINKED_PATH = re.compile(r"`((?:references|templates|scripts)/[^`\s]+)`")
MACHINE_PATH = re.compile(r"/home/[\w.-]+|/Users/|[A-Za-z]:\\")
ARCHIVE_SUFFIX = ".rollback-broken"
REQUIRED_FIELDS = ("name", "description")
CONVENTION_FIELDS = ("version", "author", "license", "platforms")


@dataclass
class SkillReport:
    """Validation outcome for one skill directory."""

    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def split_frontmatter(content: str) -> tuple[str, str] | None:
    """Return (frontmatter_text, body_text), or None if delimiters are invalid."""
    if not content.startswith("---"):
        return None
    lines = content.splitlines()
    if lines[0].rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return frontmatter, body
    return None


def validate_skill_dir(skill_dir: Path) -> SkillReport:
    """Validate one skill directory; SKILL.md must exist inside it."""
    report = SkillReport(path=skill_dir)
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        report.errors.append(f"missing SKILL.md in {skill_dir}")
        return report

    content = skill_md.read_text(encoding="utf-8")

    if len(content) > CONTENT_HARD_LIMIT:
        report.errors.append(
            f"SKILL.md is {len(content)} chars (> {CONTENT_HARD_LIMIT})"
        )
    elif len(content) > CONTENT_SOFT_LIMIT:
        report.warnings.append(
            f"SKILL.md is {len(content)} chars; move detail to references/"
        )

    split = split_frontmatter(content)
    if split is None:
        report.errors.append(
            "frontmatter must start with '---' on line 1 and close with '---'"
        )
        return report
    frontmatter_text, body = split

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        report.errors.append(f"frontmatter is not valid YAML: {exc}")
        return report
    if not isinstance(frontmatter, dict):
        report.errors.append("frontmatter must be a YAML mapping")
        return report

    _check_name(frontmatter, skill_dir, report)
    _check_description(frontmatter, report)
    _check_conventions(frontmatter, report)
    _check_body(body, report)
    _check_linked_paths(content, skill_dir, report)
    _check_machine_paths(content, report)
    return report


def _check_name(
    frontmatter: dict[object, object], skill_dir: Path, report: SkillReport
) -> None:
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        report.errors.append("frontmatter field 'name' is missing or empty")
        return
    if not NAME_PATTERN.match(name):
        report.errors.append(f"name '{name}' is not lowercase-hyphen")
    if len(name) > MAX_NAME_LENGTH:
        report.errors.append(f"name exceeds {MAX_NAME_LENGTH} chars")
    if name != skill_dir.name:
        report.warnings.append(
            f"name '{name}' does not match directory '{skill_dir.name}'; the"
            " loader registers skills under the frontmatter name"
        )


def _check_description(frontmatter: dict[object, object], report: SkillReport) -> None:
    description = frontmatter.get("description")
    if description is None:
        report.errors.append("frontmatter field 'description' is missing")
        return
    if not isinstance(description, str):
        report.errors.append(
            "description must be a string; quote it if it contains ':'"
        )
        return
    if not description.strip():
        report.errors.append("description is empty")
        return
    if len(description) > DESCRIPTION_HARD_LIMIT:
        report.errors.append(
            f"description is {len(description)} chars (> {DESCRIPTION_HARD_LIMIT})"
        )
    elif len(description) > DESCRIPTION_SOFT_LIMIT:
        report.warnings.append(
            f"description is {len(description)} chars; convention is "
            f"<= {DESCRIPTION_SOFT_LIMIT}"
        )
    if not description.rstrip().endswith("."):
        report.warnings.append("description should end with a period")


def _check_conventions(frontmatter: dict[object, object], report: SkillReport) -> None:
    for field_name in CONVENTION_FIELDS:
        if field_name not in frontmatter:
            report.warnings.append(f"missing conventional field '{field_name}'")
    platforms = frontmatter.get("platforms")
    if platforms is not None and not isinstance(platforms, list):
        report.warnings.append("platforms should be a list")
    author = frontmatter.get("author")
    if isinstance(author, str) and author.strip() == "Hermes Agent":
        report.warnings.append("author should credit the human contributor first")
    metadata = frontmatter.get("metadata")
    hermes = metadata.get("hermes") if isinstance(metadata, dict) else None
    if not isinstance(hermes, dict) or not hermes.get("tags"):
        report.warnings.append("missing metadata.hermes.tags")


def _check_body(body: str, report: SkillReport) -> None:
    if not body.strip():
        report.errors.append("body after frontmatter is empty")


def _check_linked_paths(content: str, skill_dir: Path, report: SkillReport) -> None:
    for linked in sorted(set(LINKED_PATH.findall(content))):
        if not (skill_dir / linked).exists():
            report.errors.append(f"referenced file does not exist: {linked}")


def _check_machine_paths(content: str, report: SkillReport) -> None:
    matches = sorted(set(MACHINE_PATH.findall(content)))
    for match in matches:
        report.errors.append(f"machine-local path '{match}' breaks portability")


def discover_skill_dirs(root: Path) -> list[Path]:
    """Return skill directories under root (root itself if it is one skill).

    Frozen archive copies with the `.rollback-broken` suffix are skipped:
    they keep old frontmatter names that collide with active skills and are
    never loaded. Pass such a directory explicitly to validate it anyway.
    """
    if (root / "SKILL.md").is_file():
        return [root]
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.endswith(ARCHIVE_SUFFIX)
        and (child / "SKILL.md").is_file()
    )


def format_report(report: SkillReport) -> str:
    status = "OK  " if report.ok else "FAIL"
    lines = [f"{status} {report.path}"]
    lines += [f"  error: {item}" for item in report.errors]
    lines += [f"  warn:  {item}" for item in report.warnings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Hermes project skill directories (SKILL.md layout)."
    )
    parser.add_argument(
        "skill_dirs",
        nargs="*",
        type=Path,
        help="Skill directories to validate (each must contain SKILL.md).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Directory whose child skill dirs are all validated (e.g. .agents/skills).",
    )
    args = parser.parse_args(argv)

    targets: list[Path] = list(args.skill_dirs)
    if args.root is not None:
        if not args.root.is_dir():
            parser.error(f"--root is not a directory: {args.root}")
        targets.extend(discover_skill_dirs(args.root))
    if not targets:
        parser.error("pass at least one skill directory or --root")

    invalid = [t for t in targets if not t.is_dir()]
    if invalid:
        for path in invalid:
            print(f"FAIL {path}\n  error: not a directory", file=sys.stderr)
        return 1

    unique_targets = list(dict.fromkeys(targets))
    reports = [validate_skill_dir(t) for t in unique_targets]
    error_count = 0
    warning_count = 0
    for report in reports:
        print(format_report(report))
        error_count += len(report.errors)
        warning_count += len(report.warnings)
    print(
        f"\n{len(reports)} skill(s): {error_count} error(s), {warning_count} warning(s)"
    )
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
