"""Deterministic validation for the public technical work-sample repository."""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import httpx


ROOT = Path(__file__).resolve().parents[1]
RESULT_REL = "validation/validation-summary.json"
PASS_TERMINAL = "PASS_PORT0003_PUBLIC_TECHNICAL_CANDIDATE"
N8N_STATUS = "N8N_RUNTIME_EXECUTION_DEFERRED"
EXERCISE_BOUNDARY = (
    "SYNTHETIC_LOCAL_EXERCISE_ONLY; NO_REAL_CUSTOMER_OR_PRODUCTION_OUTCOME"
)
MAPPING_BOUNDARY = (
    "SYNTHETIC_LOCAL_EXECUTION_ONLY; NO_REAL_CLIENT_OR_PRODUCTION_OUTCOME"
)
EVIDENCE_SHA256 = "399E3FAF6C5AEE97920142457AC677AA2823AC7DAAF689A4D3297FC6243C6AEF"
TRACE_SHA256 = "12B4367F65AC15B80AD98CC035A2A93FE6B47C67943D91B12C7997A3480601A3"

EXPECTED_FILES = (
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "CASE_STUDY.md",
    "LIMITATIONS.md",
    "README.md",
    "dependency-lock.txt",
    "docs/ARCHITECTURE.md",
    "docs/SUPPORT_RUNBOOK.md",
    "evidence/execution-and-reconciliation.json",
    "evidence/structured-execution-trace.jsonl",
    "pyproject.toml",
    "02_Source_System/source_api.py",
    "02_Source_System/source_openapi.json",
    "03_Target_System/target_api.py",
    "03_Target_System/target_openapi.json",
    "04_Integration_Workflow/integration_state_api.py",
    "04_Integration_Workflow/n8n_entitlement_sync_workflow.draft.json",
    "04_Integration_Workflow/reference_orchestrator.py",
    "05_Data_Mapping_and_Contracts/PORT0003_Mapping_and_Reliability_Rules.csv",
    "06_Test_Fixtures/PORT0003_12_Scenario_Test_Matrix.json",
    "07_Tests/test_port0003_end_to_end.py",
    "validation/validate_public_candidate.py",
    RESULT_REL,
)

SCENARIOS = (
    "VALID_CREATE",
    "VALID_TIER_UPDATE",
    "VALID_SUSPENSION",
    "DUPLICATE_EVENT",
    "OUT_OF_ORDER_EVENT",
    "MISSING_REQUIRED_FIELD",
    "INVALID_ENUM_VALUE",
    "TRANSIENT_429_RECOVERY",
    "TRANSIENT_503_EXHAUSTION",
    "AUTHENTICATION_FAILURE",
    "RECONCILIATION_MISMATCH",
    "DEAD_LETTER_REPLAY",
)
TEST_NAMES = tuple(
    f"test_{index:02d}_{name.lower()}" for index, name in enumerate(SCENARIOS, 1)
)
ATTEMPT_VECTOR = (1, 1, 1, 0, 0, 0, 0, 2, 3, 0, 0, 1)
MAPPING_HEADERS = (
    "Rule_ID",
    "Rule_Family",
    "Precedence",
    "Source_Field",
    "Source_Condition",
    "Target_Field",
    "Target_Value_or_Disposition",
    "Retryable",
    "Target_HTTP_Attempt_Cap",
    "Evidence_Requirement",
    "Example",
    "Exercise_Boundary",
)
LOCKS = {
    "fastapi": "0.139.2",
    "httpx": "0.28.1",
    "pydantic": "2.13.4",
    "pytest": "9.1.1",
    "setuptools": "80.9.0",
    "uvicorn": "0.51.0",
}


class ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Gate:
    number: int
    name: str
    function: Callable[[], dict[str, Any]]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def read_text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def read_json(relative: str) -> Any:
    return json.loads(read_text(relative))


def visible_files() -> set[str]:
    result: set[str] = set()
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(".git/"):
            continue
        result.add(relative)
    return result


