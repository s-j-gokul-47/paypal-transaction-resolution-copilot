"""
Heuristic grounding audit tests verifying all 8 transaction scenarios against
StatusEngine.explain_transaction_state and AIService.generate_support_summary
without OpenAI configured (deterministic record-grounded baseline).
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import database  # noqa: E402
import main  # noqa: E402
from ai_service import AIService  # noqa: E402
from models import Base, Dispute, Transaction, TransactionEvent  # noqa: E402
from paypal_service import PayPalService  # noqa: E402
from status_engine import StatusEngine  # noqa: E402


class HeuristicGroundingAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_grounding.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        Base.metadata.create_all(bind=self.engine)
        self.TestSession = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def _override_get_db():
            db = self.TestSession()
            try:
                yield db
            finally:
                db.close()

        main.app.dependency_overrides[database.get_db] = _override_get_db
        main.app.dependency_overrides[main.get_db] = _override_get_db
        self.status_engine = StatusEngine()
        self.heuristic_ai = AIService(api_key="")

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _create_txn(
        self,
        pp_id: str,
        *,
        status: str = "COMPLETED",
        amount: float = 50.00,
        currency: str = "USD",
        buyer_email: str | None = "buyer@example.com",
        metadata: dict | None = None,
        with_event: bool = True,
    ) -> Transaction:
        db = self.TestSession()
        try:
            now = datetime.utcnow()
            txn = Transaction(
                id=f"txn-{pp_id}",
                paypal_transaction_id=pp_id,
                merchant_id="MERCH-AUDIT",
                amount=amount,
                currency=currency,
                status=status,
                buyer_email=buyer_email,
                metadata_=metadata or {},
                created_at=now,
                updated_at=now,
            )
            db.add(txn)
            db.flush()
            if with_event:
                db.add(
                    TransactionEvent(
                        id=f"ev-{pp_id}",
                        transaction_id=txn.id,
                        event_type="TRANSACTION_RECORDED",
                        title=f"Recorded {status}",
                        description=f"Initial {status} record for {amount:.2f} {currency}",
                        source="database",
                        occurred_at=now,
                    )
                )
            db.commit()
            db.refresh(txn)
            return txn
        finally:
            db.close()

    async def test_1_completed_payment_reports_amount_status_and_order(self):
        txn = self._create_txn(
            "CAP-AUD-COMP",
            status="COMPLETED",
            amount=42.50,
            metadata={"paypal_order_id": "ORD-AUD-42"},
        )
        with patch.object(main, "ai_service", self.heuristic_ai):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                exp = (await client.post(f"/api/transactions/{txn.id}/explain")).json()
                note = (
                    await client.post(
                        f"/api/transactions/{txn.id}/support-summary",
                        json={"audience": "internal"},
                    )
                ).json()

        self.assertEqual(exp["mode"], "heuristic")
        self.assertEqual(exp["status"], "COMPLETED")
        self.assertIn("42.50 USD", exp["plain_english_summary"])
        self.assertIn("COMPLETED", exp["plain_english_summary"])
        self.assertIn("ORD-AUD-42", exp["plain_english_summary"])
        self.assertIn("42.50 USD", note["summary"])
        self.assertIn("ORD-AUD-42", note["summary"])

    async def test_2_pending_payment_never_claims_success(self):
        txn_with_reason = self._create_txn(
            "CAP-AUD-PEND-1",
            status="PENDING",
            amount=120.00,
            metadata={"pending_reason": "ECHECK"},
        )
        txn_without_reason = self._create_txn(
            "CAP-AUD-PEND-2",
            status="PENDING",
            amount=75.00,
            metadata={},
        )
        with patch.object(main, "ai_service", self.heuristic_ai):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                exp1 = (await client.post(f"/api/transactions/{txn_with_reason.id}/explain")).json()
                exp2 = (await client.post(f"/api/transactions/{txn_without_reason.id}/explain")).json()
                buyer_note = (
                    await client.post(
                        f"/api/transactions/{txn_without_reason.id}/support-summary",
                        json={"audience": "buyer"},
                    )
                ).json()

        for exp in (exp1, exp2):
            text = (exp["headline"] + " " + exp["plain_english_summary"]).lower()
            self.assertEqual(exp["status"], "PENDING")
            self.assertIn("not succeeded", text)
            self.assertNotIn("captured and settled", text)

        self.assertIn("ECHECK", exp1["plain_english_summary"])
        self.assertIn("metadata.pending_reason", exp2["plain_english_summary"])
        self.assertNotIn("echeck", exp2["plain_english_summary"].lower())
        self.assertIn("has not completed yet", buyer_note["summary"].lower())

    async def test_3_full_refund_reports_refunded_amount_and_zero_remaining(self):
        txn = self._create_txn(
            "CAP-AUD-REF",
            status="REFUNDED",
            amount=19.99,
            metadata={
                "refund_id": "REF-AUD-1999",
                "total_refunded": 19.99,
                "remaining_refundable": 0.0,
            },
        )
        with patch.object(main, "ai_service", self.heuristic_ai):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                exp = (await client.post(f"/api/transactions/{txn.id}/explain")).json()
                note = (
                    await client.post(
                        f"/api/transactions/{txn.id}/support-summary",
                        json={"audience": "internal"},
                    )
                ).json()

        self.assertIn("19.99 USD refunded", exp["plain_english_summary"])
        self.assertIn("0.00 USD remaining refundable", exp["plain_english_summary"])
        self.assertIn("REF-AUD-1999", exp["plain_english_summary"])
        self.assertIn("19.99 USD refunded; 0.00 USD remaining refundable", note["summary"])

    async def test_4_partial_refund_reports_original_refunded_and_remaining(self):
        txn = self._create_txn(
            "CAP-AUD-PART",
            status="COMPLETED",
            amount=24.99,
            metadata={
                "refund_id": "REF-PART-10",
                "total_refunded": 10.00,
                "remaining_refundable": 14.99,
                "partial_refund": True,
                "refunds": [
                    {
                        "refund_id": "REF-PART-10",
                        "amount": 10.00,
                        "currency": "USD",
                        "status": "COMPLETED",
                    }
                ],
            },
        )
        with patch.object(main, "ai_service", self.heuristic_ai):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                exp = (await client.post(f"/api/transactions/{txn.id}/explain")).json()
                note = (
                    await client.post(
                        f"/api/transactions/{txn.id}/support-summary",
                        json={"audience": "internal"},
                    )
                ).json()

        self.assertEqual(exp["status"], "COMPLETED")
        self.assertIn("24.99 USD", exp["plain_english_summary"])
        self.assertIn("10.00 USD partially refunded", exp["plain_english_summary"])
        self.assertIn("14.99 USD remaining refundable", exp["plain_english_summary"])
        self.assertIn("10.00 USD refunded; 14.99 USD remaining refundable", note["summary"])

    async def test_5_duplicate_refund_webhooks_cause_no_duplicate_accounting(self):
        txn = self._create_txn("CAP-AUD-DUPWH", status="COMPLETED", amount=30.00)
        wh1 = {
            "id": "WH-AUD-DUP-1",
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource_type": "refund",
            "resource": {
                "id": "REF-AUD-SINGLE",
                "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "12.00"},
                "seller_payable_breakdown": {
                    "total_refunded_amount": {"currency_code": "USD", "value": "12.00"}
                },
                "links": [{"rel": "up", "href": "https://api.sandbox.paypal.com/v2/payments/captures/CAP-AUD-DUPWH"}],
            },
        }
        wh_diff_event_same_refund = dict(wh1, id="WH-AUD-DUP-2")

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
            patch.object(main, "ai_service", self.heuristic_ai),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                await client.post("/api/webhooks/paypal", json=wh1)
                await client.post("/api/webhooks/paypal", json=wh1)
                await client.post("/api/webhooks/paypal", json=wh_diff_event_same_refund)
                exp = (await client.post(f"/api/transactions/{txn.id}/explain")).json()

        self.assertIn("12.00 USD partially refunded", exp["plain_english_summary"])
        self.assertIn("18.00 USD remaining refundable", exp["plain_english_summary"])
        self.assertNotIn("24.00 USD", exp["plain_english_summary"])
        self.assertNotIn("36.00 USD", exp["plain_english_summary"])

    async def test_6_stale_webhook_explains_older_event_was_ignored(self):
        txn = self._create_txn("CAP-AUD-STALE", status="REFUNDED", amount=20.00, metadata={"refund_id": "REF-1"})
        stale_completed = {
            "id": "WH-AUD-STALE-1",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAP-AUD-STALE", "amount": {"currency_code": "USD", "value": "20.00"}},
        }
        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
            patch.object(main, "ai_service", self.heuristic_ai),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                r = await client.post("/api/webhooks/paypal", json=stale_completed)
                self.assertEqual(r.json()["result"]["status"], "ignored_stale_transition")
                exp = (await client.post(f"/api/transactions/{txn.id}/explain")).json()
                note = (
                    await client.post(
                        f"/api/transactions/{txn.id}/support-summary",
                        json={"audience": "internal"},
                    )
                ).json()

        self.assertEqual(exp["status"], "REFUNDED")
        self.assertIn("out-of-order webhook", exp["plain_english_summary"].lower())
        self.assertIn("ignored", exp["plain_english_summary"].lower())
        self.assertTrue(any("ignored" in c.lower() for c in exp["likely_causes"]))
        self.assertIn("out-of-order webhooks ignored: 1", note["summary"].lower())

    async def test_7_disputed_transaction_reports_status_and_evidence_or_discloses_missing(self):
        txn_with_disp = self._create_txn("CAP-AUD-DISP-1", status="DISPUTED", amount=89.99)
        txn_without_disp = self._create_txn("CAP-AUD-DISP-2", status="DISPUTED", amount=40.00)

        db = self.TestSession()
        try:
            db.add(
                Dispute(
                    id="disp-aud-1",
                    transaction_id=txn_with_disp.id,
                    paypal_dispute_id="PP-D-AUD-1",
                    reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    status="UNDER_REVIEW",
                    evidence_submitted=True,
                )
            )
            db.commit()
        finally:
            db.close()

        with patch.object(main, "ai_service", self.heuristic_ai):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                exp1 = (await client.post(f"/api/transactions/{txn_with_disp.id}/explain")).json()
                exp2 = (await client.post(f"/api/transactions/{txn_without_disp.id}/explain")).json()

        self.assertIn("UNDER_REVIEW", exp1["plain_english_summary"])
        self.assertIn("evidence submitted", exp1["plain_english_summary"])
        self.assertIn("PP-D-AUD-1", exp1["plain_english_summary"])
        self.assertIn("unavailable", exp2["plain_english_summary"].lower())
        self.assertIn("dispute_records", exp2["plain_english_summary"])

    def test_8_missing_transaction_data_discloses_unavailable_without_inventing(self):
        sparse_failed = {
            "id": "txn-sparse-fail",
            "paypal_transaction_id": None,
            "status": "FAILED",
            "amount": None,
            "currency": None,
            "buyer_email": None,
            "created_at": None,
            "metadata": {},
        }
        exp = self.status_engine.explain_transaction_state(sparse_failed, [], [], [])

        self.assertIn("unavailable amount", exp["plain_english_summary"].lower())
        self.assertNotIn("0.00", exp["plain_english_summary"])
        self.assertIn("unavailable", exp["likely_causes"][0].lower())
        for missing_field in ("paypal_transaction_id", "amount", "currency", "buyer_email", "timeline_events", "metadata.error_code"):
            self.assertIn(missing_field, exp["plain_english_summary"])
        self.assertNotIn("created_at", exp["verified_fields_used"])
        self.assertNotIn("amount", exp["verified_fields_used"])


if __name__ == "__main__":
    unittest.main()

