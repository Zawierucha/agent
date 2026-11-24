"""Isolated CLI harness for GEO search-to-seal integration tests."""
from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
from click.testing import CliRunner
from tools.sc_dataset import geo_database, geo_envelope, geo_metadata, geo_search

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "20261123T120000Z-abcdef01"
SEARCH_URL, SUMMARY_URL = geo_search.ESEARCH_URL, geo_search.ESUMMARY_URL


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: Any = None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    stream_error_after: int | None = None
    def json(self) -> Any:
        if self.payload is None:
            raise ValueError("response has no JSON payload")
        return self.payload
    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)
    def iter_content(self, chunk_size: int = 65536):
        emitted = 0
        for offset in range(0, len(self.content), chunk_size):
            chunk = self.content[offset:offset + chunk_size]
            if self.stream_error_after is not None and emitted >= self.stream_error_after:
                import requests
                raise requests.ConnectionError("injected mid-stream abort")
            if self.stream_error_after is not None and emitted + len(chunk) > self.stream_error_after:
                chunk = chunk[: self.stream_error_after - emitted]
            if chunk:
                emitted += len(chunk)
                yield chunk
        if self.stream_error_after is not None:
            import requests
            raise requests.ConnectionError("injected mid-stream abort")
    def close(self) -> None:
        return None


