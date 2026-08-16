"""Synthetic support-entitlement target API for PORT-0003."""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class TargetEntitlement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account_id: str = Field(pattern=r"^ACCOUNT-[0-9]{4}$")
    entitlement_status: Literal["ENABLED", "PENDING", "DISABLED"]
    support_priority: Literal["Normal", "High", "Urgent"]
    coverage_region: Literal["NorthAmerica", "Europe", "AsiaPacific"]
    product_scope: str = Field(pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$", max_length=40)
    source_version: int = Field(ge=1)
    correlation_id: str = Field(pattern=r"^TRACE-[A-Z0-9-]+$")
    last_event_id: str = Field(pattern=r"^EVENT-[0-9]{4}$")
    updated_at: str = Field(pattern=r"^T\+[0-9]{4}$")


class FaultPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    statuses: list[Literal[200, 429, 503]] = Field(min_length=1, max_length=3)


class TamperRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    support_priority: Literal["Normal", "High", "Urgent"]


def _database_value(database_path: str | Path | None) -> str:
    if database_path is not None:
        return str(database_path)
    return os.environ.get("PORT0003_TARGET_DB", ":memory:")


def create_app(
    database_path: str | Path | None = None, api_key: str | None = None
) -> FastAPI:
    """Create an isolated target app; the key is supplied at runtime only."""

    expected_api_key = (
        api_key
        or os.environ.get("PORT0003_TARGET_API_KEY")
        or os.environ.get("PORT0003_LOCAL_API_KEY")
    )
    app = FastAPI(
        title="PORT-0003 Synthetic Target API",
        version="1.0.0",
        description="Local fictional entitlement target; no production system.",
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
            CREATE TABLE IF NOT EXISTS entitlements (
                account_id TEXT PRIMARY KEY,
                entitlement_status TEXT NOT NULL,
                support_priority TEXT NOT NULL,
                coverage_region TEXT NOT NULL,
                product_scope TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                correlation_id TEXT NOT NULL,
                last_event_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applied_events (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fault_plan (
                event_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                response_status INTEGER NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (event_id, ordinal)
            );
            """
        )
    app.state.connection = connection
    app.state.lock = lock

    def authenticate(provided: str | None) -> None:
        if expected_api_key is None:
            raise HTTPException(status_code=503, detail="Local API key is not configured")
        if provided is None or not secrets.compare_digest(provided, expected_api_key):
            raise HTTPException(status_code=401, detail="Unauthorized local request")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "synthetic-target"}

    @app.put("/target/entitlements/{account_id}", response_model=None)
    def upsert_entitlement(
        account_id: str,
        entitlement: TargetEntitlement,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> Any:
        authenticate(local_api_key)
        if account_id != entitlement.account_id:
            raise HTTPException(status_code=400, detail="Path/body account mismatch")

        with lock:
            fault = connection.execute(
                """
                SELECT ordinal, response_status FROM fault_plan
                WHERE event_id = ? AND consumed = 0
                ORDER BY ordinal LIMIT 1
                """,
                (entitlement.last_event_id,),
            ).fetchone()
            if fault is not None:
                connection.execute(
                    "UPDATE fault_plan SET consumed = 1 WHERE event_id = ? AND ordinal = ?",
                    (entitlement.last_event_id, int(fault["ordinal"])),
                )
                status = int(fault["response_status"])
                if status in (429, 503):
                    return JSONResponse(
                        status_code=status,
                        content={
                            "disposition": "MODELED_TRANSIENT_FAILURE",
                            "event_id": entitlement.last_event_id,
                            "response_status": status,
                        },
                    )

            prior_event = connection.execute(
                "SELECT account_id FROM applied_events WHERE event_id = ?",
                (entitlement.last_event_id,),
            ).fetchone()
            if prior_event is not None:
                return {
                    "disposition": "TARGET_DUPLICATE_NO_SIDE_EFFECT",
                    "account_id": str(prior_event["account_id"]),
                    "source_version": entitlement.source_version,
                }

            current = connection.execute(
                "SELECT source_version FROM entitlements WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if current is not None and entitlement.source_version <= int(current["source_version"]):
                return JSONResponse(
                    status_code=409,
                    content={
                        "disposition": "TARGET_OUT_OF_ORDER_REJECTED",
                        "account_id": account_id,
                        "current_source_version": int(current["source_version"]),
                    },
                )

            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO entitlements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        entitlement_status=excluded.entitlement_status,
                        support_priority=excluded.support_priority,
                        coverage_region=excluded.coverage_region,
                        product_scope=excluded.product_scope,
                        source_version=excluded.source_version,
                        correlation_id=excluded.correlation_id,
                        last_event_id=excluded.last_event_id,
                        updated_at=excluded.updated_at
                    """,
                    tuple(entitlement.model_dump().values()),
                )
                connection.execute(
                    "INSERT INTO applied_events VALUES (?, ?, ?)",
                    (
                        entitlement.last_event_id,
                        entitlement.account_id,
                        entitlement.correlation_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "disposition": "TARGET_APPLIED",
            "account_id": account_id,
            "source_version": entitlement.source_version,
        }

    @app.get("/target/entitlements")
    def list_entitlements(
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> list[dict[str, object]]:
        authenticate(local_api_key)
        with lock:
            rows = connection.execute("SELECT * FROM entitlements ORDER BY account_id").fetchall()
        return [dict(row) for row in rows]

    @app.get("/target/entitlements/{account_id}")
    def get_entitlement(
        account_id: str,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, object]:
        authenticate(local_api_key)
        with lock:
            row = connection.execute(
                "SELECT * FROM entitlements WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Synthetic entitlement not found")
        return dict(row)

    @app.post("/target/test/fault-plan/{event_id}")
    def set_fault_plan(
        event_id: str,
        plan: FaultPlan,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, object]:
        authenticate(local_api_key)
        with lock:
            connection.execute("DELETE FROM fault_plan WHERE event_id = ?", (event_id,))
            connection.executemany(
                "INSERT INTO fault_plan VALUES (?, ?, ?, 0)",
                [(event_id, i, status) for i, status in enumerate(plan.statuses, start=1)],
            )
        return {"event_id": event_id, "statuses": plan.statuses}

    @app.delete("/target/test/fault-plan/{event_id}")
    def clear_fault_plan(
        event_id: str,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        authenticate(local_api_key)
        with lock:
            connection.execute("DELETE FROM fault_plan WHERE event_id = ?", (event_id,))
        return {"event_id": event_id, "status": "cleared"}

    @app.post("/target/test/tamper/{account_id}")
    def tamper_target(
        account_id: str,
        request: TamperRequest,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        authenticate(local_api_key)
        with lock:
            cursor = connection.execute(
                "UPDATE entitlements SET support_priority = ? WHERE account_id = ?",
                (request.support_priority, account_id),
            )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Synthetic entitlement not found")
        return {"account_id": account_id, "support_priority": request.support_priority}

    @app.post("/target/test/repair/{account_id}")
    def repair_target(
        account_id: str,
        entitlement: TargetEntitlement,
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        """Apply an explicit reconciliation repair without replaying a source event."""

        authenticate(local_api_key)
        if account_id != entitlement.account_id:
            raise HTTPException(status_code=400, detail="Path/body account mismatch")
        with lock:
            cursor = connection.execute(
                """
                UPDATE entitlements SET entitlement_status=?, support_priority=?,
                    coverage_region=?, product_scope=?, source_version=?, correlation_id=?,
                    last_event_id=?, updated_at=? WHERE account_id=?
                """,
                (
                    entitlement.entitlement_status,
                    entitlement.support_priority,
                    entitlement.coverage_region,
                    entitlement.product_scope,
                    entitlement.source_version,
                    entitlement.correlation_id,
                    entitlement.last_event_id,
                    entitlement.updated_at,
                    account_id,
                ),
            )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Synthetic entitlement not found")
        return {"account_id": account_id, "status": "repaired"}

    @app.post("/target/test/reset")
    def reset(
        local_api_key: str | None = Header(default=None, alias="X-PORT0003-API-Key"),
    ) -> dict[str, str]:
        authenticate(local_api_key)
        with lock:
            connection.execute("DELETE FROM fault_plan")
            connection.execute("DELETE FROM applied_events")
            connection.execute("DELETE FROM entitlements")
        return {"status": "reset"}

    return app


app = create_app()
