"""Read-only MCP access to durable bill-text observations."""
from typing import Optional
from ..mcp_app import mcp
from ..bill_text_receipt_ledger import connect, report

@mcp.tool("bill_text_observations", title="Persisted bill-text observations and receipt evidence")
async def bill_text_observations(
    first_seen_since: Optional[str] = None,
    receipt_date: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """Return observed text packages, keeping publication/observation/receipt dates separate.

    This reads the persisted local ledger. A zero means zero recorded packages,
    NOT zero Congress.gov receipts or zero published texts. The ledger must be
    populated by a separate collector, and its storage must survive restarts.
    """
    with connect() as db:
        result = report(db, first_seen_since=first_seen_since,
                        receipt_date=receipt_date, limit=limit)
    result["coverage_note"] = (
        "Local observations only. Not an exhaustive Congress.gov receipt-date feed. "
        "Empty ledger may indicate that collection has not run."
    )
    return result
