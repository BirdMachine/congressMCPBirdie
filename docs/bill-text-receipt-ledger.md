# Durable bill-text receipt ledger (first milestone)

The module `congress_api.bill_text_receipt_ledger` provides a persistent,
idempotent SQLite ledger keyed by **GovInfo version package ID**.

## Dates must remain separate

- `publication_date`: date reported by GovInfo for that version.
- `first_observed_at`: first time *this installation* successfully saw it.
- `receipt_date`: **nullable**; only populated with explicit evidence from
  Congress.gov's receipt list, never guessed from a publication or ingestion date.

If the Congress.gov receipt list is robots-blocked, keep `receipt_date=null`.
Report newly observed packages as *newly observed*, not *received today*.

## Usage

```python
from congress_api.bill_text_receipt_ledger import connect, observe, report
with connect() as db:
    observe(db, [{
        "package_id": "BILLS-119hr10905ih",
        "congress": 119, "bill_type": "hr", "bill_number": 10905,
        "version_code": "ih", "publication_date": "2026-10-22",
        "official_url": "https://www.congress.gov/bill/119th-congress/house-bill/10905",
    }])
    print(report(db, first_seen_since="2026-10-27T00:00:00Z"))
```

The default path is `/data/bill_text_receipts.sqlite3`, configurable via
`CONGRESS_BILL_TEXT_LEDGER_PATH` or `DATA_DIR`. **Mount /data as a persistent
volume** in hosted deployments; otherwise container restarts reset the ledger.

## Integration still required

1. Wire `observe` into the *existing paginated* GovInfo version discovery
   pipeline **after successful completion**, not after an API failure.
2. Expose a read-only MCP operation for `report`, with cursor-based pagination
   for large datasets and explicit source-health metadata.
3. Add scheduled polling, rate-limit/backoff handling, and persistent checkpoints
   for complete scans (not just the first 50 candidates).
4. Add an authenticated Congress.gov receipt-list adapter if permitted and
   available; populate `receipt_date` only from direct evidence.
5. Migrate or seed historical version IDs as a baseline so the first run does
   not mislabel the entire historical corpus as newly received.
6. Add concurrency and deployment-level tests before relying on it for daily
   production reporting.

This PR implements the **storage and reconciliation foundation**, not a
finished scheduled collector or a bypass of robots.txt.
