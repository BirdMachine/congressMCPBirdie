"""Durable bill-text observation ledger.

Publication dates, first-observed dates and Congress.gov receipt dates are
distinct. A Congress.gov receipt date must come from an explicitly identified
receipt source; it is never inferred from GovInfo metadata or observation time.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS bill_text_observations (
  package_id TEXT PRIMARY KEY,
  congress INTEGER NOT NULL,
  bill_type TEXT NOT NULL,
  bill_number INTEGER NOT NULL,
  version_code TEXT NOT NULL,
  publication_date TEXT,
  first_observed_at TEXT NOT NULL,
  last_observed_at TEXT NOT NULL,
  receipt_date TEXT,
  receipt_source TEXT,
  official_url TEXT,
  source_metadata TEXT NOT NULL DEFAULT '{}',
  CHECK ((receipt_date IS NULL AND receipt_source IS NULL)
      OR (receipt_date IS NOT NULL AND receipt_source IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS bill_text_first_seen_idx
  ON bill_text_observations(first_observed_at);
CREATE INDEX IF NOT EXISTS bill_text_receipt_idx
  ON bill_text_observations(receipt_date);
"""

def default_db_path() -> Path:
    return Path(os.environ.get(
        "CONGRESS_BILL_TEXT_LEDGER_PATH",
        str(Path(os.environ.get("DATA_DIR", "/data")) / "bill_text_receipts.sqlite3"),
    ))

def connect(path: str | Path | None = None) -> sqlite3.Connection:
    destination = Path(path) if path is not None else default_db_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(destination), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    return db

def _utc(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()

def observe(
    db: sqlite3.Connection,
    versions: Iterable[Mapping],
    *,
    observed_at: str | None = None,
) -> dict:
    """Record version packages idempotently, returning only first-seen packages.

    A source failure must not be represented by an empty successful snapshot.
    Caller owns polling, API pagination, and source health reporting.
    """
    timestamp = _utc(observed_at)
    added = []
    seen = 0
    with db:
        for version in versions:
            package_id = str(version["package_id"])
            if not package_id.startswith("BILLS-"):
                raise ValueError("Unexpected GovInfo package_id")
            receipt_date = version.get("receipt_date")
            receipt_source = version.get("receipt_source")
            if bool(receipt_date) != bool(receipt_source):
                raise ValueError("Receipt date requires an explicit source (and vice versa)")
            if receipt_source and receipt_source != "congress.gov/receipt-list":
                raise ValueError("Unverified receipt source")
            before = db.execute(
                "SELECT package_id, receipt_date FROM bill_text_observations WHERE package_id=?",
                (package_id,),
            ).fetchone()
            db.execute(
                """INSERT INTO bill_text_observations
                   (package_id, congress, bill_type, bill_number, version_code,
                    publication_date, first_observed_at, last_observed_at,
                    receipt_date, receipt_source, official_url, source_metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(package_id) DO UPDATE SET
                     last_observed_at=excluded.last_observed_at,
                     publication_date=COALESCE(
                       bill_text_observations.publication_date, excluded.publication_date),
                     receipt_date=COALESCE(
                       bill_text_observations.receipt_date, excluded.receipt_date),
                     receipt_source=COALESCE(
                       bill_text_observations.receipt_source, excluded.receipt_source),
                     official_url=COALESCE(
                       excluded.official_url, bill_text_observations.official_url)""",
                (package_id, int(version["congress"]), str(version["bill_type"]),
                 int(version["bill_number"]), str(version["version_code"]),
                 version.get("publication_date"), timestamp, timestamp,
                 receipt_date, receipt_source, version.get("official_url"),
                 json.dumps(version.get("source_metadata", {}), sort_keys=True)),
            )
            seen += 1
            if before is None:
                added.append(package_id)
    return {"observed": seen, "new_packages": added, "first_observed_at": timestamp}

def report(db: sqlite3.Connection, *, first_seen_since: str | None = None,
           receipt_date: str | None = None, limit: int = 100) -> dict:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be 1..1000")
    where, args = [], []
    if first_seen_since:
        where.append("first_observed_at >= ?")
        args.append(_utc(first_seen_since))
    if receipt_date:
        where.append("receipt_date = ?")
        args.append(receipt_date)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = db.execute(
        "SELECT * FROM bill_text_observations" + clause +
        " ORDER BY first_observed_at DESC, package_id LIMIT ?", (*args, limit),
    ).fetchall()
    return {"results": [dict(row) for row in rows], "count": len(rows),
            "date_semantics": {
                "publication_date": "GovInfo version publication date",
                "first_observed_at": "first durable local observation, not receipt",
                "receipt_date": "only explicit Congress.gov receipt-list evidence",
            }}