class FakeNCBI:
    """Strict URL registry and response queues shared by both GEO tools."""
    def __init__(self) -> None:
        self.registry: dict[str, list[FakeResponse]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
    @staticmethod
    def key(url: str, params: dict[str, Any] | None = None) -> str:
        if params:
            return f"{url}|{json.dumps(params, sort_keys=True, separators=(",", ":"))}"
        return url
    def register(self, url: str, response: FakeResponse, params: dict[str, Any] | None = None) -> None:
        self.registry.setdefault(self.key(url, params), []).append(response)
    def request(self, url: str, **kwargs: Any) -> FakeResponse:
        key = self.key(url, kwargs.get("params"))
        self.calls.append((key, kwargs))
        queue = self.registry.get(key)
        if not queue:
            raise AssertionError(f"network guard: unregistered URL: {key}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def soft_gzip(gse_id: str, gsm_ids: tuple[str, ...] = ("GSM100001",)) -> bytes:
    lines = [f"^SERIES = {gse_id}", "!Series_title = Fixture series", "!Series_organism = Homo sapiens"]
    for index, gsm_id in enumerate(gsm_ids, 1):
        lines.extend([f"^SAMPLE = {gsm_id}", f"!Sample_title = Sample {index}", "!Sample_organism_ch1 = Homo sapiens", "!Sample_characteristics_ch1 = disease: Crohn", "!Sample_characteristics_ch1 = tissue: colon"])
    return gzip.compress(("\n".join(lines) + "\n").encode())


def _json_output(result: Any) -> Any:
    try:
        parsed = json.loads(result.output)
        return {"issues": parsed} if isinstance(parsed, list) else parsed
    except (json.JSONDecodeError, TypeError):
        pass
    exception = getattr(result, "exception", None)
    if exception is not None:
        issues = getattr(exception, "issues", None)
        return {"error": str(exception), "issues": [issue.__dict__ for issue in issues or []]}
    try:
        from click.exceptions import Exit
        if isinstance(exception, Exit):
            return {"error": "command exited with non-zero status", "exit_code": getattr(exception, "exit_code", result.exit_code)}
    except ImportError:
        pass
    for line in reversed(result.output.splitlines()):
        try: return json.loads(line)
        except (json.JSONDecodeError, TypeError): continue
    return {"output": result.output.strip()} if result.output.strip() else None


class PipelineHarness:
    def __init__(self, tmp_path: Path, monkeypatch: Any) -> None:
        self.root = tmp_path
        self.run_dir = tmp_path / f"run_{RUN_ID}"
        self.run_dir.mkdir()
        self.envelope = self.run_dir / "envelope.json"
        self.db = tmp_path / "catalog.duckdb"
        self.fake = FakeNCBI()
        self.runner = CliRunner()
        self.production_db = ROOT / "data" / "geo.duckdb"
        self.production_md5 = self._md5(self.production_db)
        monkeypatch.setattr(geo_search.requests.Session, "get", self._search_get)
        monkeypatch.setattr(geo_metadata.requests, "get", self._metadata_get)
        monkeypatch.setattr(geo_search.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(geo_metadata.time, "sleep", lambda _seconds: None)
        result = self.runner.invoke(geo_database.cli, ["--db", str(self.db), "init", "--schema", str(ROOT / "data" / "schema.sql")])
        if result.exit_code != 0:
            exception = repr(result.exception)
            raise AssertionError(result.output or exception or "CLI invocation failed without output or exception")

    @staticmethod
    def _md5(path: Path) -> str:
        return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else "MISSING"

    def assert_production_db_unchanged(self) -> None:
        assert self._md5(self.production_db) == self.production_md5

    def _search_get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.fake.request(url, **kwargs)

    def _metadata_get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.fake.request(url, **kwargs)

    def register_query(self, query: str, gse_ids: tuple[str, ...] = (), *, count: int | None = None, search_script: tuple[FakeResponse, ...] | None = None) -> None:
        params = {"db": "gds", "term": query, "retmax": 100, "retmode": "json"}
        if search_script is not None:
            for response in search_script:
                self.fake.register(SEARCH_URL, response, params)
            ids = [str(i) for i in range(len(gse_ids))]
            if ids:
                summary = {"result": {"uids": ids}}
                for record_id, gse_id in zip(ids, gse_ids, strict=True):
                    summary["result"][record_id] = {"accession": gse_id, "title": f"Fixture {gse_id}", "summary": "fixture", "taxon": "Homo sapiens", "gdstype": "Expression profiling by high throughput sequencing", "organism": "Homo sapiens", "gpl": "GPL1", "n_samples": 1}
                self.fake.register(SUMMARY_URL, FakeResponse(payload=summary), {"db": "gds", "id": ",".join(ids), "retmode": "json"})
            return
        payload = {"esearchresult": {"count": str(len(gse_ids) if count is None else count), "idlist": [str(i) for i in range(len(gse_ids))]}}
        self.fake.register(SEARCH_URL, FakeResponse(payload=payload), params)
        ids = [str(i) for i in range(len(gse_ids))]
        if ids:
            summary = {"result": {"uids": ids}}
            for record_id, gse_id in zip(ids, gse_ids, strict=True):
                summary["result"][record_id] = {"accession": gse_id, "title": f"Fixture {gse_id}", "summary": "fixture", "taxon": "Homo sapiens", "gdstype": "Expression profiling by high throughput sequencing", "organism": "Homo sapiens", "gpl": "GPL1", "n_samples": 1}
            self.fake.register(SUMMARY_URL, FakeResponse(payload=summary), {"db": "gds", "id": ",".join(ids), "retmode": "json"})

    def register_soft(self, gse_id: str, gsm_ids: tuple[str, ...] = ("GSM100001",), script: tuple[FakeResponse, ...] | None = None) -> None:
        for response in (script or (FakeResponse(content=soft_gzip(gse_id, gsm_ids)),)):
            self.fake.register(geo_metadata._get_soft_url(gse_id), response)

    def command(self, command: Any, args: list[str]) -> tuple[int, Any]:
        result = self.runner.invoke(command, args)
        return result.exit_code, _json_output(result)

    def run_init(self, queries: tuple[str, ...] = ("fixture query",), run_id: str = RUN_ID) -> tuple[int, Any]:
        args = ["init-run", str(self.envelope), "--run-id", run_id, "--skill-version", "0.5.0"]
        for query in queries:
            args.extend(["--query", query])
        return self.command(geo_envelope.cli, args)

    def run_search(self, queries: tuple[str, ...] = ("fixture query",), run_id: str = RUN_ID) -> tuple[int, Any]:
        args = ["--run-id", run_id, "--output-envelope", str(self.envelope)]
        for query in queries:
            args.extend(["--query", query])
        return self.command(geo_search.main, args)

    def run_handoff(self, transferred: list[str], excluded: list[dict[str, Any]] | None = None, *, allowed: str = "complete", acceptance: dict[str, str] | None = None) -> tuple[int, Any]:
        path = self.run_dir / "handoff.json"
        path.write_text(json.dumps({"transferred_gse_ids": transferred, "excluded": excluded or [], "allowed_to_proceed": allowed, "acceptance": acceptance, "created_at_utc": "2026-11-23T12:00:00Z"}), encoding="utf-8")
        return self.command(geo_envelope.cli, ["build-handoff", str(self.envelope), str(path)])

    def run_admit(self) -> tuple[int, Any]:
        return self.command(geo_envelope.cli, ["admit", str(self.envelope)])

    def run_fetch(self, gse_id: str, *, max_mb: int = 256) -> tuple[int, Any]:
        run_id = json.loads(self.envelope.read_text())["run_id"]
        geo_metadata._fetch_and_record.run_id = run_id
        return self.command(geo_metadata.main, [gse_id, "--run-dir", str(self.run_dir), "--run-id", run_id, "--max-mb", str(max_mb)])

    def run_recover(self, *gse_ids: str) -> tuple[int, Any]:
        args = [str(self.run_dir)]
        for gse_id in gse_ids:
            args.extend(["--gse", gse_id])
        return self.command(geo_metadata.recover, args)

    def run_preflight(self, gse_id: str) -> tuple[int, Any]:
        return self.command(geo_metadata.preflight_registration, [str(self.run_dir), gse_id, "--db", str(self.db)])

    def run_register(self, gse_id: str) -> tuple[int, Any]:
        artifact = json.loads((self.run_dir / "metadata_parsed" / f"{gse_id}.json").read_text())
        grant = str(self.run_dir / "preflight" / f"{gse_id}.grant.json")
        result_codes = []
        gse = artifact["gse"]
        result = self.runner.invoke(geo_database.cli, ["--db", str(self.db), "save-dataset", gse_id, "--preflight", grant, "--title", str(gse.get("title") or "Fixture"), "--organism", str(gse.get("organism") or "Homo sapiens"), "--n-samples", str(len(artifact["samples"]))])
        result_codes.append(result.exit_code)
        for sample in artifact["samples"]:
            chars = json.dumps(sample.get("characteristics", {}), separators=(",", ":"))
            result = self.runner.invoke(geo_database.cli, ["--db", str(self.db), "save-sample", sample["gsm_id"], "--preflight", grant, "--gse-id", gse_id, "--title", str(sample.get("title") or "Fixture sample"), "--organism", str(sample.get("organism") or "Homo sapiens"), "--characteristics", chars])
            result_codes.append(result.exit_code)
        return max(result_codes, default=0), {"write_exit_codes": result_codes}

    def run_verify(self, gse_id: str) -> tuple[int, Any]:
        return self.command(geo_metadata.verify_registration, [str(self.run_dir), gse_id, "--db", str(self.db)])

    def run_write_qual(self, qualification: dict[str, Any]) -> tuple[int, Any]:
        path = self.run_dir / "qualification.json"
        path.write_text(json.dumps(qualification), encoding="utf-8")
        skill = ROOT / ".agents" / "skills" / "sc-dataset-qualification" / "SKILL.md"
        return self.command(geo_envelope.cli, ["write-qualification", str(self.envelope), str(path), "--skill-md", str(skill)])

    def run_refuse(self, status: str = "BLOCKED_UPSTREAM", reason: str = "upstream failed") -> tuple[int, Any]:
        return self.command(geo_envelope.cli, ["refuse", str(self.envelope), "--stage", "qualification", "--status", status, "--reason", reason])

    def run_resume(self, *, query: str | None = None) -> tuple[int, Any]:
        env = json.loads(self.envelope.read_text())
        invocation = {"search": json.loads(json.dumps(env["search"]))}
        if query is not None:
            invocation["search"]["params"]["queries"][0]["query"] = query
        path = self.run_dir / "invocation.json"
        path.write_text(json.dumps(invocation))
        return self.command(geo_envelope.cli, ["resume", str(self.envelope), str(path)])

    def run_seal(self) -> tuple[int, Any]:
        return self.command(geo_envelope.cli, ["seal", str(self.envelope)])

    def run_derive(self) -> tuple[int, Any]:
        return self.command(geo_envelope.cli, ["derive", str(self.envelope)])

    def rows(self, table: str) -> list[tuple[Any, ...]]:
        columns = {"geo_datasets": "gse_id", "geo_samples": "gsm_id", "geo_sample_characteristics": "gsm_id, characteristic, value", "geo_sample_metadata": "gsm_id", "geo_qualification": "gse_id, qualification", "geo_sample_qualification": "gsm_id"}
        if table not in columns:
            raise ValueError(table)
        connection = duckdb.connect(str(self.db), read_only=True)
        try:
            return connection.execute(f"SELECT {columns[table]} FROM {table} ORDER BY 1").fetchall()
        finally:
            connection.close()


def make_qualification(transferred: list[str], *, include: list[str] | None = None, failed: list[str] | None = None, persistence: bool = False, report: str = "") -> dict[str, Any]:
    include, failed = include or [], failed or []
    processed = sorted(set(transferred) - set(failed))
    decisions = {"include": include, "exclude": [g for g in processed if g not in include], "uncertain": []}
    result: dict[str, Any] = {"skill": {"name": "sc-dataset-qualification", "version": "0.2.0"}, "status": "COMPLETE" if not failed else ("PARTIAL" if processed else "FAILED"), "criteria_profile": {"name": "fixture-profile"}, "processed_gse_ids": processed, "failed_gse_ids": failed, "skipped_gse_ids": [], "decisions": decisions, "sample_counts": {"include": len(include), "exclude": len(decisions["exclude"]), "uncertain": 0}, "errors": [{"gse_id": g, "detail": "fixture fetch failed"} for g in failed], "skipped_reasons": []}
    if persistence:
        result["persistence"] = {"media": "duckdb", "database": "temporary", "tables": ["geo_datasets", "geo_samples"], "verified_counts": {"datasets": len(transferred), "gsm": len(transferred)}, "report": report}
    return result


def full_chain(harness: PipelineHarness, *, queries: tuple[str, ...] = ("fixture query",), found: tuple[str, ...] = ("GSE123456",), transferred: list[str] | None = None, excluded: list[dict[str, Any]] | None = None, include: list[str] | None = None, failed_fetch: tuple[str, ...] = (), persistence: bool = True, allowed: str = "complete", acceptance: dict[str, str] | None = None, search_scripts: dict[str, tuple[FakeResponse, ...]] | None = None, hooks: dict[str, Callable[[PipelineHarness, dict[str, Any]], Any]] | None = None) -> dict[str, Any]:
    hooks, search_scripts = hooks or {}, search_scripts or {}
    found, moved, results = tuple(found), list(found if transferred is None else transferred), {}
    def execute(name: str, action: Callable[[], tuple[int, Any]]) -> tuple[int, Any] | None:
        hook = hooks.get(name)
        if hook:
            decision = hook(harness, results)
            if decision == "skip":
                results[name] = "SKIPPED"
                return None
            if isinstance(decision, tuple):
                results[name] = decision
                return decision
        results[name] = action()
        return results[name]
    execute("init", lambda: harness.run_init(queries))
    for index, query in enumerate(queries):
        harness.register_query(query, found if index == 0 else (), search_script=search_scripts.get(query))
    execute("search", lambda: harness.run_search(queries))
    search_status = json.loads(harness.envelope.read_text())["search"]["status"]
    if search_status == "FAILED":
        results["handoff"] = "SKIPPED: failed search remains unhanded"
    else:
        execute("handoff", lambda: harness.run_handoff(moved, excluded, allowed=allowed, acceptance=acceptance))
    execute("admit", harness.run_admit)
    if not moved:
        not_applicable = {"skill": {"name": "sc-dataset-qualification", "version": "0.2.0"}, "status": "NOT_APPLICABLE", "criteria_profile": {"name": "fixture-profile"}, "processed_gse_ids": [], "failed_gse_ids": [], "skipped_gse_ids": [], "decisions": {"include": [], "exclude": [], "uncertain": []}, "sample_counts": {"include": 0, "exclude": 0, "uncertain": 0}, "errors": [], "skipped_reasons": []}
        if json.loads(harness.envelope.read_text())["search"]["status"] == "FAILED":
            results["refuse"] = harness.command(
                geo_envelope.cli,
                ["refuse", str(harness.envelope), "--stage", "qualification", "--status", "BLOCKED_UPSTREAM", "--reason", "all planned queries failed"],
            )
            results["derive"] = harness.run_derive()
        else:
            execute("qualification", lambda: harness.run_write_qual(not_applicable))
        execute("seal", harness.run_seal)
        return results
    failed = list(failed_fetch)
    for gse in moved:
        if gse in failed:
            harness.register_soft(gse, script=tuple(FakeResponse(status_code=503) for _ in range(4)))
        else:
            harness.register_soft(gse)
    for gse in moved:
        fetched = execute(f"fetch:{gse}", lambda gse=gse: harness.run_fetch(gse))
        if fetched and fetched[0] != 0 and gse not in failed:
            failed.append(gse)
    execute("qualification", lambda: harness.run_write_qual(make_qualification(moved, include=include, failed=failed, persistence=persistence and not failed, report=str(harness.run_dir / "metadata_fetch.jsonl"))))
    for gse in moved:
        if gse in failed:
            continue
        execute(f"preflight:{gse}", lambda gse=gse: harness.run_preflight(gse))
        execute(f"register:{gse}", lambda gse=gse: harness.run_register(gse))
        execute(f"verify:{gse}", lambda gse=gse: harness.run_verify(gse))
    if include:
        for gse in include:
            artifact = json.loads((harness.run_dir / "metadata_parsed" / f"{gse}.json").read_text())
            sample_results = [{"gsm_id": s["gsm_id"], "decision": "include", "evidence": "fixture"} for s in artifact["samples"]]
            path = harness.run_dir / f"{gse}-qual.json"
            path.write_text(json.dumps(sample_results))
            result = harness.runner.invoke(geo_database.cli, ["--db", str(harness.db), "save-qualification", gse, "--qualification", "include", "--is-human", "true", "--is-single-cell", "true", "--is-ibd-related", "true", "--sample-results-file", str(path)])
            results[f"db_qualification:{gse}"] = (result.exit_code, _json_output(result))
    execute("seal", harness.run_seal)
    return results
