"""
webhook_service.py - PayPal webhook verification and event processing.

Uses PayPal's official `/v1/notifications/verify-webhook-signature` endpoint
when webhook verification is enabled. Does NOT use a non-standard local HMAC
check. When `PAYPAL_VERIFY_WEBHOOKS=false` in local development/mock mode,
events are explicitly recorded as `verification_status="skipped_dev_mode"`.
"""
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from models import Dispute, Transaction, TransactionEvent, WebhookEvent
from paypal_service import PayPalAPIError, PayPalService


class WebhookService:
    """
    Handles incoming PayPal webhook events:
    - Signature verification via PayPal's verify-webhook-signature API
    - Event routing to domain-specific handlers
    - Idempotent event storage and payment timeline logging
    """

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    async def verify_webhook_signature(
        self,
        headers: dict,
        webhook_event: dict,
        webhook_id: str,
        paypal_service: PayPalService,
        verify_webhooks: bool = True,
    ) -> tuple[bool, str]:
        """
        Verify a PayPal webhook using PayPal's official API.

        Returns `(is_allowed, verification_status)`:
          - `(True, "verified_paypal_api")` when PayPal returns `SUCCESS`
          - `(True, "skipped_dev_mode")` ONLY when `verify_webhooks=False` and in mock/dev mode
          - `(False, "failed_<reason>")` otherwise
        """
        if not verify_webhooks:
            if paypal_service._mock_mode:
                return True, "skipped_dev_mode"
            # Even if PAYPAL_VERIFY_WEBHOOKS=false, warn explicitly via status
            return True, "skipped_dev_override"

        if not webhook_id or webhook_id in {"", "your_webhook_id"}:
            return False, "failed_missing_webhook_id"

        if paypal_service._mock_mode:
            return False, "failed_mock_credentials_cannot_verify"

        # Sanity-check PAYPAL-CERT-URL belongs to PayPal HTTPS host
        normalised = {k.upper(): v for k, v in headers.items()}
        cert_url = normalised.get("PAYPAL-CERT-URL", "")
        parsed = urlparse(cert_url)
        if parsed.scheme != "https" or not (
            parsed.netloc == "paypal.com" or parsed.netloc.endswith(".paypal.com")
        ):
            return False, "failed_invalid_cert_url"

        try:
            res = await paypal_service.verify_webhook_signature(
                headers=headers,
                webhook_event=webhook_event,
                webhook_id=webhook_id,
            )
        except PayPalAPIError:
            return False, "failed_paypal_api_error"

        if res.get("verification_status") == "SUCCESS":
            return True, "verified_paypal_api"
        return False, "failed_signature_mismatch"

    # ------------------------------------------------------------------
    # Event processing entry point
    # ------------------------------------------------------------------

    async def process_event(
        self,
        event_type: str,
        payload: dict,
        db: Session,
        verification_status: str = "unverified",
    ) -> dict:
        """
        Persist and route a webhook event.

        Returns a dict describing the processing outcome.
        """
        event_id = payload.get("id") or str(uuid.uuid4())
        existing = db.query(WebhookEvent).filter(
            WebhookEvent.id == event_id,
            WebhookEvent.processed == True,  # noqa: E712
        ).first()
        if existing:
            return {
                "status": "skipped",
                "reason": "duplicate",
                "event_id": event_id,
                "verification_status": existing.verification_status,
            }

        db_event = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
        if not db_event:
            db_event = WebhookEvent(
                id=event_id,
                event_type=event_type,
                payload=payload,
                verification_status=verification_status,
                processed=False,
                received_at=datetime.utcnow(),
            )
            db.add(db_event)
            db.commit()
            db.refresh(db_event)

        handler_map = {
            "CUSTOMER.DISPUTE.CREATED": self._handle_dispute_created,
            "CUSTOMER.DISPUTE.RESOLVED": self._handle_dispute_resolved,
            "CUSTOMER.DISPUTE.UPDATED": self._handle_dispute_updated,
            "PAYMENT.CAPTURE.COMPLETED": self._handle_payment_capture_completed,
            "PAYMENT.CAPTURE.REFUNDED": self._handle_refund_completed,
            "PAYMENT.CAPTURE.REVERSED": self._handle_refund_completed,
        }

        handler = handler_map.get(event_type)
        result: dict = {"status": "unhandled", "event_type": event_type}

        if handler:
            try:
                result = await handler(payload, db)
                result["event_type"] = event_type
            except Exception as exc:
                result = {
                    "status": "error",
                    "event_type": event_type,
                    "error": str(exc),
                }

        db_event.processed = True
        db_event.processed_at = datetime.utcnow()
        db_event.verification_status = verification_status
        db.commit()

        result["verification_status"] = verification_status
        return result

    # ------------------------------------------------------------------
    # Timeline helper
    # ------------------------------------------------------------------

    @staticmethod
    def _log_timeline_event(
        db: Session,
        transaction_id: str,
        event_type: str,
        title: str,
        description: str,
        details: Optional[dict] = None,
    ) -> None:
        db.add(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=transaction_id,
                event_type=event_type,
                title=title,
                description=description,
                source="webhook",
                details=details or {},
                occurred_at=datetime.utcnow(),
            )
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_dispute_created(self, payload: dict, db: Session) -> dict:
        resource = payload.get("resource", {})
        dispute_id = resource.get("dispute_id") or resource.get("id")
        reason = resource.get("reason", "UNKNOWN")

        disputed_txns = resource.get("disputed_transactions", [])
        transaction_id: Optional[str] = None
        for dtxn in disputed_txns:
            paypal_txn_id = dtxn.get("buyer_transaction_id") or dtxn.get("seller_transaction_id")
            if paypal_txn_id:
                txn = db.query(Transaction).filter(
                    Transaction.paypal_transaction_id == paypal_txn_id
                ).first()
                if txn:
                    transaction_id = txn.id
                    txn.status = "DISPUTED"
                    txn.updated_at = datetime.utcnow()
                    break

        if transaction_id:
            existing_dispute = db.query(Dispute).filter(
                Dispute.paypal_dispute_id == dispute_id
            ).first()

            if not existing_dispute:
                new_dispute = Dispute(
                    id=str(uuid.uuid4()),
                    transaction_id=transaction_id,
                    paypal_dispute_id=dispute_id,
                    reason=reason,
                    status="OPEN",
                    evidence_submitted=False,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(new_dispute)
                self._log_timeline_event(
                    db,
                    transaction_id=transaction_id,
                    event_type="DISPUTE_OPENED",
                    title="PayPal dispute opened",
                    description=f"Webhook CUSTOMER.DISPUTE.CREATED for {dispute_id} ({reason}).",
                    details={"paypal_dispute_id": dispute_id, "reason": reason},
                )
                db.commit()
                return {"status": "created", "dispute_id": new_dispute.id}
            return {"status": "exists", "dispute_id": existing_dispute.id}

        return {"status": "no_transaction_match", "paypal_dispute_id": dispute_id}

    async def _handle_dispute_resolved(self, payload: dict, db: Session) -> dict:
        resource = payload.get("resource", {})
        dispute_id = resource.get("dispute_id") or resource.get("id")
        outcome = resource.get("dispute_outcome", {})
        resolution_text = outcome.get("outcome_code", "RESOLVED")

        dispute = db.query(Dispute).filter(
            Dispute.paypal_dispute_id == dispute_id
        ).first()

        if dispute:
            dispute.status = "RESOLVED"
            dispute.resolution = resolution_text
            dispute.updated_at = datetime.utcnow()
            if dispute.transaction:
                new_txn_status = "REFUNDED" if "REFUND" in resolution_text.upper() else "COMPLETED"
                dispute.transaction.status = new_txn_status
                dispute.transaction.updated_at = datetime.utcnow()
                self._log_timeline_event(
                    db,
                    transaction_id=dispute.transaction.id,
                    event_type="DISPUTE_RESOLVED",
                    title="Dispute resolved via webhook",
                    description=f"Dispute {dispute_id} resolved with outcome {resolution_text}.",
                    details={"paypal_dispute_id": dispute_id, "outcome": resolution_text},
                )
            db.commit()
            return {"status": "resolved", "dispute_id": dispute.id}

        return {"status": "not_found", "paypal_dispute_id": dispute_id}

    async def _handle_dispute_updated(self, payload: dict, db: Session) -> dict:
        resource = payload.get("resource", {})
        dispute_id = resource.get("dispute_id") or resource.get("id")
        new_status = resource.get("status", "UNDER_REVIEW")

        status_map = {
            "OPEN": "OPEN",
            "UNDER_REVIEW": "UNDER_REVIEW",
            "WAITING_FOR_BUYER_RESPONSE": "UNDER_REVIEW",
            "WAITING_FOR_SELLER_RESPONSE": "UNDER_REVIEW",
            "RESOLVED": "RESOLVED",
            "OTHER": "UNDER_REVIEW",
        }
        mapped_status = status_map.get(new_status.upper(), "UNDER_REVIEW")

        dispute = db.query(Dispute).filter(
            Dispute.paypal_dispute_id == dispute_id
        ).first()

        if dispute:
            dispute.status = mapped_status
            dispute.updated_at = datetime.utcnow()
            db.commit()
            return {"status": "updated", "dispute_id": dispute.id, "new_status": mapped_status}

        return {"status": "not_found", "paypal_dispute_id": dispute_id}

    async def _handle_payment_capture_completed(self, payload: dict, db: Session) -> dict:
        resource = payload.get("resource", {})
        capture_id = resource.get("id")
        amount_obj = resource.get("amount", {})
        amount = float(amount_obj.get("value", 0) or 0)
        currency = amount_obj.get("currency_code", "USD")
        seller_email = resource.get("payee", {}).get("email_address") or None

        existing = db.query(Transaction).filter(
            Transaction.paypal_transaction_id == capture_id
        ).first()

        if existing:
            existing.status = "COMPLETED"
            existing.updated_at = datetime.utcnow()
            self._log_timeline_event(
                db,
                transaction_id=existing.id,
                event_type="PAYMENT_CAPTURED",
                title="Payment capture completed",
                description=f"Webhook PAYMENT.CAPTURE.COMPLETED confirmed {amount:.2f} {currency}.",
                details={"capture_id": capture_id},
            )
            db.commit()
            return {"status": "updated", "transaction_id": existing.id}

        new_txn = Transaction(
            id=str(uuid.uuid4()),
            paypal_transaction_id=capture_id,
            merchant_id=seller_email or "webhook-merchant",
            amount=amount,
            currency=currency,
            status="COMPLETED",
            seller_email=seller_email,
            description="Payment capture completed via webhook",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(new_txn)
        db.flush()
        self._log_timeline_event(
            db,
            transaction_id=new_txn.id,
            event_type="PAYMENT_CAPTURED",
            title="Payment capture recorded via webhook",
            description=f"Webhook PAYMENT.CAPTURE.COMPLETED created record for {amount:.2f} {currency}.",
            details={"capture_id": capture_id},
        )
        db.commit()
        return {"status": "created", "transaction_id": new_txn.id}

    async def _handle_refund_completed(self, payload: dict, db: Session) -> dict:
        resource = payload.get("resource", {})
        links = resource.get("links", [])
        capture_id: Optional[str] = None
        for link in links:
            if link.get("rel") == "up":
                href = link.get("href", "")
                capture_id = href.rstrip("/").split("/")[-1]
                break

        if capture_id:
            txn = db.query(Transaction).filter(
                Transaction.paypal_transaction_id == capture_id
            ).first()
            if txn:
                txn.status = "REFUNDED"
                txn.updated_at = datetime.utcnow()
                self._log_timeline_event(
                    db,
                    transaction_id=txn.id,
                    event_type="REFUND_ISSUED",
                    title="Refund completed via webhook",
                    description=f"PayPal webhook confirmed refund {resource.get('id', 'N/A')}.",
                    details={"refund_id": resource.get("id")},
                )
                db.commit()
                return {"status": "refunded", "transaction_id": txn.id}

        refund_id = resource.get("id", "unknown")
        return {"status": "no_transaction_match", "refund_id": refund_id}