def load_module(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    require(spec is not None and spec.loader is not None, f"Cannot import {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def openapi_schema_fields(document: dict[str, Any], schema_name: str) -> set[str]:
    schema = document["components"]["schemas"][schema_name]
    properties = schema["properties"]
    require(set(schema["required"]) == set(properties), f"OpenAPI required fields drift: {schema_name}")
    return set(properties)


def openapi_schema_signature(document: dict[str, Any], schema_name: str) -> dict[str, Any]:
    schema = document["components"]["schemas"][schema_name]
    keys = (
        "type",
        "format",
        "pattern",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
    )
    return {
        "additionalProperties": schema.get("additionalProperties"),
        "required": sorted(schema["required"]),
        "properties": {
            name: {key: definition[key] for key in keys if key in definition}
            for name, definition in sorted(schema["properties"].items())
        },
    }


def openapi_operations(document: dict[str, Any]) -> set[tuple[str, str]]:
    methods = {"get", "put", "post", "delete", "patch", "options", "head"}
    return {
        (path, method)
        for path, item in document["paths"].items()
        for method in item
        if method.lower() in methods
    }


def topology_gate() -> dict[str, Any]:
    actual = visible_files()
    expected = set(EXPECTED_FILES)
    result_missing = RESULT_REL not in actual
    if result_missing:
        expected.remove(RESULT_REL)
    require(actual == expected, f"Public candidate topology mismatch: {sorted(actual ^ expected)}")
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            raise ValidationError(f"Symlink is not allowed: {path.relative_to(ROOT)}")
    return {"controlled_files": 24, "validation_result": "GENERATED_OR_VERIFIED"}


def structured_gate() -> dict[str, Any]:
    json_paths = (
        "02_Source_System/source_openapi.json",
        "03_Target_System/target_openapi.json",
        "04_Integration_Workflow/n8n_entitlement_sync_workflow.draft.json",
        "06_Test_Fixtures/PORT0003_12_Scenario_Test_Matrix.json",
        "evidence/execution-and-reconciliation.json",
    )
    for relative in json_paths:
        read_json(relative)
    trace_rows = [json.loads(line) for line in read_text("evidence/structured-execution-trace.jsonl").splitlines()]
    require(len(trace_rows) == 48, "Structured trace must contain exactly 48 records")
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    require(project["project"]["requires-python"] == "==3.11.*", "Python authority drift")
    return {"json": len(json_paths), "jsonl_records": len(trace_rows), "toml": 1}


def dependency_gate() -> dict[str, Any]:
    found: dict[str, str] = {}
    for line in read_text("dependency-lock.txt").splitlines():
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        found[name.lower()] = version
    for name, version in LOCKS.items():
        require(found.get(name.lower()) == version, f"Dependency pin mismatch: {name}")
    require(len(found) == 23, f"Expected 23 locked packages, got {len(found)}")
    return {"locked_packages": len(found), "required_runtime_pins": LOCKS}


async def service_probe() -> dict[str, Any]:
    os.environ["PORT0003_LOCAL_API_KEY"] = "public-validator-placeholder"
    source_module = load_module("public_source_api", "02_Source_System/source_api.py")
    target_module = load_module("public_target_api", "03_Target_System/target_api.py")
    state_module = load_module(
        "public_state_api", "04_Integration_Workflow/integration_state_api.py"
    )
    source = source_module.create_app()
    target = target_module.create_app()
    state = state_module.create_app()
    source_contract = read_json("02_Source_System/source_openapi.json")
    target_contract = read_json("03_Target_System/target_openapi.json")
    expected_operations = {
        "source": {
            ("/health", "get"),
            ("/source/accounts", "get"),
            ("/source/accounts/apply", "post"),
            ("/source/accounts/{account_id}", "get"),
        },
        "target": {
            ("/health", "get"),
            ("/target/entitlements", "get"),
            ("/target/entitlements/{account_id}", "get"),
            ("/target/entitlements/{account_id}", "put"),
        },
    }
    for label, static, runtime, schema_name in (
        ("source", source_contract, source.openapi(), "SourceEvent"),
        ("target", target_contract, target.openapi(), "TargetEntitlement"),
    ):
        require(str(static["openapi"]).startswith("3."), f"{label} OpenAPI version drift")
        require(
            openapi_schema_fields(static, schema_name)
            == openapi_schema_fields(runtime, schema_name),
            f"{label} OpenAPI field parity drift",
        )
        require(
            openapi_schema_signature(static, schema_name)
            == openapi_schema_signature(runtime, schema_name),
            f"{label} OpenAPI constraint parity drift",
        )
        documented = openapi_operations(static)
        executable = openapi_operations(runtime)
        require(documented == expected_operations[label], f"{label} documented operation drift")
        require(documented <= executable, f"{label} documented operation is not executable")
        require(
            all("/test/" in path for path, _ in executable - documented),
            f"{label} has an undocumented non-test operation",
        )
    outcomes: dict[str, int] = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=source), base_url="http://source.local"
    ) as client:
        outcomes["source_health"] = (await client.get("/health")).status_code
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=target), base_url="http://target.local"
    ) as client:
        outcomes["target_without_key"] = (await client.post("/target/test/reset")).status_code
        outcomes["target_with_key"] = (
            await client.post(
                "/target/test/reset",
                headers={"X-PORT0003-API-Key": "public-validator-placeholder"},
            )
        ).status_code
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state), base_url="http://state.local"
    ) as client:
        outcomes["state_without_key"] = (await client.get("/state/evidence")).status_code
        outcomes["state_with_key"] = (
            await client.get(
                "/state/evidence",
                headers={"X-PORT0003-API-Key": "public-validator-placeholder"},
            )
        ).status_code
    for app in (source, target, state):
        app.state.connection.close()
    require(
        outcomes
        == {
            "source_health": 200,
            "target_without_key": 401,
            "target_with_key": 200,
            "state_without_key": 401,
            "state_with_key": 200,
        },
        f"Service probe mismatch: {outcomes}",
    )
    return outcomes


