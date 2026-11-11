"""
Tests for Step 1: Order-ID preservation on PayPal sync/webhooks and
order-aware, status-aware duplicate payment detection (without auto-refunds).
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import database  # noqa: E402
import main  # noqa: E402
from models import Base, Transaction  # noqa: E402
from paypal_service import PayPalService  # noqa: E402
from status_engine import StatusEngine, extract_order_id  # noqa: E402


class DuplicateDetectionStep1Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_dups.db")
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

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _seed_txn(
        self,
        pp_id: str,
        *,
        status: str = "COMPLETED",
        amount: float = 99.00,
        currency: str = "USD",
        buyer_email: str = "buyer@example.com",
        merchant_id: str = "MERCH-1",
        description: str = "Wireless headphones",
        metadata: dict | None = None,
        offset_seconds: int = 0,
    ) -> Transaction:
        db = self.TestSession()
        try:
            t = datetime(2026, 11, 10, 12, 0, 0) + timedelta(seconds=offset_seconds)
            txn = Transaction(
                id=f"txn-{pp_id}",
                paypal_transaction_id=pp_id,
                merchant_id=merchant_id,
                amount=amount,
                currency=currency,
                status=status,
                description=description,
                buyer_email=buyer_email,
                metadata_=metadata or {},
                created_at=t,
                updated_at=t,
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)
            return txn
        finally:
            db.close()

    async def test_sync_preserves_existing_paypal_order_id_and_extracts_supplementary(self):
        txn = self._seed_txn(
            "CAP-SYNC-1",
            status="COMPLETED",
            amount=49.99,
            metadata={"paypal_order_id": "ORD-ORIG-777"},
        )
        live_pp = PayPalService("cid", "secret", "https://api-m.sandbox.paypal.com")

        # Sync returns capture WITHOUT supplementary order_id -> must NOT overwrite ORD-ORIG-777 with None
        async def _fake_get_txn_no_order(cap_id):
            return {
                "id": cap_id,
                "status": "COMPLETED",
                "amount": {"value": "49.99", "currency_code": "USD"},
                "mode": "paypal_sandbox",
                "is_simulated": False,
            }

        with (
            patch.object(main, "paypal_service", live_pp),
            patch.object(live_pp, "get_transaction", side_effect=_fake_get_txn_no_order),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                r = await client.post(f"/api/paypal/sync/{txn.paypal_transaction_id}")

        self.assertEqual(r.status_code, 200)
        body_txn = r.json()["transaction"]
        self.assertEqual(body_txn["metadata"]["paypal_order_id"], "ORD-ORIG-777")
        self.assertEqual(extract_order_id(body_txn), "ORD-ORIG-777")

    async def test_webhook_created_capture_retains_supplementary_order_id(self):
        payload = {
            "id": "WH-ORDER-CAP-1",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-WH-ORD-1",
                "status": "COMPLETED",
                "amount": {"value": "55.00", "currency_code": "USD"},
                "supplementary_data": {"related_ids": {"order_id": "PP-ORD-555"}},
            },
        }
        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/webhooks/paypal", json=payload)

        self.assertEqual(resp.status_code, 200)
        db = self.TestSession()
        try:
            txn = db.query(Transaction).filter(Transaction.paypal_transaction_id == "CAP-WH-ORD-1").first()
            self.assertIsNotNone(txn)
            self.assertEqual(txn.metadata_.get("paypal_order_id"), "PP-ORD-555")
            self.assertEqual(extract_order_id(txn), "PP-ORD-555")
        finally:
            db.close()

    async def test_matching_order_ids_high_confidence_without_auto_refund(self):
        t1 = self._seed_txn("CAP-SAME-1", metadata={"order_id": "ORD-SAME-10"}, offset_seconds=0)
        t2 = self._seed_txn("CAP-SAME-2", metadata={"paypal_order_id": "ORD-SAME-10"}, offset_seconds=120)

        with patch.object(main.paypal_service, "issue_refund", new=AsyncMock()) as refund_spy:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                dups = (await client.get(f"/api/transactions/{t1.id}/duplicates")).json()
                summary = (await client.get("/api/analytics/summary")).json()

        refund_spy.assert_not_called()
        self.assertTrue(dups["has_possible_duplicates"])
        self.assertEqual(len(dups["candidates"]), 1)
        c = dups["candidates"][0]
        self.assertEqual(c["paypal_transaction_id"], t2.paypal_transaction_id)
        self.assertEqual(c["classification"], "POSSIBLE_DUPLICATE")
        self.assertEqual(c["confidence"], "HIGH")
        self.assertEqual(c["order_relationship"], "EXACT_MATCH")
        self.assertIn("order_id", c["matching_fields"])
        self.assertIn("COMPLETED captures", c["explanation"])
        self.assertEqual(summary["duplicate_alerts_count"], 1)

    async def test_different_order_ids_retained_as_separate_orders_and_suppress_alert(self):
        # 5 seconds apart, exact same amount & description, DIFFERENT order IDs
        t1 = self._seed_txn(
            "CAP-SEP-1",
            description="Monthly widget box",
            metadata={"order_id": "ORD-20001"},
            offset_seconds=0,
        )
        t2 = self._seed_txn(
            "CAP-SEP-2",
            description="Monthly widget box",
            metadata={"order_id": "ORD-20002"},
            offset_seconds=5,
        )
        # Also confirm split/numbered suffixes (-1 vs -2) are NOT treated as retry suffix
        t3 = self._seed_txn(
            "CAP-SEP-3",
            description="Monthly widget box",
            metadata={"order_id": "ORD-20001-1"},
            offset_seconds=10,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            dups = (await client.get(f"/api/transactions/{t1.id}/duplicates")).json()
            summary = (await client.get("/api/analytics/summary")).json()
            t1_res = (await client.get(f"/api/transactions/{t1.id}")).json()

        self.assertFalse(dups["has_possible_duplicates"])
        self.assertEqual(t1_res["possible_duplicate_count"], 0)
        self.assertEqual(summary["duplicate_alerts_count"], 0)
        self.assertEqual(len(dups["candidates"]), 2)
        for c in dups["candidates"]:
            self.assertEqual(c["classification"], "SEPARATE_ORDERS")
            self.assertEqual(c["confidence"], "LOW")
            self.assertEqual(c["order_relationship"], "DIFFERENT")
            self.assertIn("order IDs differ", c["explanation"])
        self.assertEqual({c["paypal_transaction_id"] for c in dups["candidates"]}, {t2.paypal_transaction_id, t3.paypal_transaction_id})

    def test_conservative_retry_suffix_and_missing_order_id_never_high_from_time_alone(self):
        # Retry suffix ORD-10001 vs ORD-10001-B -> POSSIBLE_DUPLICATE capped at MEDIUM
        r1 = self._seed_txn(
            "CAP-RET-1",
            description="MacBook Pro case",
            metadata={"order_id": "ORD-10001"},
            offset_seconds=0,
        )
        r2 = self._seed_txn(
            "CAP-RET-2",
            description="MacBook Pro case (checkout retry)",
            metadata={"order_id": "ORD-10001-B"},
            offset_seconds=30,
        )
        # Missing order IDs 5 seconds apart -> MEDIUM max, never HIGH
        m1 = self._seed_txn("CAP-NOORD-1", buyer_email="noord@example.com", metadata={}, offset_seconds=0)
        m2 = self._seed_txn("CAP-NOORD-2", buyer_email="noord@example.com", metadata={}, offset_seconds=5)

        retry_dups = self.status_engine.detect_duplicate_transactions(r1, [r1, r2])
        self.assertEqual(len(retry_dups), 1)
        self.assertEqual(retry_dups[0]["classification"], "POSSIBLE_DUPLICATE")
        self.assertEqual(retry_dups[0]["confidence"], "MEDIUM")
        self.assertEqual(retry_dups[0]["order_relationship"], "POSSIBLE_RETRY_VARIANT")
        self.assertIn("possible checkout retry", retry_dups[0]["explanation"])

        missing_dups = self.status_engine.detect_duplicate_transactions(m1, [m1, m2])
        self.assertEqual(len(missing_dups), 1)
        self.assertEqual(missing_dups[0]["classification"], "POSSIBLE_DUPLICATE")
        self.assertEqual(missing_dups[0]["confidence"], "MEDIUM")
        self.assertEqual(missing_dups[0]["order_relationship"], "UNKNOWN")

    def test_status_wording_distinguishes_pending_disputed_and_failed_pairs(self):
        c1 = self._seed_txn("CAP-STAT-COMP", status="COMPLETED", metadata={"order_id": "ORD-ST-1"}, offset_seconds=0)
        p1 = self._seed_txn("CAP-STAT-PEND", status="PENDING", metadata={"order_id": "ORD-ST-1"}, offset_seconds=20)
        d1 = self._seed_txn("CAP-STAT-DISP", status="DISPUTED", metadata={"order_id": "ORD-ST-1"}, offset_seconds=40)
        f1 = self._seed_txn("CAP-STAT-FAIL1", status="FAILED", buyer_email="f@example.com", metadata={"order_id": "ORD-F-1"}, offset_seconds=0)
        f2 = self._seed_txn("CAP-STAT-FAIL2", status="FAILED", buyer_email="f@example.com", metadata={"order_id": "ORD-F-1"}, offset_seconds=10)

        pend_res = self.status_engine.detect_duplicate_transactions(c1, [c1, p1])
        self.assertIn("PENDING", pend_res[0]["explanation"])
        self.assertNotIn("Both payments are COMPLETED", pend_res[0]["explanation"])

        disp_res = self.status_engine.detect_duplicate_transactions(c1, [c1, d1])
        self.assertIn("DISPUTED", disp_res[0]["explanation"])

        fail_res = self.status_engine.detect_duplicate_transactions(f1, [f1, f2])
        self.assertEqual(fail_res[0]["classification"], "RETRY_AFTER_FAILURE")
        self.assertIn("Both payment attempts FAILED", fail_res[0]["explanation"])


if __name__ == "__main__":
    unittest.main()

