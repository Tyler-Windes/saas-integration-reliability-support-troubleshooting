"""Exactly twelve synthetic end-to-end expected-behavior tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "06_Test_Fixtures" / "PORT0003_12_Scenario_Test_Matrix.json"
ORCHESTRATOR = (
    PROJECT_ROOT / "04_Integration_Workflow" / "reference_orchestrator.py"
)


def _load_orchestrator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("port0003_reference_orchestrator_test", ORCHESTRATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load the local reference orchestrator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def canonical_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    module = _load_orchestrator()
    runtime_dir = tmp_path_factory.mktemp("port0003_runtime")
    evidence, trace = module.run_canonical_suite(FIXTURES, runtime_dir)
    return {
        "evidence": evidence,
        "trace": trace,
        "by_name": {row["scenario"]: row for row in evidence["scenario_results"]},
    }


def _assert_pass(canonical_run: dict[str, Any], name: str) -> dict[str, Any]:
    result = canonical_run["by_name"][name]
    assert result["result"] == "PASS"
    assert result["observed_disposition"] == result["expected_disposition"]
    assert result["observed_attempt_count"] == result["expected_attempt_count"]
    assert result["observed_target_state"] == result["expected_target_state"]
    return result


def test_01_valid_create(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "VALID_CREATE")
    assert result["observed_target_state"]["entitlement_status"] == "ENABLED"
    assert result["observed_target_state"]["support_priority"] == "Normal"


def test_02_valid_tier_update(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "VALID_TIER_UPDATE")
    assert result["observed_target_state"]["source_version"] == 2
    assert result["observed_target_state"]["support_priority"] == "Urgent"


def test_03_valid_suspension(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "VALID_SUSPENSION")
    assert result["observed_target_state"]["source_version"] == 3
    assert result["observed_target_state"]["entitlement_status"] == "DISABLED"


def test_04_duplicate_event(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "DUPLICATE_EVENT")
    assert result["observed_attempt_count"] == 0
    assert result["observed_target_state"]["correlation_id"] == "TRACE-0003"


def test_05_out_of_order_event(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "OUT_OF_ORDER_EVENT")
    assert result["observed_attempt_count"] == 0
    assert result["observed_target_state"]["source_version"] == 3


def test_06_missing_required_field(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "MISSING_REQUIRED_FIELD")
    assert result["observed_attempt_count"] == 0
    assert result["observed_target_state"] is None


def test_07_invalid_enum_value(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "INVALID_ENUM_VALUE")
    assert result["observed_attempt_count"] == 0
    assert result["observed_target_state"] is None


def test_08_transient_429_recovery(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "TRANSIENT_429_RECOVERY")
    assert result["observed_attempt_count"] == 2
    assert result["observed_target_state"]["support_priority"] == "High"
    attempts = [
        row
        for row in canonical_run["evidence"]["reliability_evidence"]["attempts"]
        if row["correlation_id"] == "TRACE-0008"
    ]
    assert [row["response_status"] for row in attempts] == [429, 200]


def test_09_transient_503_exhaustion(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "TRANSIENT_503_EXHAUSTION")
    assert result["observed_attempt_count"] == 3
    attempts = [
        row
        for row in canonical_run["evidence"]["reliability_evidence"]["attempts"]
        if row["correlation_id"] == "TRACE-0009"
    ]
    assert [row["response_status"] for row in attempts] == [503, 503, 503]
    dead = canonical_run["evidence"]["reliability_evidence"]["dead_letters"]
    assert len(dead) == 1
    assert dead[0]["event_id"] == "EVENT-0008"


def test_10_authentication_failure(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "AUTHENTICATION_FAILURE")
    assert result["observed_attempt_count"] == 0
    assert not any(
        row["correlation_id"] == "TRACE-0010"
        for row in canonical_run["evidence"]["reliability_evidence"]["attempts"]
    )


def test_11_reconciliation_mismatch(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "RECONCILIATION_MISMATCH")
    assert "FIELD_MISMATCH" in result["observed_log_evidence"]
    assert "RECONCILIATION_CORRECTION_APPLIED" in result["observed_log_evidence"]
    assert result["observed_target_state"]["correlation_id"] == "TRACE-0011"


def test_12_dead_letter_replay(canonical_run: dict[str, Any]) -> None:
    result = _assert_pass(canonical_run, "DEAD_LETTER_REPLAY")
    assert result["observed_target_state"]["last_event_id"] == "EVENT-0008"
    assert result["observed_target_state"]["correlation_id"] == "TRACE-0012"
    dead = canonical_run["evidence"]["reliability_evidence"]["dead_letters"][0]
    assert dead["correlation_id"] == "TRACE-0009"
    assert dead["replay_correlation_id"] == "TRACE-0012"
    assert dead["resolution_status"] == "RESOLVED"
    assert canonical_run["evidence"]["final_reconciliation"]["summary"] == {
        "MATCH": 3,
        "MISSING_TARGET": 0,
        "STALE_TARGET": 0,
        "FIELD_MISMATCH": 0,
        "UNEXPECTED_TARGET": 0,
    }

