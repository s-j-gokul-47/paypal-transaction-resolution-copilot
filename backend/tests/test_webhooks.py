"""
Automated tests for hardened PayPal webhook verification and event processing.
"""
import asyncio
import json
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
from models import Base, Transaction, TransactionEvent, WebhookEvent  # noqa: E402
from paypal_service import PayPalService  # noqa: E402
from webhook_service import WebhookService  # noqa: E402


VALID_HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-123",
    "PAYPAL-TRANSMISSION-ID": "trans-123",
    "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJl",
    "PAYPAL-TRANSMISSION-TIME": "2026-11-10T10:00:00Z",
    "Content-Type": "application/json",
}


class WebhookHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_webhooks.db")
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

    def _seed_transaction(self, capture_id: str, status: str = "COMPLETED") -> Transaction:
        db = self.TestSession()
        try:
            now = datetime.utcnow()
            txn = Transaction(
                id=f"txn-{capture_id}",
                paypal_transaction_id=capture_id,
                merchant_id="MERCH-TEST",
                amount=25.00,
                currency="USD",
                status=status,
                description="Test capture",
                created_at=now,
                updated_at=now,
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)
            return txn
        finally:
            db.close()

    async def test_valid_signature_processes_and_persists_event(self):
        live_pp = PayPalService("sandbox-cid", "sandbox-secret", "https://api-m.sandbox.paypal.com")
        raw_payload = json.dumps(
            {
                "id": "WH-VALID-001",
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {
                    "id": "CAP-VALID-001",
                    "amount": {"value": "49.50", "currency_code": "USD"},
                },
            }
        ).encode("utf-8")

        async def _fake_post_raw(path, raw_json_bytes, extra_headers=None):
            return {"verification_status": "SUCCESS"}

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", True),
            patch.object(main, "PAYPAL_WEBHOOK_ID", "WH-ID-999"),
            patch.object(main, "paypal_service", live_pp),
            patch.object(live_pp, "_post_raw_json", side_effect=_fake_post_raw),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/webhooks/paypal", content=raw_payload, headers=VALID_HEADERS)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["verification_status"], "verified_paypal_api")
        self.assertEqual(body["result"]["processing_status"], "PROCESSED")

        db = self.TestSession()
        try:
            ev = db.query(WebhookEvent).filter(WebhookEvent.id == "WH-VALID-001").first()
            self.assertIsNotNone(ev)
            self.assertTrue(ev.processed)
            self.assertEqual(ev.processing_status, "PROCESSED")
            self.assertEqual(ev.verification_status, "verified_paypal_api")
            txn = db.query(Transaction).filter(Transaction.paypal_transaction_id == "CAP-VALID-001").first()
            self.assertIsNotNone(txn)
            self.assertEqual(txn.status, "COMPLETED")
        finally:
            db.close()

    async def test_invalid_signature_returns_401_and_does_not_store_event(self):
        live_pp = PayPalService("sandbox-cid", "sandbox-secret", "https://api-m.sandbox.paypal.com")
        raw_payload = json.dumps(
            {
                "id": "WH-INVALID-001",
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {"id": "CAP-INVALID-001", "amount": {"value": "10.00", "currency_code": "USD"}},
            }
        ).encode("utf-8")

        async def _fake_post_raw(path, raw_json_bytes, extra_headers=None):
            return {"verification_status": "FAILURE"}

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", True),
            patch.object(main, "PAYPAL_WEBHOOK_ID", "WH-ID-999"),
            patch.object(main, "paypal_service", live_pp),
            patch.object(live_pp, "_post_raw_json", side_effect=_fake_post_raw),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/webhooks/paypal", content=raw_payload, headers=VALID_HEADERS)

        self.assertEqual(resp.status_code, 401)
        db = self.TestSession()
        try:
            self.assertIsNone(db.query(WebhookEvent).filter(WebhookEvent.id == "WH-INVALID-001").first())
            self.assertIsNone(db.query(Transaction).filter(Transaction.paypal_transaction_id == "CAP-INVALID-001").first())
        finally:
            db.close()

    async def test_missing_webhook_id_returns_401(self):
        live_pp = PayPalService("sandbox-cid", "sandbox-secret", "https://api-m.sandbox.paypal.com")
        raw_payload = json.dumps({"id": "WH-NOID-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}).encode("utf-8")

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", True),
            patch.object(main, "PAYPAL_WEBHOOK_ID", ""),
            patch.object(main, "paypal_service", live_pp),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/webhooks/paypal", content=raw_payload, headers=VALID_HEADERS)

        self.assertEqual(resp.status_code, 401)
        self.assertIn("failed_missing_webhook_id", resp.json()["detail"])

    async def test_exact_raw_body_preserved_unchanged(self):
        live_pp = PayPalService("sandbox-cid", "sandbox-secret", "https://api-m.sandbox.paypal.com")
        # Deliberate trailing zero (19.90), escaped slash (\/), unusual key order, and whitespace
        exact_raw_bytes = (
            b'{\n  "z_first": 1,  "amount_hint": 19.90,\n'
            b'  "link": "https:\\/\\/example.com\\/ok",\n'
            b'  "id": "WH-RAW-EXACT-1",\n'
            b'  "event_type": "PAYMENT.CAPTURE.COMPLETED",\n'
            b'  "resource": {"id": "CAP-RAW-1", "amount": {"value": "19.90", "currency_code": "USD"}}\n'
            b'}\n'
        )
        captured_outbound = []

        async def _capture_outbound(path, raw_json_bytes, extra_headers=None):
            captured_outbound.append((path, raw_json_bytes))
            return {"verification_status": "SUCCESS"}

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", True),
            patch.object(main, "PAYPAL_WEBHOOK_ID", "WH-ID-RAW"),
            patch.object(main, "paypal_service", live_pp),
            patch.object(live_pp, "_post_raw_json", side_effect=_capture_outbound),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/webhooks/paypal", content=exact_raw_bytes, headers=VALID_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(captured_outbound), 1)
        path, sent_bytes = captured_outbound[0]
        self.assertEqual(path, "/v1/notifications/verify-webhook-signature")
        # Outbound body must be valid JSON AND contain the exact raw bytes unchanged!
        parsed_outbound = json.loads(sent_bytes.decode("utf-8"))
        self.assertEqual(parsed_outbound["webhook_id"], "WH-ID-RAW")
        self.assertIn(exact_raw_bytes, sent_bytes)

    async def test_payment_capture_pending_matched_and_unmatched(self):
        seeded = self._seed_transaction("CAP-PEND-1", status="PENDING")

        matched_payload = {
            "id": "WH-PEND-MATCH-1",
            "event_type": "PAYMENT.CAPTURE.PENDING",
            "resource": {
                "id": "CAP-PEND-1",
                "status": "PENDING",
                "status_details": {"reason": "ECHECK"},
            },
        }
        unmatched_payload = {
            "id": "WH-PEND-UNMATCH-1",
            "event_type": "PAYMENT.CAPTURE.PENDING",
            "resource": {"id": "CAP-UNKNOWN-999", "status": "PENDING"},
        }

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                r1 = await client.post("/api/webhooks/paypal", json=matched_payload)
                r2 = await client.post("/api/webhooks/paypal", json=unmatched_payload)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["result"]["status"], "pending")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["result"]["status"], "no_transaction_match")

        db = self.TestSession()
        try:
            txn = db.query(Transaction).filter(Transaction.id == seeded.id).first()
            self.assertEqual(txn.status, "PENDING")
            events = db.query(TransactionEvent).filter(TransactionEvent.transaction_id == seeded.id).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, "PAYMENT_PENDING")
            self.assertEqual(events[0].source, "webhook")
            self.assertIsNone(db.query(Transaction).filter(Transaction.paypal_transaction_id == "CAP-UNKNOWN-999").first())
        finally:
            db.close()

    async def test_payment_capture_completed_updates_existing_and_logs_timeline(self):
        seeded = self._seed_transaction("CAP-COMP-1", status="PENDING")
        payload = {
            "id": "WH-COMP-1",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-COMP-1",
                "amount": {"value": "25.00", "currency_code": "USD"},
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
        self.assertEqual(resp.json()["result"]["status"], "updated")

        db = self.TestSession()
        try:
            txn = db.query(Transaction).filter(Transaction.id == seeded.id).first()
            self.assertEqual(txn.status, "COMPLETED")
            events = db.query(TransactionEvent).filter(TransactionEvent.transaction_id == seeded.id).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, "PAYMENT_CAPTURED")
            self.assertEqual(events[0].source, "webhook")
        finally:
            db.close()

    async def test_duplicate_event_sequential_and_concurrent_deduplication(self):
        payload = {
            "id": "WH-DUP-001",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-DUP-001",
                "amount": {"value": "33.00", "currency_code": "USD"},
            },
        }

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                # Fire 2 concurrent requests simultaneously + 1 sequential replay
                r1, r2 = await asyncio.gather(
                    client.post("/api/webhooks/paypal", json=payload),
                    client.post("/api/webhooks/paypal", json=payload),
                )
                r3 = await client.post("/api/webhooks/paypal", json=payload)

        statuses = sorted([r1.json()["result"]["status"], r2.json()["result"]["status"]])
        self.assertEqual(statuses, ["created", "skipped"])
        self.assertEqual(r3.json()["result"]["status"], "skipped")
        self.assertEqual(r3.json()["result"]["reason"], "duplicate")

        db = self.TestSession()
        try:
            txns = db.query(Transaction).filter(Transaction.paypal_transaction_id == "CAP-DUP-001").all()
            self.assertEqual(len(txns), 1)
            events = db.query(TransactionEvent).filter(TransactionEvent.transaction_id == txns[0].id).all()
            self.assertEqual(len(events), 1)
        finally:
            db.close()

    async def test_handler_failure_rolls_back_and_allows_retry(self):
        seeded = self._seed_transaction("CAP-RETRY-1", status="PENDING")
        payload = {
            "id": "WH-RETRY-001",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-RETRY-1",
                "amount": {"value": "25.00", "currency_code": "USD"},
            },
        }

        ws = WebhookService()
        original_handler = ws._handle_payment_capture_completed

        async def _flaky_handler(p, db):
            # Mutate transaction inside handler BEFORE raising to prove rollback!
            t = db.query(Transaction).filter(Transaction.paypal_transaction_id == "CAP-RETRY-1").first()
            t.status = "COMPLETED"
            db.flush()
            raise RuntimeError("Simulated transient DB write error")

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
            patch.object(main, "webhook_service", ws),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                with patch.object(ws, "_handle_payment_capture_completed", side_effect=_flaky_handler):
                    fail_resp = await client.post("/api/webhooks/paypal", json=payload)

                self.assertEqual(fail_resp.status_code, 503)

                # Verify transaction rolled back and event was NOT marked processed
                db = self.TestSession()
                try:
                    ev = db.query(WebhookEvent).filter(WebhookEvent.id == "WH-RETRY-001").first()
                    self.assertIsNotNone(ev)
                    self.assertFalse(ev.processed)
                    self.assertEqual(ev.processing_status, "FAILED")
                    self.assertIn("RuntimeError", ev.error_message)
                    txn = db.query(Transaction).filter(Transaction.id == seeded.id).first()
                    self.assertEqual(txn.status, "PENDING")
                finally:
                    db.close()

                # Retry succeeds now that transient error is gone
                with patch.object(ws, "_handle_payment_capture_completed", side_effect=original_handler):
                    ok_resp = await client.post("/api/webhooks/paypal", json=payload)

                self.assertEqual(ok_resp.status_code, 200)
                self.assertEqual(ok_resp.json()["result"]["processing_status"], "PROCESSED")

                db = self.TestSession()
                try:
                    ev = db.query(WebhookEvent).filter(WebhookEvent.id == "WH-RETRY-001").first()
                    self.assertTrue(ev.processed)
                    self.assertEqual(ev.processing_status, "PROCESSED")
                    self.assertIsNone(ev.error_message)
                    txn = db.query(Transaction).filter(Transaction.id == seeded.id).first()
                    self.assertEqual(txn.status, "COMPLETED")
                finally:
                    db.close()

    async def test_unknown_event_type_and_malformed_missing_id(self):
        unknown_payload = {
            "id": "WH-UNKNOWN-1",
            "event_type": "BILLING.SUBSCRIPTION.CREATED",
            "resource": {"id": "SUB-1"},
        }
        malformed_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAP-NO-EVT-ID"},
        }

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                r_unknown = await client.post("/api/webhooks/paypal", json=unknown_payload)
                r_malformed = await client.post("/api/webhooks/paypal", json=malformed_payload)

        self.assertEqual(r_unknown.status_code, 200)
        self.assertEqual(r_unknown.json()["result"]["status"], "unhandled")
        self.assertEqual(r_unknown.json()["result"]["processing_status"], "UNHANDLED")

        self.assertEqual(r_malformed.status_code, 400)

        db = self.TestSession()
        try:
            ev = db.query(WebhookEvent).filter(WebhookEvent.id == "WH-UNKNOWN-1").first()
            self.assertIsNotNone(ev)
            self.assertFalse(ev.processed)
            self.assertEqual(ev.processing_status, "UNHANDLED")
            # No random UUID row created for malformed event
            all_rows = db.query(WebhookEvent).all()
            self.assertEqual(len(all_rows), 1)
        finally:
            db.close()

    async def test_out_of_order_webhooks_preserve_newer_states(self):
        t_completed = self._seed_transaction("CAP-OOO-COMP", status="COMPLETED")
        t_refunded = self._seed_transaction("CAP-OOO-REF", status="REFUNDED")
        t_disputed = self._seed_transaction("CAP-OOO-DISP", status="DISPUTED")

        # Link a dispute on t_refunded to test CUSTOMER.DISPUTE.RESOLVED preservation
        from models import Dispute

        db = self.TestSession()
        try:
            db.add(
                Dispute(
                    id="disp-ooo-1",
                    transaction_id=t_refunded.id,
                    paypal_dispute_id="PP-D-OOO-1",
                    reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    status="OPEN",
                )
            )
            db.commit()
        finally:
            db.close()

        stale_events = [
            {
                "id": "WH-OOO-PEND-ON-COMP",
                "event_type": "PAYMENT.CAPTURE.PENDING",
                "resource": {"id": "CAP-OOO-COMP", "status": "PENDING", "status_details": {"reason": "ECHECK"}},
            },
            {
                "id": "WH-OOO-PEND-ON-DISP",
                "event_type": "PAYMENT.CAPTURE.PENDING",
                "resource": {"id": "CAP-OOO-DISP", "status": "PENDING"},
            },
            {
                "id": "WH-OOO-COMP-ON-REF",
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {"id": "CAP-OOO-REF", "amount": {"value": "25.00", "currency_code": "USD"}},
            },
            {
                "id": "WH-OOO-COMP-ON-DISP",
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {"id": "CAP-OOO-DISP", "amount": {"value": "25.00", "currency_code": "USD"}},
            },
            {
                "id": "WH-OOO-DISP-RES-ON-REF",
                "event_type": "CUSTOMER.DISPUTE.RESOLVED",
                "resource": {
                    "dispute_id": "PP-D-OOO-1",
                    "dispute_outcome": {"outcome_code": "RESOLVED_SELLER_FAVOUR"},
                },
            },
        ]

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                responses = [await client.post("/api/webhooks/paypal", json=ev) for ev in stale_events]

        for r in responses:
            self.assertEqual(r.status_code, 200)
        for r in responses[:4]:
            self.assertEqual(r.json()["result"]["status"], "ignored_stale_transition")

        db = self.TestSession()
        try:
            self.assertEqual(db.query(Transaction).filter(Transaction.id == t_completed.id).first().status, "COMPLETED")
            self.assertEqual(db.query(Transaction).filter(Transaction.id == t_refunded.id).first().status, "REFUNDED")
            self.assertEqual(db.query(Transaction).filter(Transaction.id == t_disputed.id).first().status, "DISPUTED")
        finally:
            db.close()

    async def test_partial_refunds_api_webhook_dedup_and_over_refund_guard(self):
        seeded = self._seed_transaction("CAP-PARTIAL-1", status="COMPLETED")  # 25.00 USD
        live_pp = PayPalService("sandbox-cid", "sandbox-secret", "https://api-m.sandbox.paypal.com")

        refund_calls = []

        async def _fake_issue_refund(capture_id, amount, currency="USD", note_to_payer=""):
            idx = len(refund_calls) + 1
            refund_calls.append((capture_id, amount))
            return {
                "id": f"REF-PART-{idx}",
                "status": "COMPLETED",
                "mode": "paypal_sandbox",
                "is_simulated": False,
            }

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", live_pp),
            patch.object(live_pp, "issue_refund", side_effect=_fake_issue_refund),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                # 1. Partial refund $10.00 of $25.00
                r_p1 = await client.post(
                    f"/api/transactions/{seeded.id}/refund",
                    json={"amount": 10.00, "reason": "Partial item refund"},
                )
                self.assertEqual(r_p1.status_code, 200)
                t_after_p1 = r_p1.json()["transaction"]
                self.assertEqual(t_after_p1["status"], "COMPLETED")
                self.assertTrue(t_after_p1["metadata"]["partial_refund"])
                self.assertEqual(t_after_p1["metadata"]["total_refunded"], 10.0)
                self.assertEqual(t_after_p1["metadata"]["remaining_refundable"], 15.0)

                # 2. Over-refund $16.00 must be rejected 422 before calling PayPal
                r_over = await client.post(
                    f"/api/transactions/{seeded.id}/refund",
                    json={"amount": 16.00, "reason": "Too high"},
                )
                self.assertEqual(r_over.status_code, 422)
                self.assertEqual(len(refund_calls), 1)

                # 3. Webhook confirms first partial refund REF-PART-1 (should not double count!)
                wh_p1 = {
                    "id": "WH-PART-1A",
                    "event_type": "PAYMENT.CAPTURE.REFUNDED",
                    "resource_type": "refund",
                    "resource": {
                        "id": "REF-PART-1",
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "10.00"},
                        "seller_payable_breakdown": {"total_refunded_amount": {"currency_code": "USD", "value": "10.00"}},
                        "links": [{"rel": "up", "href": "https://api.sandbox.paypal.com/v2/payments/captures/CAP-PARTIAL-1"}],
                    },
                }
                r_wh1 = await client.post("/api/webhooks/paypal", json=wh_p1)
                self.assertEqual(r_wh1.status_code, 200)
                self.assertEqual(r_wh1.json()["result"]["status"], "partially_refunded")
                self.assertEqual(r_wh1.json()["result"]["total_refunded"], 10.0)

                # 4. Repeated delivery for SAME refund_id under a DIFFERENT PayPal event ID must be ignored
                wh_p1_dup = dict(wh_p1, id="WH-PART-1B-RETRY")
                r_wh1_dup = await client.post("/api/webhooks/paypal", json=wh_p1_dup)
                self.assertEqual(r_wh1_dup.status_code, 200)
                self.assertEqual(r_wh1_dup.json()["result"]["status"], "duplicate_refund_ignored")

                # 5. Final partial refund $15.00 completes the full $25.00 -> REFUNDED
                r_p2 = await client.post(
                    f"/api/transactions/{seeded.id}/refund",
                    json={"amount": 15.00, "reason": "Remaining refund"},
                )
                self.assertEqual(r_p2.status_code, 200)
                t_after_p2 = r_p2.json()["transaction"]
                self.assertEqual(t_after_p2["status"], "REFUNDED")
                self.assertFalse(t_after_p2["metadata"]["partial_refund"])
                self.assertEqual(t_after_p2["metadata"]["total_refunded"], 25.0)
                self.assertEqual(t_after_p2["metadata"]["remaining_refundable"], 0.0)

                # 6. Further refund attempt rejected with 409
                r_after_full = await client.post(f"/api/transactions/{seeded.id}/refund", json={"amount": 1.00})
                self.assertEqual(r_after_full.status_code, 409)

        db = self.TestSession()
        try:
            events = db.query(TransactionEvent).filter(TransactionEvent.transaction_id == seeded.id).all()
            # 1 API partial + 1 webhook confirm + 1 API final = 3 (duplicate WH-PART-1B-RETRY added 0 events!)
            self.assertEqual(len(events), 3)
        finally:
            db.close()

    async def test_payment_capture_reversal_matches_capture_id_directly(self):
        seeded = self._seed_transaction("CAP-REV-1", status="COMPLETED")
        reversal_payload = {
            "id": "WH-REV-1",
            "event_type": "PAYMENT.CAPTURE.REVERSED",
            "resource_type": "capture",
            "resource": {
                "id": "CAP-REV-1",
                "status": "REVERSED",
                "amount": {"currency_code": "USD", "value": "25.00"},
                "links": [{"rel": "up", "href": "https://api.sandbox.paypal.com/v2/checkout/orders/ORD-IGNORED"}],
            },
        }

        with (
            patch.object(main, "PAYPAL_VERIFY_WEBHOOKS", False),
            patch.object(main, "paypal_service", PayPalService("", "", "")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/webhooks/paypal", json=reversal_payload)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["result"]["status"], "reversed")

        db = self.TestSession()
        try:
            txn = db.query(Transaction).filter(Transaction.id == seeded.id).first()
            self.assertEqual(txn.status, "REFUNDED")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

