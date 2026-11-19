"""Tests for the agent-development skill validator and skill self-check.

Run: uv run pytest tests/test_agent_development_validate_skill.py -q
The validator is loaded by file path because skill scripts live outside the
importable `tools` package.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
VALIDATOR_PATH = SKILLS_ROOT / "agent-development" / "scripts" / "validate_skill.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "agent_development_validate_skill", VALIDATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validate = _load_validator()


def _write_skill(
    root: Path,
    name: str = "demo-skill",
    description: str = "Demo skill for validator tests.",
    body: str = "# Demo\n\nSteps and checks.\n",
    frontmatter_extra: str = "version: 0.1.0\n",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f"name: {name}\ndescription: {description}\n{frontmatter_extra}"
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}---\n\n{body}", encoding="utf-8"
    )
    return skill_dir


def test_valid_skill_has_no_errors(tmp_path: Path) -> None:
    report = validate.validate_skill_dir(_write_skill(tmp_path))
    assert report.ok


def test_missing_skill_md_fails(tmp_path: Path) -> None:
    report = validate.validate_skill_dir(tmp_path / "absent")
    assert not report.ok
    assert any("missing SKILL.md" in error for error in report.errors)


def test_frontmatter_delimiters_required(tmp_path: Path) -> None:
    skill_dir = tmp_path / "broken-delims"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    report = validate.validate_skill_dir(skill_dir)
    assert not report.ok


def test_non_mapping_frontmatter_fails(tmp_path: Path) -> None:
    skill_dir = tmp_path / "list-frontmatter"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n- not: a mapping\n---\n\nbody\n", encoding="utf-8"
    )
    report = validate.validate_skill_dir(skill_dir)
    assert not report.ok
    assert any("mapping" in error for error in report.errors)


def test_invalid_name_pattern_fails(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, name="Bad_Name")
    # directory is `Bad_Name` and frontmatter matches, but the pattern check fires
    report = validate.validate_skill_dir(skill_dir)
    assert not report.ok
    assert any("lowercase-hyphen" in error for error in report.errors)


def test_name_directory_mismatch_warns_only(tmp_path: Path) -> None:
    skill_dir = tmp_path / "dir-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: Fine.\n---\n\nbody\n",
        encoding="utf-8",
    )
    report = validate.validate_skill_dir(skill_dir)
    assert report.ok
    assert any("does not match directory" in w for w in report.warnings)


def test_empty_description_fails(tmp_path: Path) -> None:
    report = validate.validate_skill_dir(_write_skill(tmp_path, description=""))
    assert not report.ok


def test_overlong_description_fails(tmp_path: Path) -> None:
    report = validate.validate_skill_dir(_write_skill(tmp_path, description="x" * 1025))
    assert not report.ok


def test_soft_description_length_warns_without_failing(tmp_path: Path) -> None:
    report = validate.validate_skill_dir(_write_skill(tmp_path, description="d" * 89))
    assert report.ok
    assert any("convention is" in w for w in report.warnings)


def test_empty_body_fails(tmp_path: Path) -> None:
    report = validate.validate_skill_dir(_write_skill(tmp_path, body="\n"))
    assert not report.ok


def test_broken_linked_file_fails(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, body="# Demo\n\nSee `references/missing.md`.\n")
    report = validate.validate_skill_dir(skill_dir)
    assert not report.ok
    assert any("references/missing.md" in error for error in report.errors)


def test_existing_linked_file_passes(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, body="# Demo\n\nSee `references/guide.md`.\n")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.md").write_text("Guide.\n", encoding="utf-8")
    assert validate.validate_skill_dir(skill_dir).ok


def test_machine_local_path_fails(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, body="# Demo\n\nConfig at /home/piotr/x.yaml\n")
    report = validate.validate_skill_dir(skill_dir)
    assert not report.ok
    assert any("machine-local" in error for error in report.errors)


def test_discovery_skips_rollback_archive_dirs(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="live-skill")
    _write_skill(tmp_path / "live-skill.rollback-broken", name="live-skill")
    found = validate.discover_skill_dirs(tmp_path)
    assert [p.name for p in found] == ["live-skill"]


def test_cli_exits_zero_for_repo_skills() -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), "--root", str(SKILLS_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "agent-development" in result.stdout


def test_cli_exits_one_for_broken_skill(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path)
    (skill_dir / "SKILL.md").write_text("garbage", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), str(skill_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1


@pytest.mark.parametrize(
    "template",
    ["tool_template.py", "test_template.py"],
)
def test_skill_templates_parse_as_python(template: str) -> None:
    source = (SKILLS_ROOT / "agent-development" / "templates" / template).read_text(
        encoding="utf-8"
    )
    ast.parse(source)
