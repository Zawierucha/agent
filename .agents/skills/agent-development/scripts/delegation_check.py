from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

ALLOWED_TOP_LEVEL = {"tasks", "action", "subagent_id", "message"}
SHORTHAND_KEYS = {"goal", "context", "output_schema", "images"}
ALLOWED_ACTIONS = {"spawn", "list", "steer", "stop", "continue"}
MODEL_PATTERN = re.compile(r"Model:\s*([A-Za-z0-9._/-]+)")


class CheckArgumentParser(argparse.ArgumentParser):
    """Use the delegation checker error prefix and failure status for CLI errors."""

    def error(self, message: str) -> None:
        print(f"error: {message}", file=sys.stderr)
        self.exit(1)


DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent.parent / "templates" / "delegation_manifest.json"
)


def _error(message: str) -> None:
    print(f"error: {message}")


def _warning(message: str) -> None:
    print(f"warn: {message}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read JSON file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object in {path}")
    return value


def _manifest_path(argument: str | None) -> Path:
    return Path(argument) if argument is not None else DEFAULT_MANIFEST


def _preflight(arguments: argparse.Namespace) -> int:
    payload_path = arguments.payload
    try:
        if payload_path == "-":
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("payload must be one JSON object")
        else:
            payload = _read_json(Path(payload_path))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _error(str(exc))
        return 1

    errors = 0
    if "option" in payload:
        _error("top-level 'option' is invalid; put option inside each tasks[] entry")
        errors += 1
    shorthand = sorted(SHORTHAND_KEYS.intersection(payload))
    if shorthand:
        _error(
            f"top-level {', '.join(shorthand)} uses shorthand dispatch and silently routes to the default; wrap in tasks[]"
        )
        errors += 1
    unknown = sorted(set(payload).difference(ALLOWED_TOP_LEVEL))
    if unknown:
        _error(f"unknown top-level key(s): {', '.join(unknown)}")
        errors += 1

    action = payload.get("action")
    if action is not None and (
        not isinstance(action, str) or action not in ALLOWED_ACTIONS
    ):
        _error(
            f"invalid action {action!r}; expected one of: {', '.join(sorted(ALLOWED_ACTIONS))}"
        )
        errors += 1
    if isinstance(action, str) and action in {"steer", "stop", "continue"}:
        if not payload.get("subagent_id"):
            _error(f"action={action!r} requires subagent_id")
            errors += 1
        _warning(
            f"action={action!r} ignores tasks/option; the child keeps its original route"
        )
    elif action == "spawn" or "action" not in payload:
        if "tasks" not in payload:
            _error("spawn requires a non-empty tasks list")
            errors += 1
        elif not isinstance(payload["tasks"], list) or not payload["tasks"]:
            _error("tasks must be a non-empty list of objects")
            errors += 1
        else:
            tasks = payload["tasks"]
            for index, task in enumerate(tasks):
                if not isinstance(task, dict):
                    _error(f"tasks[{index}] must be an object")
                    errors += 1
                elif not isinstance(task.get("goal"), str) or not task["goal"].strip():
                    _error(f"tasks[{index}].goal must be a non-empty string")
                    errors += 1
            option_indices = [
                index
                for index, task in enumerate(tasks)
                if isinstance(task, dict) and "option" in task
            ]
            if option_indices:
                missing_indices = [
                    index
                    for index, task in enumerate(tasks)
                    if not isinstance(task, dict) or "option" not in task
                ]
                if missing_indices:
                    _error(
                        "option must be present on every task when used; missing at indices: "
                        + ", ".join(map(str, missing_indices))
                    )
                    errors += 1
                options = {
                    item.strip()
                    for item in arguments.options.split(",")
                    if item.strip()
                }
                for index in option_indices:
                    option = tasks[index]["option"]
                    if not isinstance(option, str) or option not in options:
                        _error(
                            f"tasks[{index}].option {option!r} is not in --options: "
                            + (", ".join(sorted(options)) or "(none)")
                        )
                        errors += 1
                distinct_options = sorted(
                    {
                        tasks[index]["option"]
                        for index in option_indices
                        if isinstance(tasks[index]["option"], str)
                    }
                )
                if len(distinct_options) > 1:
                    _error(
                        "one route per delegate_task call; tasks mix options: "
                        + ", ".join(distinct_options)
                    )
                    errors += 1
    return 1 if errors else 0


