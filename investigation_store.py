"""
PostMortem.ai — Investigation Store
=====================================
SQLite-backed store (WAL mode) for persisting investigation outcomes.
All queries use parameterized form — no f-strings in SQL.
"""

import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Database file path — can be overridden via env var
_DB_PATH = os.getenv("DB_PATH", "investigations.db")

# Module-level lock so aiosqlite-style operations serialise correctly
_lock = asyncio.Lock()


def _get_connection() -> sqlite3.Connection:
    """Open a new SQLite connection with WAL mode enabled."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Create the investigations table if it does not exist."""
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS investigations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id      TEXT    NOT NULL,
                investigation_start TEXT NOT NULL,
                investigation_end   TEXT,
                root_cause       TEXT,
                confidence       INTEGER,
                agent_model_used TEXT,
                token_count      INTEGER DEFAULT 0,
                hypotheses_tested INTEGER DEFAULT 0,
                hypotheses_rejected INTEGER DEFAULT 0,
                created_at       TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


async def save_investigation(
    incident_id: str,
    investigation_start: str,
    investigation_end: str,
    root_cause: str | None,
    confidence: int | None,
    agent_model_used: str | None = None,
    token_count: int = 0,
    hypotheses_tested: int = 0,
    hypotheses_rejected: int = 0,
) -> int:
    """
    Persist an investigation record.

    Returns the new row ID.
    All SQL uses parameterized queries — no string interpolation.
    Serialized via asyncio.Lock to prevent concurrent write races.
    SQLite work runs in a thread so the event loop is not blocked.
    """
    params = (
        incident_id,
        investigation_start,
        investigation_end,
        root_cause,
        confidence,
        agent_model_used,
        token_count,
        hypotheses_tested,
        hypotheses_rejected,
    )

    def _sync_write() -> int:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO investigations
                    (incident_id, investigation_start, investigation_end,
                     root_cause, confidence, agent_model_used, token_count,
                     hypotheses_tested, hypotheses_rejected)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    async with _lock:
        return await asyncio.to_thread(_sync_write)


def get_history(limit: int = 20) -> list[dict]:
    """
    Return the last `limit` investigations ordered by most recent first.

    Parameterized to prevent SQL injection.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, incident_id, investigation_start, investigation_end,
                   root_cause, confidence, agent_model_used, token_count,
                   hypotheses_tested, hypotheses_rejected, created_at
            FROM investigations
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_metrics_summary() -> dict[str, Any]:
    """
    Aggregate investigation statistics for the /metrics endpoint.

    Returns counts, average confidence, average duration, and top root causes.
    """
    conn = _get_connection()
    try:
        # Basic stats
        stats = conn.execute(
            """
            SELECT
                COUNT(*) as total_investigations,
                AVG(confidence) as avg_confidence,
                MIN(confidence) as min_confidence,
                MAX(confidence) as max_confidence
            FROM investigations
            WHERE confidence IS NOT NULL
            """
        ).fetchone()

        # Confidence distribution buckets
        dist = conn.execute(
            """
            SELECT
                SUM(CASE WHEN confidence >= 90 THEN 1 ELSE 0 END) as high_90,
                SUM(CASE WHEN confidence >= 80 AND confidence < 90 THEN 1 ELSE 0 END) as med_80,
                SUM(CASE WHEN confidence < 80 THEN 1 ELSE 0 END) as low_under80
            FROM investigations
            WHERE confidence IS NOT NULL
            """
        ).fetchone()

        # Most investigated incidents
        top_incidents = conn.execute(
            """
            SELECT incident_id, COUNT(*) as run_count
            FROM investigations
            GROUP BY incident_id
            ORDER BY run_count DESC
            LIMIT 5
            """
        ).fetchall()

        return {
            "total_investigations": dict(stats).get("total_investigations", 0),
            "avg_confidence": round(dict(stats).get("avg_confidence") or 0, 1),
            "min_confidence": dict(stats).get("min_confidence"),
            "max_confidence": dict(stats).get("max_confidence"),
            "confidence_distribution": {
                "high_90_plus": dict(dist).get("high_90") or 0,
                "medium_80_89": dict(dist).get("med_80") or 0,
                "low_under_80": dict(dist).get("low_under80") or 0,
            },
            "top_incidents": [
                {"incident_id": row["incident_id"],
                    "run_count": row["run_count"]}
                for row in top_incidents
            ],
        }
    finally:
        conn.close()


# init_db() is called from the FastAPI lifespan in main.py.
# Do NOT call it here — the DB_PATH directory may not be mounted yet at import
# time (named Docker volume), and blocking SQLite setup belongs in the startup
# lifecycle, not at module import.
