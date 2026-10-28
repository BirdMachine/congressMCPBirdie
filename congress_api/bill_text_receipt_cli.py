"""Import successful GovInfo version discoveries into the durable ledger.

Input is a JSON array of normalized version packages, or an object with
`results` / `versions`. Never treat an API error as an empty successful scan.
"""
import argparse
import json
import sys
from .bill_text_receipt_ledger import connect, observe, report

def main(argv=None):
    parser = argparse.ArgumentParser(description="Bill-text observation ledger")
    parser.add_argument("command", choices=("ingest", "report"))
    parser.add_argument("--db")
    parser.add_argument("--since", help="UTC ISO timestamp for first observation")
    parser.add_argument("--receipt-date", help="Verified Congress.gov receipt date")
    args = parser.parse_args(argv)
    with connect(args.db) as db:
        if args.command == "ingest":
            payload = json.load(sys.stdin)
            if isinstance(payload, dict):
                if payload.get("error") or payload.get("success") is False:
                    parser.error("Source reported an error; refusing to ingest")
                versions = payload.get("results", payload.get("versions"))
                if versions is None:
                    parser.error("Expected results or versions array")
            elif isinstance(payload, list):
                versions = payload
            else:
                parser.error("Expected a JSON array or result object")
            if not isinstance(versions, list):
                parser.error("Versions must be a JSON array")
            output = observe(db, versions)
        else:
            output = report(db, first_seen_since=args.since, receipt_date=args.receipt_date)
    print(json.dumps(output, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