def _load_expected_model(route: str, manifest_argument: str | None) -> str:
    path = _manifest_path(manifest_argument)
    try:
        manifest = _read_json(path)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    routes = manifest.get("routes")
    route_data = routes.get(route) if isinstance(routes, dict) else None
    model = route_data.get("model") if isinstance(route_data, dict) else None
    if not isinstance(model, str) or not model:
        raise ValueError(
            f"route {route!r} has no model in {path}; record the route in the manifest "
            "or pass --expected-model"
        )
    return model


def _postcheck(arguments: argparse.Namespace) -> int:
    texts = list(arguments.text or [])
    for filename in arguments.file or []:
        try:
            texts.append(Path(filename).read_text(encoding="utf-8"))
        except OSError as exc:
            _error(f"cannot read text file {filename}: {exc}")
            return 1
    if not texts:
        _error("postcheck requires at least one --text or --file input")
        return 1

    expected = arguments.expected_model
    if expected is None:
        try:
            expected = _load_expected_model(arguments.route, arguments.manifest)
        except (TypeError, ValueError) as exc:
            _error(str(exc))
            return 1

    observed = set(MODEL_PATTERN.findall("\n".join(texts)))
    if not observed:
        _error("runtime Model: metadata not found")
        return 1
    if observed != {expected}:
        _error(
            f"expected model {expected!r}; observed model(s): "
            + ", ".join(sorted(observed))
        )
        return 1
    return 0


def _manifest(arguments: argparse.Namespace) -> int:
    path = _manifest_path(arguments.file)
    if not path.is_file():
        _error(f"manifest file does not exist: {path}")
        return 1
    try:
        manifest = _read_json(path)
    except (TypeError, ValueError) as exc:
        _error(str(exc))
        return 1

    if arguments.manifest_action == "show":
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0

    routes = manifest.get("routes")
    if not isinstance(routes, dict):
        _error(f"manifest {path} must contain a routes object")
        return 1
    routes[arguments.route] = {
        "model": arguments.model,
        "verified_at": date.today().isoformat(),
        "evidence": arguments.evidence or "",
    }
    if not isinstance(manifest.get("_comment"), str):
        manifest["_comment"] = (
            "Route -> last runtime-verified model mapping for delegate_task."
        )
    try:
        path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _error(f"cannot write manifest {path}: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for delegation contract checks."""
    parser = CheckArgumentParser(
        description="Validate delegate_task payloads and completion model metadata."
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=CheckArgumentParser
    )

    preflight = subparsers.add_parser(
        "preflight", help="validate one delegate_task arguments JSON object"
    )
    preflight.add_argument(
        "--options", default="developer", help="comma-separated configured option names"
    )
    preflight.add_argument(
        "--payload",
        required=True,
        metavar="PATH_OR_DASH",
        help="JSON file path or - for stdin",
    )
    preflight.set_defaults(handler=_preflight)

    postcheck = subparsers.add_parser(
        "postcheck", help="verify runtime Model metadata against a route expectation"
    )
    postcheck.add_argument("--route", required=True, help="delegation route name")
    postcheck.add_argument(
        "--expected-model", help="explicit expected runtime model ID"
    )
    postcheck.add_argument("--manifest", help="manifest path override")
    postcheck.add_argument(
        "--text", action="append", help="notification text; may be repeated"
    )
    postcheck.add_argument(
        "--file", action="append", help="notification text file; may be repeated"
    )
    postcheck.set_defaults(handler=_postcheck)

    manifest = subparsers.add_parser(
        "manifest", help="show or record verified route models"
    )
    manifest.add_argument("--file", help="manifest file override")
    manifest_actions = manifest.add_subparsers(
        dest="manifest_action", required=True, parser_class=CheckArgumentParser
    )
    manifest_actions.add_parser("show", help="print the manifest as pretty JSON")
    record = manifest_actions.add_parser(
        "record", help="record a runtime-verified route model"
    )
    record.add_argument("--route", required=True, help="route name")
    record.add_argument("--model", required=True, help="verified model ID")
    record.add_argument(
        "--evidence", default="", help="runtime evidence for this mapping"
    )
    manifest.set_defaults(handler=_manifest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected delegation check and return its process status."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
