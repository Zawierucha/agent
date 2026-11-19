from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "agent-development"
    / "scripts"
    / "delegation_check.py"
)
SPEC = importlib.util.spec_from_file_location("delegation_check", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

GOOD = "Role: leaf   Model: openai/gpt-6-luna   Total duration: 314.73s"
BAD = "Role: leaf   Model: qwen/qwen3.8-flash   Total duration: 354.98s"
PYTHON = sys.executable


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(SCRIPT_PATH), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_preflight_accepts_options_inside_each_task() -> None:
    payload = {
        "tasks": [
            {
                "goal": "g",
                "context": "c",
                "option": "developer",
                "output_schema": {"type": "object"},
            }
        ]
    }
    result = subprocess.run(
        [PYTHON, str(SCRIPT_PATH), "preflight", "--payload", "-"],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "error:" not in result.stdout


def test_top_level_option_and_shorthand_are_reported(tmp_path: Path) -> None:
    payload = write_json(
        tmp_path / "payload.json", {"option": "developer", "goal": "g"}
    )
    result = run_cli("preflight", "--payload", str(payload))
    assert result.returncode == 1
    assert "error:" in result.stdout
    assert "top-level 'option'" in result.stdout
    assert "shorthand" in result.stdout


def test_preflight_reports_option_missing_task_indices(tmp_path: Path) -> None:
    payload = write_json(
        tmp_path / "payload.json",
        {"tasks": [{"goal": "one", "option": "developer"}, {"goal": "two"}]},
    )
    result = run_cli("preflight", "--payload", str(payload))
    assert result.returncode == 1
    assert "indices" in result.stdout and "1" in result.stdout


def test_preflight_rejects_unconfigured_option(tmp_path: Path) -> None:
    payload = write_json(
        tmp_path / "payload.json",
        {"tasks": [{"goal": "g", "option": "fast"}]},
    )
    result = run_cli("preflight", "--payload", str(payload), "--options", "developer")
    assert result.returncode == 1
    assert "fast" in result.stdout and "developer" in result.stdout


def test_preflight_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    payload = write_json(
        tmp_path / "payload.json", {"tasks": [{"goal": "g"}], "wat": 1}
    )
    result = run_cli("preflight", "--payload", str(payload))
    assert result.returncode == 1
    assert "wat" in result.stdout and "error:" in result.stdout


def test_continue_without_subagent_id_fails(tmp_path: Path) -> None:
    payload = write_json(tmp_path / "payload.json", {"action": "continue"})
    result = run_cli("preflight", "--payload", str(payload))
    assert result.returncode == 1
    assert "subagent_id" in result.stdout


def test_continue_with_subagent_id_warns_without_failing(tmp_path: Path) -> None:
    payload = write_json(
        tmp_path / "payload.json", {"action": "continue", "subagent_id": "child-1"}
    )
    result = run_cli("preflight", "--payload", str(payload))
    assert result.returncode == 0
    assert "warn:" in result.stdout
    assert "original route" in result.stdout


def test_list_does_not_require_tasks(tmp_path: Path) -> None:
    payload = write_json(tmp_path / "payload.json", {"action": "list"})
    result = run_cli("preflight", "--payload", str(payload))
    assert result.returncode == 0


def test_incident_wrong_route_fails_and_good_route_passes() -> None:
    bad = run_cli(
        "postcheck",
        "--route",
        "developer",
        "--expected-model",
        "openai/gpt-6-luna",
        "--text",
        BAD,
    )
    good = run_cli(
        "postcheck",
        "--route",
        "developer",
        "--expected-model",
        "openai/gpt-6-luna",
        "--text",
        GOOD,
    )
    assert bad.returncode == 1 and "qwen/qwen3.8-flash" in bad.stdout
    assert good.returncode == 0


def test_postcheck_fails_without_model_metadata() -> None:
    result = run_cli(
        "postcheck",
        "--route",
        "developer",
        "--expected-model",
        "openai/gpt-6-luna",
        "--text",
        "Role: leaf",
    )
    assert result.returncode == 1
    assert "runtime Model: metadata not found" in result.stdout


def test_postcheck_missing_manifest_route_is_actionable() -> None:
    result = run_cli("postcheck", "--route", "unknown-route", "--text", GOOD)
    assert result.returncode == 1
    assert "record" in result.stdout and "--expected-model" in result.stdout


def test_postcheck_expected_model_overrides_manifest() -> None:
    result = run_cli(
        "postcheck",
        "--route",
        "unknown-route",
        "--expected-model",
        "openai/gpt-6-luna",
        "--text",
        GOOD,
    )
    assert result.returncode == 0


def test_postcheck_checks_every_model_in_batch() -> None:
    result = run_cli(
        "postcheck",
        "--route",
        "developer",
        "--expected-model",
        "openai/gpt-6-luna",
        "--text",
        f"{GOOD}\n{BAD}",
    )
    assert result.returncode == 1
    assert "openai/gpt-6-luna" in result.stdout
    assert "qwen/qwen3.8-flash" in result.stdout


def test_manifest_record_and_show_round_trip(tmp_path: Path) -> None:
    manifest = write_json(
        tmp_path / "manifest.json",
        {"_comment": "test manifest", "routes": {}},
    )
    recorded = run_cli(
        "manifest",
        "--file",
        str(manifest),
        "record",
        "--route",
        "developer",
        "--model",
        "openai/gpt-6-luna",
        "--evidence",
        GOOD,
    )
    shown = run_cli("manifest", "--file", str(manifest), "show")
    assert recorded.returncode == 0
    assert shown.returncode == 0
    data = json.loads(shown.stdout)
    assert data["routes"]["developer"]["model"] == "openai/gpt-6-luna"
    assert data["routes"]["developer"]["evidence"] == GOOD
    assert data["routes"]["developer"]["verified_at"]


def test_missing_manifest_file_reports_its_path(tmp_path: Path) -> None:
    manifest = tmp_path / "missing.json"
    result = run_cli("manifest", "--file", str(manifest), "show")
    assert result.returncode == 1
    assert str(manifest) in result.stdout


def test_preflight_and_postcheck_read_files(tmp_path: Path) -> None:
    payload = write_json(tmp_path / "payload.json", {"tasks": [{"goal": "g"}]})
    notification = tmp_path / "notification.txt"
    notification.write_text(GOOD, encoding="utf-8")
    preflight = run_cli("preflight", "--payload", str(payload))
    postcheck = run_cli(
        "postcheck",
        "--route",
        "developer",
        "--expected-model",
        "openai/gpt-6-luna",
        "--file",
        str(notification),
    )
    assert preflight.returncode == 0
    assert postcheck.returncode == 0


def test_preflight_rejects_mixed_routes_in_one_call(tmp_path: Path) -> None:
    payload = write_json(
        tmp_path / "payload.json",
        {
            "tasks": [
                {"goal": "a", "option": "developer"},
                {"goal": "b", "option": "reviewer"},
            ]
        },
    )
    result = run_cli(
        "preflight",
        "--payload",
        str(payload),
        "--options",
        "developer,reviewer",
    )
    assert result.returncode == 1
    assert "one route per delegate_task call" in result.stdout


def test_preflight_accepts_uniform_parallel_fan_out(tmp_path: Path) -> None:
    payload = write_json(
        tmp_path / "payload.json",
        {
            "tasks": [
                {"goal": "a", "option": "developer"},
                {"goal": "b", "option": "developer"},
            ]
        },
    )
    result = run_cli("preflight", "--payload", str(payload))
    assert result.returncode == 0, result.stdout + result.stderr