def service_gate() -> dict[str, Any]:
    return asyncio.run(service_probe())


def mapping_gate() -> dict[str, Any]:
    with (ROOT / "05_Data_Mapping_and_Contracts/PORT0003_Mapping_and_Reliability_Rules.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        reader = csv.DictReader(stream)
        require(tuple(reader.fieldnames or ()) == MAPPING_HEADERS, "Mapping headers drift")
        rows = list(reader)
    require(len(rows) == 28, f"Expected 28 mapping rows, got {len(rows)}")
    require(
        [row["Rule_ID"] for row in rows] == [f"MAP-{index:03d}" for index in range(1, 29)],
        "Mapping identifiers/order drift",
    )
    require({row["Exercise_Boundary"] for row in rows} == {MAPPING_BOUNDARY}, "Mapping boundary drift")
    required_outcomes = {
        "REJECTED_UNAUTHORIZED",
        "REJECTED_MISSING_REQUIRED_FIELD",
        "REJECTED_INVALID_ENUM",
        "DUPLICATE_IGNORED",
        "OUT_OF_ORDER_IGNORED",
        "RESOLVED_AFTER_REPLAY",
        "MATCH|MISSING_TARGET|STALE_TARGET|FIELD_MISMATCH|UNEXPECTED_TARGET",
    }
    require(
        required_outcomes <= {row["Target_Value_or_Disposition"] for row in rows},
        "Required reliability dispositions are missing",
    )
    formula_like = [
        value
        for row in rows
        for value in row.values()
        if re.match(r"^[=+\-@]", value or "")
    ]
    require(not formula_like, "Formula-like CSV cell detected")
    return {"rows": 28, "columns": 12, "formula_like_cells": 0}


def fixture_and_test_gate() -> dict[str, Any]:
    fixture = read_json("06_Test_Fixtures/PORT0003_12_Scenario_Test_Matrix.json")
    require(fixture["exercise_boundary"] == EXERCISE_BOUNDARY, "Fixture boundary drift")
    scenarios = fixture["scenarios"]
    require(tuple(row["name"] for row in scenarios) == SCENARIOS, "Scenario names/order drift")
    require(tuple(row["sequence"] for row in scenarios) == tuple(range(1, 13)), "Scenario sequence drift")
    tree = ast.parse(read_text("07_Tests/test_port0003_end_to_end.py"))
    tests = tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    )
    require(tests == TEST_NAMES, f"Pytest topology mismatch: {tests}")
    mismatch = scenarios[10]
    require(
        mismatch["expected_transformation"]["action"] == "REPAIR_FROM_LATEST_VALID_SOURCE"
        and mismatch["expected_attempt_count"] == 0
        and mismatch["expected_target_state"]["source_version"] == 1
        and mismatch["expected_target_state"]["last_event_id"] == "EVENT-0007",
        "Mismatch scenario no longer describes the test-only latest-source repair",
    )
    return {"scenarios": 12, "test_functions": 12, "mismatch_repair_attempts": 0}


