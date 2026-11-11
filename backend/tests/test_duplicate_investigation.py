"""
Automated tests for Step 2 Part 1: Duplicate Payment Investigation & Review Workflow.
"""
import asyncio
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
from models import Base, DuplicateReview, Transaction, TransactionEvent  # noqa: E402
from paypal_service import PayPalService  # noqa: E402


class DuplicateInvestigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_dup_investigation.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
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

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _seed_txn(
        self,
        txn_id: str,
        pp_id: str,
        *,
        created_at: datetime,
        order_id: str = "ORD-10001",
        buyer_email: str = "buyer@example.com",
        merchant_id: str = "MERCH-INV-01",
        amount: float = 49.99,
        currency: str = "USD",
        status: str = "COMPLETED",
        description: str = "Pro subscription",
    ) -> Transaction:
        db = self.TestSession()
        try:
            txn = Transaction(
                id=txn_id,
                paypal_transaction_id=pp_id,
                merchant_id=merchant_id,
                buyer_email=buyer_email,
                amount=amount,
                currency=currency,
                status=status,
                description=description,
                metadata_={"order_id": order_id} if order_id else {},
                created_at=created_at,
                updated_at=created_at,
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)
            return txn
        finally:
            db.close()

    async def test_reverse_order_urls_use_single_review_row_and_chronological_roles(self):
        base = datetime(2026, 11, 10, 12, 0, 0)
        # Early txn has lexicographically LARGER id so pair_low_id != original_transaction_id
        early = self._seed_txn(
            "txn-z-early",
            "CAP-Z-EARLY",
            created_at=base,
            order_id="ORD-500",
            description="Widget Pack",
        )
        late = self._seed_txn(
            "txn-a-late",
            "CAP-A-LATE",
            created_at=base + timedelta(minutes=3),
            order_id="ORD-500",
            description="Widget Pack",
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            # Submit review using (late, early) order in URL
            r_post = await client.post(
                f"/api/transactions/{late.id}/duplicates/{early.id}/review",
                json={
                    "decision": "CONFIRMED_DUPLICATE",
                    "reviewer": "analyst-1@example.com",
                    "notes": "Verified same order double charge",
                },
            )
            self.assertEqual(r_post.status_code, 200, r_post.text)
            post_body = r_post.json()
            self.assertEqual(post_body["pair_low_id"], "txn-a-late")
            self.assertEqual(post_body["pair_high_id"], "txn-z-early")
            self.assertEqual(post_body["original_transaction_id"], "txn-z-early")
            self.assertEqual(post_body["extra_transaction_id"], "txn-a-late")
            self.assertEqual(post_body["decision"], "CONFIRMED_DUPLICATE")
            self.assertFalse(post_body["reviewer_verified"])

            # Investigate in both URL orders
            r_inv_1 = await client.get(f"/api/transactions/{early.id}/duplicates/{late.id}/investigation")
            r_inv_2 = await client.get(f"/api/transactions/{late.id}/duplicates/{early.id}/investigation")
            self.assertEqual(r_inv_1.status_code, 200, r_inv_1.text)
            self.assertEqual(r_inv_2.status_code, 200, r_inv_2.text)

            b1 = r_inv_1.json()
            b2 = r_inv_2.json()
            self.assertEqual(b1["pair_low_id"], "txn-a-late")
            self.assertEqual(b1["pair_high_id"], "txn-z-early")
            self.assertEqual(b1["original_transaction"]["id"], "txn-z-early")
            self.assertEqual(b1["original_transaction"]["role"], "ORIGINAL")
            self.assertEqual(b1["extra_candidate"]["id"], "txn-a-late")
            self.assertEqual(b1["extra_candidate"]["role"], "EXTRA_CANDIDATE")
            self.assertEqual(b1["review"]["id"], post_body["id"])
            self.assertEqual(b1["comparison"]["order_relationship"], "EXACT_MATCH")
            self.assertIn("field_matrix", b1["comparison"])
            self.assertEqual(b1["original_transaction"], b2["original_transaction"])
            self.assertEqual(b1["extra_candidate"], b2["extra_candidate"])
            self.assertEqual(b1["review"], b2["review"])

            # Reverse URL POST with same decision must return 409 Conflict
            r_dup = await client.post(
                f"/api/transactions/{early.id}/duplicates/{late.id}/review",
                json={"decision": "CONFIRMED_DUPLICATE", "reviewer": "analyst-2@example.com"},
            )
            self.assertEqual(r_dup.status_code, 409, r_dup.text)

        db = self.TestSession()
        try:
            reviews = db.query(DuplicateReview).all()
            self.assertEqual(len(reviews), 1)
        finally:
            db.close()

    async def test_equal_timestamps_use_id_tie_breaker(self):
        same_ts = datetime(2026, 11, 10, 14, 0, 0)
        txn_b = self._seed_txn("txn-tie-b", "CAP-TIE-B", created_at=same_ts, order_id="ORD-TIE")
        txn_a = self._seed_txn("txn-tie-a", "CAP-TIE-A", created_at=same_ts, order_id="ORD-TIE")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            r_inv_forward = await client.get(
                f"/api/transactions/{txn_b.id}/duplicates/{txn_a.id}/investigation"
            )
            r_inv_reverse = await client.get(
                f"/api/transactions/{txn_a.id}/duplicates/{txn_b.id}/investigation"
            )

        self.assertEqual(r_inv_forward.status_code, 200, r_inv_forward.text)
        self.assertEqual(r_inv_reverse.status_code, 200, r_inv_reverse.text)
        self.assertEqual(r_inv_forward.json()["original_transaction"]["id"], "txn-tie-a")
        self.assertEqual(r_inv_forward.json()["extra_candidate"]["id"], "txn-tie-b")
        self.assertEqual(r_inv_reverse.json()["original_transaction"]["id"], "txn-tie-a")
        self.assertEqual(r_inv_reverse.json()["extra_candidate"]["id"], "txn-tie-b")

    async def test_self_pair_aliases_and_nonexistent_ids(self):
        t1 = self._seed_txn("txn-self-1", "CAP-SELF-1", created_at=datetime(2026, 11, 10, 15, 0, 0))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            # Exact same id
            r_same_get = await client.get(f"/api/transactions/{t1.id}/duplicates/{t1.id}/investigation")
            r_same_post = await client.post(
                f"/api/transactions/{t1.id}/duplicates/{t1.id}/review",
                json={"decision": "CONFIRMED_DUPLICATE"},
            )
            self.assertEqual(r_same_get.status_code, 422)
            self.assertEqual(r_same_post.status_code, 422)

            # Internal UUID paired with its own PayPal capture ID alias
            r_alias_get = await client.get(
                f"/api/transactions/{t1.id}/duplicates/{t1.paypal_transaction_id}/investigation"
            )
            r_alias_post = await client.post(
                f"/api/transactions/{t1.paypal_transaction_id}/duplicates/{t1.id}/review",
                json={"decision": "SEPARATE_ORDERS"},
            )
            self.assertEqual(r_alias_get.status_code, 422)
            self.assertEqual(r_alias_post.status_code, 422)

            # Missing IDs -> 404
            r_missing_1 = await client.get(f"/api/transactions/does-not-exist/duplicates/{t1.id}/investigation")
            r_missing_2 = await client.post(
                f"/api/transactions/{t1.id}/duplicates/does-not-exist/review",
                json={"decision": "CONFIRMED_DUPLICATE"},
            )
            self.assertEqual(r_missing_1.status_code, 404)
            self.assertEqual(r_missing_2.status_code, 404)

    async def test_invalid_decision_rejected_422(self):
        t1 = self._seed_txn("txn-inv-1", "CAP-INV-1", created_at=datetime(2026, 11, 10, 16, 0, 0))
        t2 = self._seed_txn("txn-inv-2", "CAP-INV-2", created_at=datetime(2026, 11, 10, 16, 2, 0))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            for bad in ("AUTO_REFUND", "PENDING_REVIEW", "INVALID", ""):
                resp = await client.post(
                    f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                    json={"decision": bad},
                )
                self.assertEqual(resp.status_code, 422, f"Expected 422 for {bad!r}, got {resp.status_code}")

    async def test_repeat_409_override_requires_reason_history_and_dual_audit_events(self):
        t1 = self._seed_txn("txn-hist-1", "CAP-HIST-1", created_at=datetime(2026, 11, 10, 17, 0, 0))
        t2 = self._seed_txn("txn-hist-2", "CAP-HIST-2", created_at=datetime(2026, 11, 10, 17, 1, 0))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            # 1. Initial decision, sending spoofed reviewer_verified=true
            r1 = await client.post(
                f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                json={
                    "decision": "CONFIRMED_DUPLICATE",
                    "reviewer": "support-lead@example.com",
                    "reviewer_verified": True,
                    "notes": "Customer reported two pending receipts",
                },
            )
            self.assertEqual(r1.status_code, 200, r1.text)
            b1 = r1.json()
            self.assertEqual(b1["decision"], "CONFIRMED_DUPLICATE")
            self.assertFalse(b1["reviewer_verified"])
            self.assertEqual(len(b1["decision_history"]), 1)
            self.assertEqual(b1["decision_history"][0]["decision"], "CONFIRMED_DUPLICATE")

            # 2. Repeating same final decision -> 409 Conflict
            r_repeat = await client.post(
                f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                json={"decision": "CONFIRMED_DUPLICATE", "reviewer": "support-lead@example.com"},
            )
            self.assertEqual(r_repeat.status_code, 409)

            # 3. Reversing decision without override_reason -> 422
            r_no_reason = await client.post(
                f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                json={
                    "decision": "SEPARATE_ORDERS",
                    "reviewer": "manager@example.com",
                    "override_reason": "   ",
                },
            )
            self.assertEqual(r_no_reason.status_code, 422)

            # 4. Reversing decision WITH non-empty override_reason -> 200 and preserves history
            r2 = await client.post(
                f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                json={
                    "decision": "SEPARATE_ORDERS",
                    "reviewer": "manager@example.com",
                    "reviewer_verified": True,
                    "notes": "Confirmed two distinct invoices",
                    "override_reason": "Merchant verified two separate shipments",
                },
            )
            self.assertEqual(r2.status_code, 200, r2.text)
            b2 = r2.json()
            self.assertEqual(b2["decision"], "SEPARATE_ORDERS")
            self.assertFalse(b2["reviewer_verified"])
            self.assertEqual(len(b2["decision_history"]), 2)
            self.assertEqual(b2["decision_history"][0]["decision"], "CONFIRMED_DUPLICATE")
            self.assertEqual(b2["decision_history"][1]["decision"], "SEPARATE_ORDERS")
            self.assertEqual(
                b2["decision_history"][1]["override_reason"],
                "Merchant verified two separate shipments",
            )

        # Verify audit events written on BOTH transactions in DB
        db = self.TestSession()
        try:
            ev1 = (
                db.query(TransactionEvent)
                .filter(
                    TransactionEvent.transaction_id == t1.id,
                    TransactionEvent.event_type == "DUPLICATE_REVIEWED",
                )
                .all()
            )
            ev2 = (
                db.query(TransactionEvent)
                .filter(
                    TransactionEvent.transaction_id == t2.id,
                    TransactionEvent.event_type == "DUPLICATE_REVIEWED",
                )
                .all()
            )
            self.assertEqual(len(ev1), 2)
            self.assertEqual(len(ev2), 2)
            for ev in ev1 + ev2:
                self.assertEqual(ev.source, "review")
        finally:
            db.close()

    async def test_concurrent_review_submissions_create_single_row(self):
        t1 = self._seed_txn("txn-conc-1", "CAP-CONC-1", created_at=datetime(2026, 11, 10, 18, 0, 0))
        t2 = self._seed_txn("txn-conc-2", "CAP-CONC-2", created_at=datetime(2026, 11, 10, 18, 1, 0))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            r_a, r_b = await asyncio.gather(
                client.post(
                    f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                    json={"decision": "CONFIRMED_DUPLICATE", "reviewer": "user-a"},
                ),
                client.post(
                    f"/api/transactions/{t2.id}/duplicates/{t1.id}/review",
                    json={"decision": "CONFIRMED_DUPLICATE", "reviewer": "user-b"},
                ),
            )

        statuses = sorted([r_a.status_code, r_b.status_code])
        self.assertEqual(statuses, [200, 409], f"Unexpected concurrent statuses: {r_a.status_code}, {r_b.status_code}")

        db = self.TestSession()
        try:
            reviews = db.query(DuplicateReview).all()
            self.assertEqual(len(reviews), 1)
            self.assertFalse(reviews[0].reviewer_verified)
        finally:
            db.close()

    async def test_separate_orders_suppresses_alert_counts_while_retaining_candidate(self):
        t1 = self._seed_txn("txn-sup-1", "CAP-SUP-1", created_at=datetime(2026, 11, 10, 19, 0, 0), order_id="ORD-SUP")
        t2 = self._seed_txn("txn-sup-2", "CAP-SUP-2", created_at=datetime(2026, 11, 10, 19, 2, 0), order_id="ORD-SUP")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            # Before review: counts = 1
            r_dups_before = await client.get(f"/api/transactions/{t1.id}/duplicates")
            r_txn_before = await client.get(f"/api/transactions/{t1.id}")
            r_list_before = await client.get("/api/transactions")
            r_analytics_before = await client.get("/api/analytics/summary")

            self.assertEqual(r_dups_before.status_code, 200)
            dups_before = r_dups_before.json()
            self.assertTrue(dups_before["has_possible_duplicates"])
            self.assertEqual(len(dups_before["candidates"]), 1)
            self.assertEqual(dups_before["candidates"][0]["review_decision"], "PENDING_REVIEW")

            self.assertEqual(r_txn_before.json()["possible_duplicate_count"], 1)
            analytics_before = r_analytics_before.json()
            self.assertEqual(analytics_before["duplicate_alerts_count"], 1)
            self.assertEqual(len(analytics_before["duplicate_alerts"]), 1)
            for item in r_list_before.json()["items"]:
                self.assertEqual(item["possible_duplicate_count"], 1)

            # Submit SEPARATE_ORDERS review
            r_review = await client.post(
                f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                json={"decision": "SEPARATE_ORDERS", "reviewer": "ops@example.com"},
            )
            self.assertEqual(r_review.status_code, 200)

            # After SEPARATE_ORDERS: alert counts drop to 0, candidate stays visible in investigation/candidates
            r_dups_after = await client.get(f"/api/transactions/{t1.id}/duplicates")
            r_txn_after = await client.get(f"/api/transactions/{t1.id}")
            r_list_after = await client.get("/api/transactions")
            r_analytics_after = await client.get("/api/analytics/summary")
            r_inv_after = await client.get(f"/api/transactions/{t1.id}/duplicates/{t2.id}/investigation")

            self.assertEqual(r_dups_after.status_code, 200)
            dups_after = r_dups_after.json()
            self.assertFalse(dups_after["has_possible_duplicates"])
            self.assertEqual(len(dups_after["candidates"]), 1)
            self.assertEqual(dups_after["candidates"][0]["review_decision"], "SEPARATE_ORDERS")

            self.assertEqual(r_txn_after.json()["possible_duplicate_count"], 0)
            analytics_after = r_analytics_after.json()
            self.assertEqual(analytics_after["duplicate_alerts_count"], 0)
            self.assertEqual(len(analytics_after["duplicate_alerts"]), 0)
            for item in r_list_after.json()["items"]:
                self.assertEqual(item["possible_duplicate_count"], 0)

            self.assertEqual(r_inv_after.status_code, 200)
            self.assertEqual(r_inv_after.json()["review"]["decision"], "SEPARATE_ORDERS")

    async def test_confirmed_duplicate_never_triggers_auto_refund(self):
        t1 = self._seed_txn("txn-noref-1", "CAP-NOREF-1", created_at=datetime(2026, 11, 10, 20, 0, 0))
        t2 = self._seed_txn("txn-noref-2", "CAP-NOREF-2", created_at=datetime(2026, 11, 10, 20, 2, 0))

        live_pp = PayPalService("cid", "sec", "https://api-m.sandbox.paypal.com")
        refund_mock = AsyncMock(side_effect=AssertionError("Review must never trigger PayPal refund"))

        with (
            patch.object(main, "paypal_service", live_pp),
            patch.object(live_pp, "issue_refund", refund_mock),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                r_rev = await client.post(
                    f"/api/transactions/{t1.id}/duplicates/{t2.id}/review",
                    json={"decision": "CONFIRMED_DUPLICATE", "reviewer": "ops@example.com"},
                )
                r_inv = await client.get(f"/api/transactions/{t1.id}/duplicates/{t2.id}/investigation")

        self.assertEqual(r_rev.status_code, 200)
        self.assertEqual(refund_mock.await_count, 0)
        inv = r_inv.json()
        self.assertEqual(inv["original_transaction"]["status"], "COMPLETED")
        self.assertEqual(inv["extra_candidate"]["status"], "COMPLETED")
        self.assertEqual(inv["extra_candidate"]["total_refunded"], 0.0)
        self.assertTrue(inv["extra_candidate"]["refund_eligible"])


if __name__ == "__main__":
    unittest.main()
