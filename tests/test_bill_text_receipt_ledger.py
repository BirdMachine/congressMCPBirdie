import sqlite3
import tempfile
import unittest
from pathlib import Path

from congress_api.bill_text_receipt_ledger import connect, observe, report

V = dict(package_id="BILLS-119hr10905ih", congress=119, bill_type="hr",
         bill_number=10905, version_code="ih", publication_date="2026-10-22",
         official_url="https://www.congress.gov/bill/119th-congress/house-bill/10905")

class LedgerTests(unittest.TestCase):
    def test_durable_idempotent_and_dates_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with connect(path) as db:
                self.assertEqual(observe(db, [V], observed_at="2026-10-27T12:00:00Z")["new_packages"],
                                 [V["package_id"]])
                self.assertEqual(observe(db, [V], observed_at="2026-10-28T12:00:00Z")["new_packages"], [])
            with connect(path) as db:
                item = report(db)["results"][0]
                self.assertEqual(item["publication_date"], "2026-10-22")
                self.assertEqual(item["first_observed_at"], "2026-10-27T12:00:00+00:00")
                self.assertIsNone(item["receipt_date"])
                self.assertEqual(item["last_observed_at"], "2026-10-28T12:00:00+00:00")

    def test_verified_receipt_can_arrive_later(self):
        with connect(":memory:") as db:
            observe(db, [V])
            observe(db, [{**V, "receipt_date": "2026-10-28",
                          "receipt_source": "congress.gov/receipt-list"}])
            self.assertEqual(report(db, receipt_date="2026-10-28")["count"], 1)

    def test_refuses_inferred_receipt(self):
        with connect(":memory:") as db:
            with self.assertRaises(ValueError):
                observe(db, [{**V, "receipt_date": "2026-10-28"}])
            self.assertEqual(report(db)["count"], 0)

if __name__ == "__main__":
    unittest.main()