def run_pytest_gate() -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    require(completed.returncode == 0, f"Pytest failed:\n{completed.stdout}")
    require("12 passed" in completed.stdout, f"Pytest count drift:\n{completed.stdout}")
    return {"collected": 12, "passed": 12, "failed": 0, "skipped": 0}


def orchestrator_run(directory: Path) -> tuple[bytes, bytes]:
    evidence_path = directory / "evidence.json"
    trace_path = directory / "trace.jsonl"
    runtime = directory / "runtime"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "04_Integration_Workflow/reference_orchestrator.py"),
            "--fixtures",
            str(ROOT / "06_Test_Fixtures/PORT0003_12_Scenario_Test_Matrix.json"),
            "--runtime-dir",
            str(runtime),
            "--evidence-out",
            str(evidence_path),
            "--trace-out",
            str(trace_path),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    require(completed.returncode == 0, f"Orchestrator failed:\n{completed.stdout}")
    return evidence_path.read_bytes(), trace_path.read_bytes()


def deterministic_execution_gate() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="port0003_public_validation_a_") as first_name, tempfile.TemporaryDirectory(
        prefix="port0003_public_validation_b_"
    ) as second_name:
        first = orchestrator_run(Path(first_name))
        second = orchestrator_run(Path(second_name))
    require(first == second, "Two clean orchestrator runs are not byte-identical")
    committed_evidence = (ROOT / "evidence/execution-and-reconciliation.json").read_bytes()
    committed_trace = (ROOT / "evidence/structured-execution-trace.jsonl").read_bytes()
    require(first[0] == committed_evidence, "Regenerated execution evidence differs from committed proof")
    require(first[1] == committed_trace, "Regenerated trace differs from committed proof")
    require(sha256_bytes(committed_evidence) == EVIDENCE_SHA256, "Execution evidence hash drift")
    require(sha256_bytes(committed_trace) == TRACE_SHA256, "Trace hash drift")
    return {
        "two_clean_runs_identical": True,
        "execution_evidence_sha256": EVIDENCE_SHA256,
        "structured_trace_sha256": TRACE_SHA256,
    }


def reliability_gate() -> dict[str, Any]:
    evidence = read_json("evidence/execution-and-reconciliation.json")
    require(evidence["terminal"] == "PASS_REFERENCE_ORCHESTRATOR_12_OF_12_AND_FINAL_RECONCILIATION", "Evidence terminal drift")
    require(evidence["n8n_runtime_status"] == N8N_STATUS, "n8n evidence status drift")
    rows = evidence["scenario_results"]
    require(len(rows) == 12 and all(row["result"] == "PASS" for row in rows), "Scenario result drift")
    require(tuple(row["observed_attempt_count"] for row in rows) == ATTEMPT_VECTOR, "Attempt vector drift")
    mismatch = rows[10]
    require(
        mismatch["observed_target_state"]["source_version"] == 1
        and mismatch["observed_target_state"]["last_event_id"] == "EVENT-0007"
        and mismatch["observed_target_state"]["correlation_id"] == "TRACE-0011"
        and mismatch["observed_attempt_count"] == 0,
        "Mismatch repair lineage drift",
    )
    dead_letter = evidence["reliability_evidence"]["dead_letters"]
    require(
        len(dead_letter) == 1
        and dead_letter[0]["event_id"] == "EVENT-0008"
        and dead_letter[0]["correlation_id"] == "TRACE-0009"
        and dead_letter[0]["replay_correlation_id"] == "TRACE-0012"
        and dead_letter[0]["resolution_status"] == "RESOLVED",
        "Dead-letter/replay lineage drift",
    )
    return {
        "scenarios_passed": 12,
        "attempt_vector": list(ATTEMPT_VECTOR),
        "mismatch_repair": "LATEST_VALID_SOURCE_TEST_PATH",
        "dead_letter_replay": "RESOLVED_WITH_LINKED_CORRELATION",
    }


