"""Local event ledger, attempt log, dead-letter, and replay-lineage API."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class ReserveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(pattern=r"^EVENT-[0-9]{4}$")
    event_version: int = Field(ge=1)
    account_id: str = Field(pattern=r"^ACCOUNT-[0-9]{4}$")
    correlation_id: str = Field(pattern=r"^TRACE-[A-Z0-9-]+$")
    replay: bool = False
    original_correlation_id: str | None = Field(
        default=None, pattern=r"^TRACE-[A-Z0-9-]+$"
    )


class DispositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(pattern=r"^EVENT-[0-9]{4}$")
    event_version: int = Field(ge=1)
    account_id: str = Field(pattern=r"^ACCOUNT-[0-9]{4}$")
    correlation_id: str = Field(pattern=r"^TRACE-[A-Z0-9-]+$")
    original_correlation_id: str | None = Field(
        default=None, pattern=r"^TRACE-[A-Z0-9-]+$"
    )
    disposition: str = Field(min_length=1, max_length=80)
    attempt_count: int = Field(ge=0, le=3)
    final_response: str = Field(min_length=1, max_length=120)
    evidence_timestamp: str = Field(pattern=r"^T\+[0-9]{4}$")


class AttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(pattern=r"^EVENT-[0-9]{4}$")
    account_id: str = Field(pattern=r"^ACCOUNT-[0-9]{4}$")
    correlation_id: str = Field(pattern=r"^TRACE-[A-Z0-9-]+$")
    attempt: int = Field(ge=1, le=3)
    response_status: int
    backoff_ms: int = Field(ge=0, le=200)
    evidence_timestamp: str = Field(pattern=r"^T\+[0-9]{4}$")


class DeadLetterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(pattern=r"^EVENT-[0-9]{4}$")
    account_id: str = Field(pattern=r"^ACCOUNT-[0-9]{4}$")
    correlation_id: str = Field(pattern=r"^TRACE-[A-Z0-9-]+$")
    attempts: int = Field(ge=1, le=3)
    final_response: str
    failure_classification: Literal["TRANSIENT_RETRY_EXHAUSTED"]
    replay_eligibility: Literal["INELIGIBLE_UNTIL_FAULT_CLEARED"]
    evidence_timestamp: str = Field(pattern=r"^T\+[0-9]{4}$")
    event_payload: dict[str, object]


class ReplayStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(pattern=r"^EVENT-[0-9]{4}$")
    replay_correlation_id: str | None = Field(
        default=None, pattern=r"^TRACE-[A-Z0-9-]+$"
    )
    evidence_timestamp: str = Field(pattern=r"^T\+[0-9]{4}$")


def _database_value(database_path: str | Path | None) -> str:
    if database_path is not None:
        return str(database_path)
    return os.environ.get("PORT0003_STATE_DB", ":memory:")


def create_app(
    database_path: str | Path | None = None, api_key: str | None = None
) -> FastAPI:
    expected_api_key = (
        api_key
        or os.environ.get("PORT0003_STATE_API_KEY")
        or os.environ.get("PORT0003_LOCAL_API_KEY")
    )
    app = FastAPI(
        title="PORT-0003 Integration State API",
        version="1.0.0",
        description="Local synthetic event ledger and recovery surface.",
    )
    connection = sqlite3.connect(
        _database_value(database_path), check_same_thread=False, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    lock = threading.RLock()
    with lock:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS event_registry (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                event_version INTEGER NOT NULL,
                original_correlation_id TEXT NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                correlation_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                original_correlation_id TEXT,
                disposition TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                final_response TEXT NOT NULL,
                evidence_timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                correlation_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                backoff_ms INTEGER NOT NULL,
                evidence_timestamp TEXT NOT NULL,
                PRIMARY KEY (correlation_id, attempt)
            );
            CREATE TABLE IF NOT EXISTS dead_letter (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                final_response TEXT NOT NULL,
                failure_classification TEXT NOT NULL,
                replay_eligibility TEXT NOT NULL,
                evidence_timestamp TEXT NOT NULL,
                resolution_status TEXT NOT NULL,
                replay_correlation_id TEXT,
                resolved_at TEXT,
                event_payload_json TEXT NOT NULL
            );
            """
        )
    app.state.connection = connection
    app.state.lock = lock

    def authenticate(provided: str | None) -> None:
        if expected_api_key is None:
            raise HTTPException(status_code=503, detail="Local state API key is not configured")
        if provided is None or not secrets.compare_digest(provided, expected_api_key):
            raise HTTPException(status_code=401, detail="Unauthorized local request")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "synthetic-integration-state"}

    @app.post("/state/reserve")
    def reserve_event(
        request: ReserveRequest,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, object]:
        authenticate(local_api_key)
        with lock:
            current = connection.execute(
                "SELECT * FROM event_registry WHERE event_id = ?", (request.event_id,)
            ).fetchone()
            if request.replay:
                dead = connection.execute(
                    "SELECT * FROM dead_letter WHERE event_id = ?", (request.event_id,)
                ).fetchone()
                if dead is None or dead["replay_eligibility"] != "ELIGIBLE_AFTER_FAULT_CLEARANCE":
                    return {"allowed": False, "disposition": "REPLAY_NOT_ELIGIBLE"}
                if request.original_correlation_id != dead["correlation_id"]:
                    return {"allowed": False, "disposition": "REPLAY_LINEAGE_MISMATCH"}
                connection.execute(
                    "UPDATE event_registry SET state = ? WHERE event_id = ?",
                    ("REPLAYING", request.event_id),
                )
                return {
                    "allowed": True,
                    "disposition": "REPLAY_RESERVED",
                    "original_correlation_id": dead["correlation_id"],
                }

            if current is not None:
                disposition = (
                    "REPLAY_REQUIRED"
                    if current["state"] in {"DEAD_LETTERED", "REPLAY_ELIGIBLE", "REPLAYING"}
                    else "DUPLICATE_EVENT"
                )
                return {
                    "allowed": False,
                    "disposition": disposition,
                    "original_correlation_id": current["original_correlation_id"],
                }

            connection.execute(
                "INSERT INTO event_registry VALUES (?, ?, ?, ?, ?)",
                (
                    request.event_id,
                    request.account_id,
                    request.event_version,
                    request.correlation_id,
                    "RESERVED",
                ),
            )
        return {"allowed": True, "disposition": "RESERVED"}

    @app.post("/state/disposition")
    def record_disposition(
        request: DispositionRequest,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        authenticate(local_api_key)
        with lock:
            connection.execute(
                """
                INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(correlation_id) DO UPDATE SET
                    disposition=excluded.disposition,
                    attempt_count=excluded.attempt_count,
                    final_response=excluded.final_response,
                    evidence_timestamp=excluded.evidence_timestamp
                """,
                (
                    request.correlation_id,
                    request.event_id,
                    request.account_id,
                    request.original_correlation_id,
                    request.disposition,
                    request.attempt_count,
                    request.final_response,
                    request.evidence_timestamp,
                ),
            )
            registry = connection.execute(
                "SELECT event_id FROM event_registry WHERE event_id = ?", (request.event_id,)
            ).fetchone()
            if registry is None:
                connection.execute(
                    "INSERT INTO event_registry VALUES (?, ?, ?, ?, ?)",
                    (
                        request.event_id,
                        request.account_id,
                        request.event_version,
                        request.original_correlation_id or request.correlation_id,
                        request.disposition,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE event_registry SET state = ? WHERE event_id = ?",
                    (request.disposition, request.event_id),
                )
        return {"status": "recorded", "disposition": request.disposition}

    @app.post("/state/attempt")
    def record_attempt(
        request: AttemptRequest,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, object]:
        authenticate(local_api_key)
        with lock:
            connection.execute(
                "INSERT INTO delivery_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request.correlation_id,
                    request.attempt,
                    request.event_id,
                    request.account_id,
                    request.response_status,
                    request.backoff_ms,
                    request.evidence_timestamp,
                ),
            )
        return {"status": "recorded", "attempt": request.attempt}

    @app.post("/state/dead-letter")
    def record_dead_letter(
        request: DeadLetterRequest,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        authenticate(local_api_key)
        with lock:
            connection.execute(
                """
                INSERT INTO dead_letter VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    attempts=excluded.attempts,
                    final_response=excluded.final_response,
                    failure_classification=excluded.failure_classification,
                    replay_eligibility=excluded.replay_eligibility,
                    evidence_timestamp=excluded.evidence_timestamp,
                    resolution_status='OPEN',
                    replay_correlation_id=NULL,
                    resolved_at=NULL,
                    event_payload_json=excluded.event_payload_json
                """,
                (
                    request.event_id,
                    request.account_id,
                    request.correlation_id,
                    request.attempts,
                    request.final_response,
                    request.failure_classification,
                    request.replay_eligibility,
                    request.evidence_timestamp,
                    "OPEN",
                    json.dumps(request.event_payload, sort_keys=True, separators=(",", ":")),
                ),
            )
            connection.execute(
                "UPDATE event_registry SET state = 'DEAD_LETTERED' WHERE event_id = ?",
                (request.event_id,),
            )
        return {"event_id": request.event_id, "status": "OPEN"}

    @app.post("/state/replay-eligible")
    def mark_replay_eligible(
        request: ReplayStateRequest,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        authenticate(local_api_key)
        with lock:
            cursor = connection.execute(
                """
                UPDATE dead_letter SET replay_eligibility='ELIGIBLE_AFTER_FAULT_CLEARANCE'
                WHERE event_id=? AND resolution_status='OPEN'
                """,
                (request.event_id,),
            )
            if cursor.rowcount != 1:
                raise HTTPException(status_code=409, detail="Open dead-letter item not found")
            connection.execute(
                "UPDATE event_registry SET state='REPLAY_ELIGIBLE' WHERE event_id=?",
                (request.event_id,),
            )
        return {"event_id": request.event_id, "status": "REPLAY_ELIGIBLE"}

    @app.post("/state/replay-resolved")
    def resolve_replay(
        request: ReplayStateRequest,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        authenticate(local_api_key)
        if request.replay_correlation_id is None:
            raise HTTPException(status_code=422, detail="Replay correlation is required")
        with lock:
            cursor = connection.execute(
                """
                UPDATE dead_letter SET resolution_status='RESOLVED',
                    replay_correlation_id=?, resolved_at=? WHERE event_id=?
                """,
                (request.replay_correlation_id, request.evidence_timestamp, request.event_id),
            )
            if cursor.rowcount != 1:
                raise HTTPException(status_code=404, detail="Dead-letter item not found")
            connection.execute(
                "UPDATE event_registry SET state='RESOLVED_AFTER_REPLAY' WHERE event_id=?",
                (request.event_id,),
            )
        return {"event_id": request.event_id, "status": "RESOLVED"}

    @app.get("/state/evidence")
    def evidence(
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, list[dict[str, object]]]:
        authenticate(local_api_key)
        with lock:
            deliveries = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM deliveries ORDER BY correlation_id"
                ).fetchall()
            ]
            attempts = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM delivery_attempts ORDER BY correlation_id, attempt"
                ).fetchall()
            ]
            dead_letters = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT event_id, account_id, correlation_id, attempts, final_response,
                           failure_classification, replay_eligibility, evidence_timestamp,
                           resolution_status, replay_correlation_id, resolved_at
                    FROM dead_letter ORDER BY event_id
                    """
                ).fetchall()
            ]
        return {
            "deliveries": deliveries,
            "attempts": attempts,
            "dead_letters": dead_letters,
        }

    return app


app = create_app()
