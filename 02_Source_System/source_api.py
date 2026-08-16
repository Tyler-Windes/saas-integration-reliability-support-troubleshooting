"""Synthetic source-system API for PORT-0003.

The service owns only fictional account state.  It uses a caller-supplied or
task-local SQLite database and never contains a real credential or customer
record.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


EventType = Literal["account.created", "account.updated", "account.suspended"]
AccountStatus = Literal["Active", "Suspended", "Closed"]
SupportTier = Literal["Standard", "Priority", "Premium"]
ContractState = Literal["Current", "Pending", "Expired"]
Region = Literal["NorthAmerica", "Europe", "AsiaPacific"]


class SourceEvent(BaseModel):
    """Exact synthetic event contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str = Field(pattern=r"^EVENT-[0-9]{4}$")
    event_version: int = Field(ge=1)
    event_type: EventType
    account_id: str = Field(pattern=r"^ACCOUNT-[0-9]{4}$")
    account_status: AccountStatus
    support_tier: SupportTier
    region: Region
    product_family: str = Field(min_length=1, max_length=40)
    contract_state: ContractState
    occurred_at: str = Field(pattern=r"^T\+[0-9]{4}$")


class SourceApplyResult(BaseModel):
    disposition: Literal["SOURCE_APPLIED", "SOURCE_DUPLICATE", "SOURCE_OUT_OF_ORDER"]
    account_id: str
    source_version: int
    last_event_id: str


def _database_value(database_path: str | Path | None) -> str:
    if database_path is not None:
        return str(database_path)
    return os.environ.get("PORT0003_SOURCE_DB", ":memory:")


def create_app(database_path: str | Path | None = None) -> FastAPI:
    """Create an isolated source API backed by one SQLite connection."""

    app = FastAPI(
        title="PORT-0003 Synthetic Source API",
        version="1.0.0",
        description="Local fictional account-event source; no real customer data.",
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
            CREATE TABLE IF NOT EXISTS source_accounts (
                account_id TEXT PRIMARY KEY,
                account_status TEXT NOT NULL,
                support_tier TEXT NOT NULL,
                region TEXT NOT NULL,
                product_family TEXT NOT NULL,
                contract_state TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                last_event_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_event_ledger (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                event_version INTEGER NOT NULL,
                disposition TEXT NOT NULL
            );
            """
        )
    app.state.connection = connection
    app.state.lock = lock

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "synthetic-source"}

    @app.post("/source/accounts/apply", response_model=SourceApplyResult)
    def apply_event(event: SourceEvent) -> SourceApplyResult:
        with lock:
            prior_event = connection.execute(
                "SELECT disposition FROM source_event_ledger WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            current = connection.execute(
                "SELECT source_version, last_event_id FROM source_accounts WHERE account_id = ?",
                (event.account_id,),
            ).fetchone()
            if prior_event is not None:
                if current is None:
                    raise HTTPException(status_code=409, detail="Source ledger/account inconsistency")
                return SourceApplyResult(
                    disposition="SOURCE_DUPLICATE",
                    account_id=event.account_id,
                    source_version=int(current["source_version"]),
                    last_event_id=str(current["last_event_id"]),
                )

            if current is not None and event.event_version <= int(current["source_version"]):
                connection.execute(
                    "INSERT INTO source_event_ledger VALUES (?, ?, ?, ?)",
                    (event.event_id, event.account_id, event.event_version, "SOURCE_OUT_OF_ORDER"),
                )
                return SourceApplyResult(
                    disposition="SOURCE_OUT_OF_ORDER",
                    account_id=event.account_id,
                    source_version=int(current["source_version"]),
                    last_event_id=str(current["last_event_id"]),
                )

            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO source_accounts (
                        account_id, account_status, support_tier, region, product_family,
                        contract_state, source_version, last_event_id, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        account_status=excluded.account_status,
                        support_tier=excluded.support_tier,
                        region=excluded.region,
                        product_family=excluded.product_family,
                        contract_state=excluded.contract_state,
                        source_version=excluded.source_version,
                        last_event_id=excluded.last_event_id,
                        occurred_at=excluded.occurred_at
                    """,
                    (
                        event.account_id,
                        event.account_status,
                        event.support_tier,
                        event.region,
                        event.product_family,
                        event.contract_state,
                        event.event_version,
                        event.event_id,
                        event.occurred_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO source_event_ledger VALUES (?, ?, ?, ?)",
                    (event.event_id, event.account_id, event.event_version, "SOURCE_APPLIED"),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return SourceApplyResult(
                disposition="SOURCE_APPLIED",
                account_id=event.account_id,
                source_version=event.event_version,
                last_event_id=event.event_id,
            )

    @app.get("/source/accounts")
    def list_accounts() -> list[dict[str, object]]:
        with lock:
            rows = connection.execute(
                "SELECT * FROM source_accounts ORDER BY account_id"
            ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/source/accounts/{account_id}")
    def get_account(account_id: str) -> dict[str, object]:
        with lock:
            row = connection.execute(
                "SELECT * FROM source_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Synthetic account not found")
        return dict(row)

    @app.post("/source/test/reset")
    def reset() -> dict[str, str]:
        with lock:
            connection.execute("DELETE FROM source_event_ledger")
            connection.execute("DELETE FROM source_accounts")
        return {"status": "reset"}

    return app


app = create_app()