def reconciliation_and_trace_gate() -> dict[str, Any]:
    evidence = read_json("evidence/execution-and-reconciliation.json")
    expected_summary = {
        "MATCH": 3,
        "MISSING_TARGET": 0,
        "STALE_TARGET": 0,
        "FIELD_MISMATCH": 0,
        "UNEXPECTED_TARGET": 0,
    }
    require(evidence["final_reconciliation"]["summary"] == expected_summary, "Final reconciliation drift")
    trace = [json.loads(line) for line in read_text("evidence/structured-execution-trace.jsonl").splitlines()]
    require([row["sequence"] for row in trace] == list(range(1, 49)), "Trace sequence drift")
    mismatch_stages = [row["stage"] for row in trace if row["scenario"] == "RECONCILIATION_MISMATCH"]
    require(
        mismatch_stages == ["FIELD_MISMATCH", "RECONCILIATION_CORRECTION_APPLIED", "MATCH"],
        "Mismatch trace stages drift",
    )
    correlations = {row["correlation_id"] for row in trace if row.get("correlation_id")}
    require(all(f"TRACE-{index:04d}" in correlations for index in range(1, 13)), "Correlation coverage drift")
    return {"matches": 3, "exceptions": 0, "trace_records": 48, "correlations": 12}


def n8n_gate() -> dict[str, Any]:
    workflow = read_json("04_Integration_Workflow/n8n_entitlement_sync_workflow.draft.json")
    meta = workflow["meta"]
    require(workflow["active"] is False, "n8n draft must remain inactive")
    require(len(workflow["nodes"]) == 10, "n8n draft node count drift")
    require(all(node["type"].startswith("n8n-nodes-base.") for node in workflow["nodes"]), "Non-built-in n8n node detected")
    require(
        meta["execution_status"] == N8N_STATUS
        and meta["structural_status"] == "UNEXECUTED_DRAFT_FOR_PORTABILITY_REVIEW"
        and meta["validated_structure_only"] is True
        and meta["contains_credentials"] is False,
        "n8n structural disclosure drift",
    )
    def keys(value: Any) -> list[str]:
        if isinstance(value, dict):
            return list(value) + [item for nested in value.values() for item in keys(nested)]
        if isinstance(value, list):
            return [item for nested in value for item in keys(nested)]
        return []
    require("credentials" not in keys(workflow), "Credential object detected in n8n draft")
    return {"nodes": 10, "active": False, "execution_status": N8N_STATUS, "credentials": 0}


