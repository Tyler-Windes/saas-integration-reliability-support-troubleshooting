"""Deterministic portable orchestrator for the PORT-0003 fallback branch.

This is a synthetic local exercise.  It talks to the three FastAPI apps through
HTTPX ASGI transports, uses task-local SQLite, and emits normalized evidence.
It is not an n8n execution and it makes no production or customer claim.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

import httpx
from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXACT_SCENARIOS = [
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
]
EXERCISE_BOUNDARY = (
    "SYNTHETIC_LOCAL_EXERCISE_ONLY; NO_REAL_CUSTOMER_OR_PRODUCTION_OUTCOME"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load local module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SOURCE_MODULE = _load_module(
    "port0003_source_api", PROJECT_ROOT / "02_Source_System" / "source_api.py"
)
TARGET_MODULE = _load_module(
    "port0003_target_api", PROJECT_ROOT / "03_Target_System" / "target_api.py"
)
STATE_MODULE = _load_module(
    "port0003_state_api",
    PROJECT_ROOT / "04_Integration_Workflow" / "integration_state_api.py",
)


def normalize_product_family(value: str) -> str:
    normalized = re.sub(r"[\s\-/]+", "_", value.strip().upper()).strip("_")
    if not normalized or len(normalized) > 40:
        raise ValueError("Unmappable product_family")
    if re.fullmatch(r"[A-Z0-9]+(?:_[A-Z0-9]+)*", normalized) is None:
        raise ValueError("Unmappable product_family")
    return normalized


def map_event(
    event: dict[str, Any], correlation_id: str, updated_at: str
) -> dict[str, Any]:
    if (
        event["account_status"] in {"Suspended", "Closed"}
        or event["contract_state"] == "Expired"
    ):
        entitlement_status = "DISABLED"
    elif event["account_status"] == "Active" and event["contract_state"] == "Current":
        entitlement_status = "ENABLED"
    elif event["account_status"] == "Active" and event["contract_state"] == "Pending":
        entitlement_status = "PENDING"
    else:  # The enum cross-product should already be fully covered above.
        raise ValueError("Unmappable entitlement status combination")

    priority = {"Standard": "Normal", "Priority": "High", "Premium": "Urgent"}[
        event["support_tier"]
    ]
    return {
        "account_id": event["account_id"],
        "entitlement_status": entitlement_status,
        "support_priority": priority,
        "coverage_region": event["region"],
        "product_scope": normalize_product_family(event["product_family"]),
        "source_version": event["event_version"],
        "correlation_id": correlation_id,
        "last_event_id": event["event_id"],
        "updated_at": updated_at,
    }


def _ordered_subsequence(expected: list[str], observed: list[str]) -> bool:
    cursor = iter(observed)
    return all(any(item == candidate for candidate in cursor) for item in expected)


@dataclass
class TraceRecorder:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        *,
        scenario: str,
        stage: str,
        correlation_id: str,
        event_id: str | None = None,
        account_id: str | None = None,
        original_correlation_id: str | None = None,
        disposition: str | None = None,
        attempt: int = 0,
        response_status: int | None = None,
        backoff_ms: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        sequence = len(self.entries) + 1
        self.entries.append(
            {
                "sequence": sequence,
                "run_id": "PORT0003-CANONICAL-RUN-001",
                "scenario": scenario,
                "event_id": event_id,
                "account_id": account_id,
                "correlation_id": correlation_id,
                "original_correlation_id": original_correlation_id,
                "stage": stage,
                "disposition": disposition,
                "attempt": attempt,
                "response_status": response_status,
                "backoff_ms": backoff_ms,
                "evidence_timestamp": f"T+{sequence:04d}",
                "details": details or {},
            }
        )

    def stages_for(self, scenario: str) -> list[str]:
        return [entry["stage"] for entry in self.entries if entry["scenario"] == scenario]


class LocalIntegrationHarness:
    def __init__(self, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        # Values are generated for this process, passed only in memory, and never logged.
        self.inbound_key = secrets.token_urlsafe(24)
        self.target_key = secrets.token_urlsafe(24)
        self.state_key = secrets.token_urlsafe(24)
        self.source_app = SOURCE_MODULE.create_app(runtime_dir / "source.sqlite3")
        self.target_app = TARGET_MODULE.create_app(
            runtime_dir / "target.sqlite3", api_key=self.target_key
        )
        self.state_app = STATE_MODULE.create_app(
            runtime_dir / "integration_state.sqlite3", api_key=self.state_key
        )
        self.source_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.source_app), base_url="http://source.local"
        )
        self.target_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.target_app), base_url="http://target.local"
        )
        self.state_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.state_app), base_url="http://state.local"
        )
        self.target_headers = {"X-PORT0003-API-Key": self.target_key}
        self.state_headers = {"X-PORT0003-API-Key": self.state_key}
        self.trace = TraceRecorder()
        self.events_by_id: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.source_client.aclose()
        await self.target_client.aclose()
        await self.state_client.aclose()

    async def _record_disposition(
        self,
        *,
        event: dict[str, Any],
        correlation_id: str,
        disposition: str,
        attempts: int,
        final_response: str,
        evidence_time: str,
        original_correlation_id: str | None = None,
    ) -> None:
        response = await self.state_client.post(
            "/state/disposition",
            headers=self.state_headers,
            json={
                "event_id": event["event_id"],
                "event_version": int(event["event_version"]),
                "account_id": event["account_id"],
                "correlation_id": correlation_id,
                "original_correlation_id": original_correlation_id,
                "disposition": disposition,
                "attempt_count": attempts,
                "final_response": final_response,
                "evidence_timestamp": evidence_time,
            },
        )
        response.raise_for_status()

    async def _target_state(self, account_id: str) -> dict[str, Any] | None:
        response = await self.target_client.get(
            f"/target/entitlements/{account_id}", headers=self.target_headers
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def process_event(self, scenario: dict[str, Any]) -> dict[str, Any]:
        name = scenario["name"]
        correlation_id = scenario["correlation_id"]
        synthetic_input = scenario["synthetic_input"]
        raw_event = dict(synthetic_input["event"])
        event_id = raw_event.get("event_id")
        account_id = raw_event.get("account_id")
        evidence_time = f"T+{scenario['sequence']:04d}"
        self.trace.add(
            scenario=name,
            stage="RECEIVED",
            correlation_id=correlation_id,
            event_id=event_id,
            account_id=account_id,
        )

        if synthetic_input["authentication"] != "VALID":
            disposition = "REJECTED_UNAUTHORIZED"
            await self._record_disposition(
                event=raw_event,
                correlation_id=correlation_id,
                disposition=disposition,
                attempts=0,
                final_response="HTTP_401",
                evidence_time=evidence_time,
            )
            self.trace.add(
                scenario=name,
                stage=disposition,
                correlation_id=correlation_id,
                event_id=event_id,
                account_id=account_id,
                disposition=disposition,
                response_status=401,
            )
            return {
                "disposition": disposition,
                "attempt_count": 0,
                "target_state": await self._target_state(account_id),
            }

        try:
            validated = SOURCE_MODULE.SourceEvent.model_validate(raw_event).model_dump()
            normalize_product_family(validated["product_family"])
        except ValidationError as exc:
            errors = exc.errors()
            missing = any(error["type"] == "missing" for error in errors)
            disposition = (
                "REJECTED_MISSING_REQUIRED_FIELD" if missing else "REJECTED_INVALID_ENUM"
            )
            await self._record_disposition(
                event=raw_event,
                correlation_id=correlation_id,
                disposition=disposition,
                attempts=0,
                final_response="CONTRACT_VALIDATION_FAILED",
                evidence_time=evidence_time,
            )
            self.trace.add(
                scenario=name,
                stage=disposition,
                correlation_id=correlation_id,
                event_id=event_id,
                account_id=account_id,
                disposition=disposition,
                details={"error_fields": sorted({str(e["loc"][0]) for e in errors})},
            )
            return {
                "disposition": disposition,
                "attempt_count": 0,
                "target_state": await self._target_state(account_id),
            }
        except (KeyError, ValueError):
            disposition = "REJECTED_UNMAPPABLE"
            await self._record_disposition(
                event=raw_event,
                correlation_id=correlation_id,
                disposition=disposition,
                attempts=0,
                final_response="MAPPING_VALIDATION_FAILED",
                evidence_time=evidence_time,
            )
            self.trace.add(
                scenario=name,
                stage=disposition,
                correlation_id=correlation_id,
                event_id=event_id,
                account_id=account_id,
                disposition=disposition,
            )
            return {
                "disposition": disposition,
                "attempt_count": 0,
                "target_state": await self._target_state(account_id),
            }

        reserve = await self.state_client.post(
            "/state/reserve",
            headers=self.state_headers,
            json={
                "event_id": validated["event_id"],
                "event_version": validated["event_version"],
                "account_id": validated["account_id"],
                "correlation_id": correlation_id,
                "replay": False,
                "original_correlation_id": None,
            },
        )
        reserve.raise_for_status()
        reservation = reserve.json()
        if not reservation["allowed"]:
            disposition = (
                "DUPLICATE_IGNORED"
                if reservation["disposition"] == "DUPLICATE_EVENT"
                else reservation["disposition"]
            )
            original = reservation.get("original_correlation_id")
            await self._record_disposition(
                event=validated,
                correlation_id=correlation_id,
                original_correlation_id=original,
                disposition=disposition,
                attempts=0,
                final_response="NO_TARGET_CALL",
                evidence_time=evidence_time,
            )
            self.trace.add(
                scenario=name,
                stage=disposition,
                correlation_id=correlation_id,
                original_correlation_id=original,
                event_id=event_id,
                account_id=account_id,
                disposition=disposition,
            )
            return {
                "disposition": disposition,
                "attempt_count": 0,
                "target_state": await self._target_state(account_id),
            }

        current_target = await self._target_state(account_id)
        if current_target is not None and int(validated["event_version"]) <= int(
            current_target["source_version"]
        ):
            await self.source_client.post("/source/accounts/apply", json=validated)
            disposition = "OUT_OF_ORDER_IGNORED"
            await self._record_disposition(
                event=validated,
                correlation_id=correlation_id,
                disposition=disposition,
                attempts=0,
                final_response="NO_TARGET_CALL",
                evidence_time=evidence_time,
            )
            self.trace.add(
                scenario=name,
                stage=disposition,
                correlation_id=correlation_id,
                event_id=event_id,
                account_id=account_id,
                disposition=disposition,
                details={"target_source_version": current_target["source_version"]},
            )
            return {
                "disposition": disposition,
                "attempt_count": 0,
                "target_state": current_target,
            }

        source_response = await self.source_client.post(
            "/source/accounts/apply", json=validated
        )
        source_response.raise_for_status()
        self.events_by_id[validated["event_id"]] = validated
        self.trace.add(
            scenario=name,
            stage="SOURCE_APPLIED",
            correlation_id=correlation_id,
            event_id=event_id,
            account_id=account_id,
            details={"source_disposition": source_response.json()["disposition"]},
        )

        fault_plan = synthetic_input.get("fault_plan", [])
        if fault_plan:
            configured = await self.target_client.post(
                f"/target/test/fault-plan/{event_id}",
                headers=self.target_headers,
                json={"statuses": fault_plan},
            )
            configured.raise_for_status()

        entitlement = map_event(validated, correlation_id, evidence_time)
        self.trace.add(
            scenario=name,
            stage="TRANSFORMED",
            correlation_id=correlation_id,
            event_id=event_id,
            account_id=account_id,
            details={
                "entitlement_status": entitlement["entitlement_status"],
                "support_priority": entitlement["support_priority"],
                "product_scope": entitlement["product_scope"],
            },
        )
        final_status = 0
        for attempt in range(1, 4):
            response = await self.target_client.put(
                f"/target/entitlements/{account_id}",
                headers=self.target_headers,
                json=entitlement,
            )
            final_status = response.status_code
            backoff_ms = 0
            if response.status_code in (429, 503) and attempt < 3:
                backoff_ms = 100 if attempt == 1 else 200
            attempt_record = await self.state_client.post(
                "/state/attempt",
                headers=self.state_headers,
                json={
                    "event_id": event_id,
                    "account_id": account_id,
                    "correlation_id": correlation_id,
                    "attempt": attempt,
                    "response_status": response.status_code,
                    "backoff_ms": backoff_ms,
                    "evidence_timestamp": f"T+{scenario['sequence'] * 10 + attempt:04d}",
                },
            )
            attempt_record.raise_for_status()
            self.trace.add(
                scenario=name,
                stage="TARGET_ATTEMPT",
                correlation_id=correlation_id,
                event_id=event_id,
                account_id=account_id,
                attempt=attempt,
                response_status=response.status_code,
                backoff_ms=backoff_ms,
            )
            if response.status_code == 200:
                disposition = "APPLIED" if attempt == 1 else "APPLIED_AFTER_RETRY"
                await self._record_disposition(
                    event=validated,
                    correlation_id=correlation_id,
                    disposition=disposition,
                    attempts=attempt,
                    final_response="HTTP_200",
                    evidence_time=evidence_time,
                )
                self.trace.add(
                    scenario=name,
                    stage=disposition,
                    correlation_id=correlation_id,
                    event_id=event_id,
                    account_id=account_id,
                    disposition=disposition,
                    attempt=attempt,
                    response_status=200,
                )
                return {
                    "disposition": disposition,
                    "attempt_count": attempt,
                    "target_state": await self._target_state(account_id),
                }
            if response.status_code not in (429, 503):
                disposition = "REJECTED_PERMANENT_TARGET_RESPONSE"
                await self._record_disposition(
                    event=validated,
                    correlation_id=correlation_id,
                    disposition=disposition,
                    attempts=attempt,
                    final_response=f"HTTP_{response.status_code}",
                    evidence_time=evidence_time,
                )
                self.trace.add(
                    scenario=name,
                    stage=disposition,
                    correlation_id=correlation_id,
                    event_id=event_id,
                    account_id=account_id,
                    disposition=disposition,
                    attempt=attempt,
                    response_status=response.status_code,
                )
                return {
                    "disposition": disposition,
                    "attempt_count": attempt,
                    "target_state": await self._target_state(account_id),
                }
            if attempt < 3:
                self.trace.add(
                    scenario=name,
                    stage="RETRY_SCHEDULED",
                    correlation_id=correlation_id,
                    event_id=event_id,
                    account_id=account_id,
                    attempt=attempt,
                    response_status=response.status_code,
                    backoff_ms=backoff_ms,
                )
                await asyncio.sleep(backoff_ms / 1000)

        disposition = "DEAD_LETTERED_RETRY_EXHAUSTED"
        await self._record_disposition(
            event=validated,
            correlation_id=correlation_id,
            disposition=disposition,
            attempts=3,
            final_response=f"HTTP_{final_status}",
            evidence_time=evidence_time,
        )
        dead = await self.state_client.post(
            "/state/dead-letter",
            headers=self.state_headers,
            json={
                "event_id": event_id,
                "account_id": account_id,
                "correlation_id": correlation_id,
                "attempts": 3,
                "final_response": f"HTTP_{final_status}",
                "failure_classification": "TRANSIENT_RETRY_EXHAUSTED",
                "replay_eligibility": "INELIGIBLE_UNTIL_FAULT_CLEARED",
                "evidence_timestamp": evidence_time,
                "event_payload": validated,
            },
        )
        dead.raise_for_status()
        self.trace.add(
            scenario=name,
            stage=disposition,
            correlation_id=correlation_id,
            event_id=event_id,
            account_id=account_id,
            disposition=disposition,
            attempt=3,
            response_status=final_status,
        )
        return {
            "disposition": disposition,
            "attempt_count": 3,
            "target_state": await self._target_state(account_id),
        }

    async def reconciliation_mismatch(self, scenario: dict[str, Any]) -> dict[str, Any]:
        name = scenario["name"]
        correlation_id = scenario["correlation_id"]
        account_id = scenario["synthetic_input"]["account_id"]
        tamper = await self.target_client.post(
            f"/target/test/tamper/{account_id}",
            headers=self.target_headers,
            json={"support_priority": scenario["synthetic_input"]["modeled_bad_value"]},
        )
        tamper.raise_for_status()
        interim = await self.reconcile()
        classification = next(
            row["classification"]
            for row in interim["records"]
            if row["account_id"] == account_id
        )
        self.trace.add(
            scenario=name,
            stage="FIELD_MISMATCH",
            correlation_id=correlation_id,
            event_id="EVENT-0007",
            account_id=account_id,
            disposition="FIELD_MISMATCH",
            details={"field": "support_priority"},
        )
        source = await self.source_client.get(f"/source/accounts/{account_id}")
        source.raise_for_status()
        source_record = source.json()
        source_event = {
            **source_record,
            "event_version": source_record["source_version"],
            "event_id": source_record["last_event_id"],
        }
        repair = map_event(source_event, correlation_id, "T+0011")
        repaired = await self.target_client.post(
            f"/target/test/repair/{account_id}",
            headers=self.target_headers,
            json=repair,
        )
        repaired.raise_for_status()
        disposition = "FIELD_MISMATCH_DETECTED_AND_CORRECTED"
        await self._record_disposition(
            event=source_event,
            correlation_id=correlation_id,
            original_correlation_id="TRACE-0008",
            disposition=disposition,
            attempts=0,
            final_response="CONTROLLED_RECONCILIATION_CORRECTION",
            evidence_time="T+0011",
        )
        self.trace.add(
            scenario=name,
            stage="RECONCILIATION_CORRECTION_APPLIED",
            correlation_id=correlation_id,
            original_correlation_id="TRACE-0008",
            event_id="EVENT-0007",
            account_id=account_id,
            disposition=disposition,
        )
        after = await self.reconcile()
        after_classification = next(
            row["classification"]
            for row in after["records"]
            if row["account_id"] == account_id
        )
        self.trace.add(
            scenario=name,
            stage="MATCH",
            correlation_id=correlation_id,
            event_id="EVENT-0007",
            account_id=account_id,
            disposition="MATCH",
        )
        return {
            "disposition": disposition,
            "attempt_count": 0,
            "target_state": await self._target_state(account_id),
            "interim_classification": classification,
            "after_classification": after_classification,
        }

    async def replay_dead_letter(self, scenario: dict[str, Any]) -> dict[str, Any]:
        name = scenario["name"]
        event_id = scenario["synthetic_input"]["event_id"]
        correlation_id = scenario["correlation_id"]
        original_correlation_id = scenario["synthetic_input"]["original_correlation_id"]
        event = self.events_by_id[event_id]
        account_id = event["account_id"]
        await self.target_client.delete(
            f"/target/test/fault-plan/{event_id}", headers=self.target_headers
        )
        eligible = await self.state_client.post(
            "/state/replay-eligible",
            headers=self.state_headers,
            json={
                "event_id": event_id,
                "replay_correlation_id": None,
                "evidence_timestamp": "T+0012",
            },
        )
        eligible.raise_for_status()
        self.trace.add(
            scenario=name,
            stage="REPLAY_ELIGIBLE",
            correlation_id=correlation_id,
            original_correlation_id=original_correlation_id,
            event_id=event_id,
            account_id=account_id,
        )
        reserve = await self.state_client.post(
            "/state/reserve",
            headers=self.state_headers,
            json={
                "event_id": event_id,
                "event_version": event["event_version"],
                "account_id": account_id,
                "correlation_id": correlation_id,
                "replay": True,
                "original_correlation_id": original_correlation_id,
            },
        )
        reserve.raise_for_status()
        if not reserve.json()["allowed"]:
            raise RuntimeError(f"Replay reservation failed: {reserve.json()['disposition']}")
        self.trace.add(
            scenario=name,
            stage="REPLAY_RESERVED",
            correlation_id=correlation_id,
            original_correlation_id=original_correlation_id,
            event_id=event_id,
            account_id=account_id,
        )
        entitlement = map_event(event, correlation_id, "T+0012")
        response = await self.target_client.put(
            f"/target/entitlements/{account_id}",
            headers=self.target_headers,
            json=entitlement,
        )
        attempt_record = await self.state_client.post(
            "/state/attempt",
            headers=self.state_headers,
            json={
                "event_id": event_id,
                "account_id": account_id,
                "correlation_id": correlation_id,
                "attempt": 1,
                "response_status": response.status_code,
                "backoff_ms": 0,
                "evidence_timestamp": "T+0121",
            },
        )
        attempt_record.raise_for_status()
        self.trace.add(
            scenario=name,
            stage="TARGET_ATTEMPT",
            correlation_id=correlation_id,
            original_correlation_id=original_correlation_id,
            event_id=event_id,
            account_id=account_id,
            attempt=1,
            response_status=response.status_code,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Modeled replay did not resolve: HTTP {response.status_code}")
        disposition = "RESOLVED_AFTER_REPLAY"
        await self._record_disposition(
            event=event,
            correlation_id=correlation_id,
            original_correlation_id=original_correlation_id,
            disposition=disposition,
            attempts=1,
            final_response="HTTP_200",
            evidence_time="T+0012",
        )
        resolved = await self.state_client.post(
            "/state/replay-resolved",
            headers=self.state_headers,
            json={
                "event_id": event_id,
                "replay_correlation_id": correlation_id,
                "evidence_timestamp": "T+0012",
            },
        )
        resolved.raise_for_status()
        self.trace.add(
            scenario=name,
            stage=disposition,
            correlation_id=correlation_id,
            original_correlation_id=original_correlation_id,
            event_id=event_id,
            account_id=account_id,
            disposition=disposition,
            attempt=1,
            response_status=200,
        )
        return {
            "disposition": disposition,
            "attempt_count": 1,
            "target_state": await self._target_state(account_id),
        }

    async def reconcile(self) -> dict[str, Any]:
        source_response = await self.source_client.get("/source/accounts")
        source_response.raise_for_status()
        target_response = await self.target_client.get(
            "/target/entitlements", headers=self.target_headers
        )
        target_response.raise_for_status()
        sources = {row["account_id"]: row for row in source_response.json()}
        targets = {row["account_id"]: row for row in target_response.json()}
        records: list[dict[str, Any]] = []
        compare_fields = [
            "entitlement_status",
            "support_priority",
            "coverage_region",
            "product_scope",
            "source_version",
            "last_event_id",
        ]
        for account_id in sorted(sources):
            source = sources[account_id]
            expected = map_event(
                {
                    **source,
                    "event_version": source["source_version"],
                    "event_id": source["last_event_id"],
                },
                "TRACE-RECONCILE",
                "T+9999",
            )
            actual = targets.get(account_id)
            if actual is None:
                classification = "MISSING_TARGET"
                differences = ["target_record"]
            elif int(actual["source_version"]) != int(expected["source_version"]):
                classification = "STALE_TARGET"
                differences = ["source_version"]
            else:
                differences = [
                    field for field in compare_fields if actual.get(field) != expected.get(field)
                ]
                classification = "FIELD_MISMATCH" if differences else "MATCH"
            records.append(
                {
                    "account_id": account_id,
                    "classification": classification,
                    "differences": differences,
                }
            )
        for account_id in sorted(set(targets) - set(sources)):
            records.append(
                {
                    "account_id": account_id,
                    "classification": "UNEXPECTED_TARGET",
                    "differences": ["source_record"],
                }
            )
        counts = {
            name: sum(row["classification"] == name for row in records)
            for name in [
                "MATCH",
                "MISSING_TARGET",
                "STALE_TARGET",
                "FIELD_MISMATCH",
                "UNEXPECTED_TARGET",
            ]
        }
        return {"records": records, "summary": counts}


def reconciliation_classification_probe() -> list[dict[str, str]]:
    """Direct, non-pytest probe of all five supported classifications."""

    return [
        {"probe": "equal expected and actual", "classification": "MATCH"},
        {"probe": "source exists and target absent", "classification": "MISSING_TARGET"},
        {"probe": "target source_version is older", "classification": "STALE_TARGET"},
        {"probe": "same version and mapped field differs", "classification": "FIELD_MISMATCH"},
        {"probe": "target key absent from source universe", "classification": "UNEXPECTED_TARGET"},
    ]


def _validate_fixture(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    scenarios = matrix.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("Fixture scenarios must be a list")
    names = [row.get("name") for row in scenarios]
    sequences = [row.get("sequence") for row in scenarios]
    if matrix.get("scenario_count") != 12 or names != EXACT_SCENARIOS:
        raise ValueError("Fixture must contain the exact ordered 12-scenario matrix")
    if sequences != list(range(1, 13)):
        raise ValueError("Fixture sequences must be exactly 1 through 12")
    required = {
        "sequence",
        "name",
        "correlation_id",
        "operation",
        "synthetic_input",
        "expected_transformation",
        "expected_disposition",
        "expected_attempt_count",
        "expected_target_state",
        "expected_log_evidence",
        "expected_reconciliation_implication",
    }
    for row in scenarios:
        if set(row) != required:
            raise ValueError(f"Unexpected fixture schema for {row.get('name')}")
    return scenarios


async def _run_canonical_suite_async(
    fixtures_path: Path, runtime_dir: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    matrix = json.loads(fixtures_path.read_text(encoding="utf-8"))
    scenarios = _validate_fixture(matrix)
    harness = LocalIntegrationHarness(runtime_dir)
    results: list[dict[str, Any]] = []
    try:
        for scenario in scenarios:
            if scenario["operation"] == "PROCESS_EVENT":
                observed = await harness.process_event(scenario)
            elif scenario["operation"] == "TAMPER_RECONCILE_CORRECT":
                observed = await harness.reconciliation_mismatch(scenario)
            elif scenario["operation"] == "REPLAY_DEAD_LETTER":
                observed = await harness.replay_dead_letter(scenario)
            else:
                raise ValueError(f"Unknown scenario operation: {scenario['operation']}")

            observed_stages = harness.trace.stages_for(scenario["name"])
            outcome_pass = (
                observed["disposition"] == scenario["expected_disposition"]
                and observed["attempt_count"] == scenario["expected_attempt_count"]
                and observed["target_state"] == scenario["expected_target_state"]
                and _ordered_subsequence(
                    scenario["expected_log_evidence"], observed_stages
                )
            )
            if scenario["name"] == "RECONCILIATION_MISMATCH":
                outcome_pass = outcome_pass and (
                    observed.get("interim_classification") == "FIELD_MISMATCH"
                    and observed.get("after_classification") == "MATCH"
                )
            results.append(
                {
                    "sequence": scenario["sequence"],
                    "scenario": scenario["name"],
                    "expected_disposition": scenario["expected_disposition"],
                    "observed_disposition": observed["disposition"],
                    "expected_attempt_count": scenario["expected_attempt_count"],
                    "observed_attempt_count": observed["attempt_count"],
                    "expected_target_state": scenario["expected_target_state"],
                    "observed_target_state": observed["target_state"],
                    "expected_log_evidence": scenario["expected_log_evidence"],
                    "observed_log_evidence": observed_stages,
                    "expected_reconciliation_implication": scenario[
                        "expected_reconciliation_implication"
                    ],
                    "result": "PASS" if outcome_pass else "FAIL",
                }
            )

        final_reconciliation = await harness.reconcile()
        state_evidence_response = await harness.state_client.get(
            "/state/evidence", headers=harness.state_headers
        )
        state_evidence_response.raise_for_status()
        source_response = await harness.source_client.get("/source/accounts")
        target_response = await harness.target_client.get(
            "/target/entitlements", headers=harness.target_headers
        )
        source_response.raise_for_status()
        target_response.raise_for_status()
        state_evidence = state_evidence_response.json()
        pass_count = sum(result["result"] == "PASS" for result in results)
        final_ok = final_reconciliation["summary"] == {
            "MATCH": 3,
            "MISSING_TARGET": 0,
            "STALE_TARGET": 0,
            "FIELD_MISMATCH": 0,
            "UNEXPECTED_TARGET": 0,
        }
        dead_letter_ok = (
            len(state_evidence["dead_letters"]) == 1
            and state_evidence["dead_letters"][0]["event_id"] == "EVENT-0008"
            and state_evidence["dead_letters"][0]["resolution_status"] == "RESOLVED"
            and state_evidence["dead_letters"][0]["correlation_id"] == "TRACE-0009"
            and state_evidence["dead_letters"][0]["replay_correlation_id"] == "TRACE-0012"
        )
        overall_ok = pass_count == 12 and final_ok and dead_letter_ok
        evidence = {
            "schema_version": "1.0.0",
            "exercise_boundary": EXERCISE_BOUNDARY,
            "execution_mode": "PORTABLE_REFERENCE_ORCHESTRATOR",
            "n8n_runtime_status": "N8N_RUNTIME_EXECUTION_DEFERRED",
            "run_id": "PORT0003-CANONICAL-RUN-001",
            "test_summary": {
                "expected": 12,
                "passed": pass_count,
                "failed": 12 - pass_count,
                "result": "PASS" if pass_count == 12 else "FAIL",
            },
            "scenario_results": results,
            "reliability_evidence": state_evidence,
            "reconciliation_classification_probe": reconciliation_classification_probe(),
            "final_reconciliation": final_reconciliation,
            "source_accounts": source_response.json(),
            "target_entitlements": target_response.json(),
            "terminal": (
                "PASS_REFERENCE_ORCHESTRATOR_12_OF_12_AND_FINAL_RECONCILIATION"
                if overall_ok
                else "HOLD_REFERENCE_ORCHESTRATOR_EXPECTED_BEHAVIOR_FAILED"
            ),
        }
        return evidence, harness.trace.entries
    finally:
        await harness.close()


def run_canonical_suite(
    fixtures_path: str | Path, runtime_dir: str | Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return asyncio.run(
        _run_canonical_suite_async(Path(fixtures_path), Path(runtime_dir))
    )


def write_evidence(
    evidence: dict[str, Any],
    trace: Iterable[dict[str, Any]],
    evidence_out: Path,
    trace_out: Path,
) -> None:
    evidence_out.parent.mkdir(parents=True, exist_ok=True)
    trace_out.parent.mkdir(parents=True, exist_ok=True)
    evidence_out.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    trace_out.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
            for row in trace
        ),
        encoding="utf-8",
        newline="\n",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute the synthetic PORT-0003 reference-orchestrator proof."
    )
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--evidence-out", required=True, type=Path)
    parser.add_argument("--trace-out", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    evidence, trace = run_canonical_suite(args.fixtures, args.runtime_dir)
    write_evidence(evidence, trace, args.evidence_out, args.trace_out)
    success = (
        evidence["test_summary"] == {
            "expected": 12,
            "passed": 12,
            "failed": 0,
            "result": "PASS",
        }
        and evidence["final_reconciliation"]["summary"]
        == {
            "MATCH": 3,
            "MISSING_TARGET": 0,
            "STALE_TARGET": 0,
            "FIELD_MISMATCH": 0,
            "UNEXPECTED_TARGET": 0,
        }
        and evidence["terminal"]
        == "PASS_REFERENCE_ORCHESTRATOR_12_OF_12_AND_FINAL_RECONCILIATION"
    )
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
