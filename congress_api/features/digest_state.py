"""Small durable observation ledger for delayed Congress.gov action indexing.

This records evidence identities, not political status or API credentials.
An initial historical snapshot establishes a baseline; only subsequent
previously unseen historical actions can be called newly observed.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def observe(legislation_id: str, actions: list[dict], since: str) -> tuple[bool, set[str], dict[str, str]]:
    path = Path(os.getenv("BIRDIE_DIGEST_STATE", "~/.cache/congressmcp/digest.sqlite3")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    identities = {action_key(a) for a in actions}
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("CREATE TABLE IF NOT EXISTS bills (id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS events (bill TEXT, identity TEXT, observed TEXT, eligible INTEGER, "
                   "PRIMARY KEY (bill, identity))")
        # Serialize baseline reads/writes so concurrent runs cannot both claim novelty.
        db.execute("BEGIN IMMEDIATE")
        known_bill = db.execute("SELECT 1 FROM bills WHERE id=?", (legislation_id,)).fetchone() is not None
        existing = {row[0] for row in db.execute("SELECT identity FROM events WHERE bill=?", (legislation_id,))}
        db.execute("INSERT OR IGNORE INTO bills VALUES (?)", (legislation_id,))
        db.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
                       [(legislation_id, key, now, int(known_bill)) for key in identities])
        replay = {row[0] for row in db.execute(
            "SELECT identity FROM events WHERE bill=? AND eligible=1 AND julianday(observed)>=julianday(?)",
            (legislation_id, since))}
        observed = dict(db.execute("SELECT identity, observed FROM events WHERE bill=?", (legislation_id,)))
    return known_bill, (identities - existing) | replay, observed


def action_key(action: dict) -> str:
    evidence = {key: action.get(key) for key in ("actionDate", "actionCode", "text", "sourceSystem")}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