def documentation_gate() -> dict[str, Any]:
    readme = read_text("README.md")
    limitations = read_text("LIMITATIONS.md")
    architecture = read_text("docs/ARCHITECTURE.md")
    case_study = read_text("CASE_STUDY.md")
    runbook = read_text("docs/SUPPORT_RUNBOOK.md")
    require("Python reference orchestrator is the only executed workflow" in readme, "README execution authority missing")
    require("n8n runtime execution was deferred" in readme, "README n8n disclosure missing")
    require("not as execution evidence or a claim of n8n proficiency" in readme, "README n8n claim boundary missing")
    require("Python reference orchestrator is the only executed workflow authority" in limitations, "Limitations execution authority missing")
    require("n8n was not executed" in limitations, "Limitations n8n disclosure missing")
    require(architecture.count("```mermaid") == 2 and architecture.count("**Text alternative:**") == 2, "Architecture visual/alternative count drift")
    require(case_study.count("```mermaid") == 3 and case_study.count("**Text alternative:**") == 3, "Case-study visual/alternative count drift")
    require("<!--" not in case_study, "Hidden metadata is not allowed in the public case study")
    topics = re.findall(r"^### [0-9]+\. ", runbook, flags=re.MULTILINE)
    incidents = re.findall(r"^### Incident [0-9]+ ", runbook, flags=re.MULTILINE)
    require(len(topics) == 10, "Runbook must retain ten topics")
    require(len(incidents) == 3, "Runbook must retain three incidents")
    topic_fields = (
        "Symptom",
        "Evidence to inspect",
        "Likely classification",
        "Diagnostic sequence",
        "Safe action",
        "Escalation condition",
        "Closure evidence",
    )
    incident_fields = (
        "Reported symptom",
        "Reproduction",
        "Evidence",
        "Root cause or classification",
        "Disposition",
        "Verification",
        "Knowledge/runbook link",
    )
    require(all(runbook.count(f"- **{field}:**") == 10 for field in topic_fields), "Runbook topic field drift")
    require(all(runbook.count(f"- **{field}:**") == 3 for field in incident_fields), "Incident field drift")
    return {"architecture_visuals": 2, "case_study_visuals": 3, "runbook_topics": 10, "incidents": 3}


def security_gate() -> dict[str, Any]:
    actual = visible_files()
    texts: dict[str, str] = {}
    for relative in actual:
        data = (ROOT / relative).read_bytes()
        require(b"\x00" not in data, f"Binary or NUL-containing file detected: {relative}")
        texts[relative] = data.decode("utf-8")
    joined = "\n".join(texts.values())
    windows_absolute = re.compile(r"(?<![A-Za-z0-9])" + r"[A-Za-z]:" + r"[\\/]")
    require(not windows_absolute.search(joined), "Absolute Windows filesystem path detected")
    private_roots = ("/" + "Users" + "/", "/" + "home" + "/")
    require(not any(root in joined for root in private_roots), "Private absolute filesystem path detected")
    internal_terms = (
        "Master" + "_Workspace",
        "Portfolio" + "_Codex",
        "PORT " + "Observer",
        "Not" + "Approved",
        "No" + "Approved" + "Release",
        "FINAL" + "_OWNER" + "_REVIEW",
        "package " + "manifest",
        "review " + "ZIP",
        "pre" + "write",
    )
    require(not any(term.lower() in joined.lower() for term in internal_terms), "Internal review/governance language detected")
    private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"
    require(private_key_marker not in joined, "Private key material detected")
    token_patterns = (
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
        re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    )
    require(not any(pattern.search(joined) for pattern in token_patterns), "Credential-like token detected")
    emails = re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", joined)
    require(not emails, f"Email address detected: {emails}")
    forbidden_names = {
        ".env",
        "node_modules",
        ".n8n",
        "__pycache__",
        ".pytest_cache",
    }
    require(not any(path.name in forbidden_names for path in ROOT.rglob("*")), "Forbidden runtime path detected")
    forbidden_suffixes = {".db", ".sqlite", ".sqlite3", ".pyc", ".zip"}
    require(not any(path.suffix.lower() in forbidden_suffixes for path in ROOT.rglob("*") if path.is_file()), "Forbidden runtime/package file detected")
    env_lines = [line for line in read_text(".env.example").splitlines() if line and not line.startswith("#")]
    require(all("=" in line for line in env_lines), "Malformed environment example")
    require("replace-with-local-test-value" in read_text(".env.example"), "Environment placeholder missing")
    return {
        "absolute_paths": 0,
        "credentials": 0,
        "emails": 0,
        "runtime_artifacts": 0,
        "files_scanned": 24,
    }


def links_gate() -> dict[str, Any]:
    links_checked = 0
    for relative in ("README.md", "CASE_STUDY.md", "LIMITATIONS.md", "docs/ARCHITECTURE.md", "docs/SUPPORT_RUNBOOK.md"):
        text = read_text(relative)
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
            target = match.group(1)
            if target.startswith(("#", "http://", "https://")):
                continue
            path_text = target.split("#", 1)[0]
            resolved = ((ROOT / relative).parent / path_text).resolve()
            require(ROOT == resolved or ROOT in resolved.parents, f"Link escapes repository: {target}")
            require(resolved.exists(), f"Broken relative link in {relative}: {target}")
            links_checked += 1
    return {"relative_links_checked": links_checked, "broken_links": 0}


GATES = (
    Gate(1, "Exact curated public topology", topology_gate),
    Gate(2, "Structured files parse", structured_gate),
    Gate(3, "Pinned dependency authority", dependency_gate),
    Gate(4, "Executable contracts and local API authentication", service_gate),
    Gate(5, "Public-safe mapping and reliability rules", mapping_gate),
    Gate(6, "Exact scenario and test topology", fixture_and_test_gate),
    Gate(7, "Twelve deterministic pytest outcomes", run_pytest_gate),
    Gate(8, "Two-run deterministic execution evidence", deterministic_execution_gate),
    Gate(9, "Reliability, repair, and replay behavior", reliability_gate),
    Gate(10, "Reconciliation and trace integrity", reconciliation_and_trace_gate),
    Gate(11, "n8n structural-only truth boundary", n8n_gate),
    Gate(12, "Public documentation and support coverage", documentation_gate),
    Gate(13, "Privacy, secret, path, and runtime-artifact boundary", security_gate),
    Gate(14, "Local relative-link integrity", links_gate),
)


def validation_payload(gate_results: list[dict[str, Any]]) -> dict[str, Any]:
    evidence = read_json("evidence/execution-and-reconciliation.json")
    return {
        "schema_version": "1.0.0",
        "scope": "PUBLIC_TECHNICAL_WORK_SAMPLE",
        "terminal": PASS_TERMINAL,
        "execution_authority": "PYTHON_REFERENCE_ORCHESTRATOR",
        "n8n_runtime_status": N8N_STATUS,
        "n8n_execution_claim": False,
        "synthetic_nonproduction_boundary": True,
        "file_count": 24,
        "gate_summary": {"pass": len(gate_results), "fail": 0, "total": len(GATES)},
        "gates": gate_results,
        "tests": {
            "scenarios": 12,
            "passed": 12,
            "failed": 0,
            "attempt_vector": list(ATTEMPT_VECTOR),
        },
        "final_reconciliation": evidence["final_reconciliation"]["summary"],
        "deterministic_evidence": {
            "execution_sha256": EVIDENCE_SHA256,
            "trace_sha256": TRACE_SHA256,
        },
        "external_actions": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-result",
        type=Path,
        default=None,
        help="Write the deterministic result to the repository-relative validation summary path.",
    )
    return parser.parse_args()


def main() -> int:
    sys.dont_write_bytecode = True
    args = parse_args()
    gate_results: list[dict[str, Any]] = []
    try:
        for gate in GATES:
            evidence = gate.function()
            gate_results.append(
                {"gate": gate.number, "name": gate.name, "status": "PASS", "evidence": evidence}
            )
        payload = validation_payload(gate_results)
        expected_bytes = canonical_json_bytes(payload)
        result_path = ROOT / RESULT_REL
        if args.write_result is not None:
            requested = args.write_result
            if not requested.is_absolute():
                requested = ROOT / requested
            require(requested.resolve() == result_path.resolve(), "Result may only be written to validation/validation-summary.json")
            result_path.write_bytes(expected_bytes)
            require(visible_files() == set(EXPECTED_FILES), "Topology is not exact after result generation")
            security_gate()
        else:
            require(result_path.is_file(), "Validation result is missing; use --write-result once")
            require(result_path.read_bytes() == expected_bytes, "Committed validation result is stale")
        print(
            json.dumps(
                {
                    "terminal": PASS_TERMINAL,
                    "gates": f"PASS_{len(GATES)}_OF_{len(GATES)}",
                    "files": 24,
                    "tests": "PASS_12_OF_12",
                    "reconciliation": "MATCH_3_EXCEPTIONS_0",
                    "n8n_runtime_status": N8N_STATUS,
                    "result_sha256": sha256_bytes(expected_bytes),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "terminal": "HOLD_PORT0003_PUBLIC_TECHNICAL_CANDIDATE",
                    "passed_gates": len(gate_results),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
