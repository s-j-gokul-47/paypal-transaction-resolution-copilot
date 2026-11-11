"""
webhook_service.py - PayPal webhook verification and reliable event processing.

Uses PayPal's official `/v1/notifications/verify-webhook-signature` endpoint
with exact raw-body preservation when webhook verification is enabled.
Tracks event processing outcomes atomically (`PROCESSED`, `UNHANDLED`, `FAILED`)
and safely deduplicates concurrent deliveries using PayPal's event ID.
"""
import logging
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Dispute, Transaction, TransactionEvent, WebhookEvent
from paypal_service import PayPalAPIError, PayPalService
from status_engine import is_valid_webhook_status_transition, record_refund_state

logger = logging.getLogger("paypal_copilot.webhooks")


class MalformedWebhookError(Exception):
    """Raised when a webhook payload lacks required PayPal identifiers."""


class WebhookProcessingError(Exception):
    """Raised when event handling fails transiently and PayPal should retry."""


class WebhookService:
    """
    Handles incoming PayPal webhook events:
    - Signature verification via PayPal's verify-webhook-signature API
    - Concurrency-safe deduplication by PayPal event ID
    - Transactional domain updates + event status tracking
    """

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    async def verify_webhook_signature(
        self,
        headers: dict,
        raw_body: bytes,
        webhook_id: str,
        paypal_service: PayPalService,
        verify_webhooks: bool = True,
    ) -> tuple[bool, str]:
        """
        Verify a PayPal webhook using PayPal's official API and exact raw bytes.

        Returns `(is_allowed, verification_status)`:
          - `(True, "verified_paypal_api")` when PayPal returns `SUCCESS`
          - `(True, "skipped_dev_mode")` ONLY when `verify_webhooks=False` in mock mode
          - `(False, "failed_<reason>")` otherwise
        """
        if not verify_webhooks:
            status = "skipped_dev_mode" if paypal_service._mock_mode else "skipped_dev_override"
            logger.warning("webhook_verification status=%s verify_webhooks=false", status)
            return True, status

        if not webhook_id or webhook_id in {"", "your_webhook_id"}:
            logger.warning("webhook_verification status=failed_missing_webhook_id")
            return False, "failed_missing_webhook_id"

        if paypal_service._mock_mode:
            logger.warning("webhook_verification status=failed_mock_credentials_cannot_verify")
            return False, "failed_mock_credentials_cannot_verify"

        normalised = {k.upper(): v for k, v in headers.items()}
        cert_url = normalised.get("PAYPAL-CERT-URL", "")
        parsed = urlparse(cert_url)
        if parsed.scheme != "https" or not (
            parsed.netloc == "paypal.com" or parsed.netloc.endswith(".paypal.com")
        ):
            logger.warning("webhook_verification status=failed_invalid_cert_url")
            return False, "failed_invalid_cert_url"

        try:
            res = await paypal_service.verify_webhook_signature(
                headers=headers,
                raw_body=raw_body,
                webhook_id=webhook_id,
            )
        except PayPalAPIError as exc:
            logger.error(
                "webhook_verification status=failed_paypal_api_error http_status=%s",
                exc.status_code,
            )
            return False, "failed_paypal_api_error"

        if res.get("verification_status") == "SUCCESS":
            logger.info("webhook_verification status=verified_paypal_api")
            return True, "verified_paypal_api"

        logger.warning(
            "webhook_verification status=failed_signature_mismatch paypal_status=%s",
            res.get("verification_status"),
        )
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
        Persist, deduplicate, and transactionally route a PayPal webhook event.

        Raises:
          - `MalformedWebhookError` if PayPal `id` or `event_type` is missing.
          - `WebhookProcessingError` if the handler raises an error (so caller
            can return a retryable 5xx without marking the event processed).
        """
        raw_event_id = payload.get("id")
        if not isinstance(raw_event_id, str) or not raw_event_id.strip():
            logger.warning("webhook_rejected reason=missing_event_id event_type=%s", event_type)
            raise MalformedWebhookError("Malformed webhook event: missing required PayPal 'id'")
        if not isinstance(event_type, str) or not event_type.strip():
            logger.warning("webhook_rejected reason=missing_event_type event_id=%s", raw_event_id)
            raise MalformedWebhookError("Malformed webhook event: missing required 'event_type'")

        event_id = raw_event_id.strip()
        event_type = event_type.strip()

        logger.info(
            "webhook_processing_start event_id=%s event_type=%s verification_status=%s",
            event_id,
            event_type,
            verification_status,
        )

        # Concurrency-safe claim / deduplication
        db_event = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
        if db_event:
            if db_event.processing_status in {"PROCESSED", "UNHANDLED", "PROCESSING"}:
                logger.info(
                    "webhook_duplicate_skipped event_id=%s event_type=%s existing_status=%s",
                    event_id,
                    event_type,
                    db_event.processing_status,
                )
                return {
                    "status": "skipped",
                    "reason": "duplicate",
                    "event_id": event_id,
                    "event_type": event_type,
                    "processing_status": db_event.processing_status,
                    "verification_status": db_event.verification_status,
                }

            # Retry allowed only when previous attempt FAILED; claim atomically
            claimed = (
                db.query(WebhookEvent)
                .filter(
                    WebhookEvent.id == event_id,
                    WebhookEvent.processing_status == "FAILED",
                )
                .update(
                    {
                        "processing_status": "PROCESSING",
                        "error_message": None,
                        "verification_status": verification_status,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if claimed == 0:
                latest = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
                logger.info(
                    "webhook_duplicate_skipped event_id=%s event_type=%s reason=concurrent_retry",
                    event_id,
                    event_type,
                )
                return {
                    "status": "skipped",
                    "reason": "duplicate",
                    "event_id": event_id,
                    "event_type": event_type,
                    "processing_status": latest.processing_status if latest else "PROCESSING",
                    "verification_status": latest.verification_status if latest else verification_status,
                }
            db_event = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
        else:
            db_event = WebhookEvent(
                id=event_id,
                event_type=event_type,
                payload=payload,
                verification_status=verification_status,
                processing_status="PROCESSING",
                processed=False,
                received_at=datetime.utcnow(),
            )
            db.add(db_event)
            try:
                db.commit()
                db.refresh(db_event)
            except IntegrityError:
                db.rollback()
                existing = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
                logger.info(
                    "webhook_duplicate_skipped event_id=%s event_type=%s reason=concurrent_insert",
                    event_id,
                    event_type,
                )
                return {
                    "status": "skipped",
                    "reason": "duplicate",
                    "event_id": event_id,
                    "event_type": event_type,
                    "processing_status": existing.processing_status if existing else "PROCESSING",
                    "verification_status": existing.verification_status if existing else verification_status,
                }

        handler_map = {
            "CUSTOMER.DISPUTE.CREATED": self._handle_dispute_created,
            "CUSTOMER.DISPUTE.RESOLVED": self._handle_dispute_resolved,
            "CUSTOMER.DISPUTE.UPDATED": self._handle_dispute_updated,
            "PAYMENT.CAPTURE.COMPLETED": self._handle_payment_capture_completed,
            "PAYMENT.CAPTURE.PENDING": self._handle_payment_capture_pending,
            "PAYMENT.CAPTURE.REFUNDED": self._handle_refund_completed,
            "PAYMENT.CAPTURE.REVERSED": self._handle_refund_completed,
        }

        handler = handler_map.get(event_type)
        if not handler:
            db_event.processed = False
            db_event.processing_status = "UNHANDLED"
            db_event.processed_at = None
            db_event.error_message = None
            db_event.verification_status = verification_status
            db.commit()
            logger.info(
                "webhook_unhandled event_id=%s event_type=%s",
                event_id,
                event_type,
            )
            return {
                "status": "unhandled",
                "processing_status": "UNHANDLED",
                "event_id": event_id,
                "event_type": event_type,
                "verification_status": verification_status,
            }

        try:
            result = await handler(payload, db)
            db_event.processed = True
            db_event.processing_status = "PROCESSED"
            db_event.processed_at = datetime.utcnow()
            db_event.error_message = None
            db_event.verification_status = verification_status
            db.commit()
        except Exception as exc:
            db.rollback()
            err_summary = f"{type(exc).__name__}: {exc}"[:500]
            failed_row = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
            if failed_row:
                failed_row.processed = False
                failed_row.processing_status = "FAILED"
                failed_row.processed_at = None
                failed_row.error_message = err_summary
                failed_row.verification_status = verification_status
                db.commit()
            logger.error(
                "webhook_handler_failed event_id=%s event_type=%s error_type=%s",
                event_id,
                event_type,
                type(exc).__name__,
            )
            raise WebhookProcessingError(
                f"Transient failure processing webhook event '{event_id}' ({type(exc).__name__})"
            ) from exc

        result["event_id"] = event_id
        result["event_type"] = event_type
        result["processing_status"] = "PROCESSED"
        result["verification_status"] = verification_status
        logger.info(
            "webhook_handler_succeeded event_id=%s event_type=%s outcome=%s",
            event_id,
            event_type,
            result.get("status"),
        )
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
    # Handlers (flush only — commit happens atomically in process_event)
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
                    if is_valid_webhook_status_transition(txn.status, "DISPUTED"):
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
                db.flush()
                self._log_timeline_event(
                    db,
                    transaction_id=transaction_id,
                    event_type="DISPUTE_OPENED",
                    title="PayPal dispute opened",
                    description=f"Webhook CUSTOMER.DISPUTE.CREATED for {dispute_id} ({reason}).",
                    details={"paypal_dispute_id": dispute_id, "reason": reason},
                )
                db.flush()
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
                target_status = "REFUNDED" if "REFUND" in resolution_text.upper() else "COMPLETED"
                current_status = (dispute.transaction.status or "").upper()
                # Preserve terminal REFUNDED state against non-refund dispute resolution webhooks
                if current_status != "REFUNDED" or target_status == "REFUNDED":
                    dispute.transaction.status = target_status
                    dispute.transaction.updated_at = datetime.utcnow()
                self._log_timeline_event(
                    db,
                    transaction_id=dispute.transaction.id,
                    event_type="DISPUTE_RESOLVED",
                    title="Dispute resolved via webhook",
                    description=f"Dispute {dispute_id} resolved with outcome {resolution_text}.",
                    details={"paypal_dispute_id": dispute_id, "outcome": resolution_text},
                )
            db.flush()
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
            db.flush()
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
            if not is_valid_webhook_status_transition(existing.status, "COMPLETED"):
                logger.info(
                    "webhook_stale_transition_ignored event_id=%s event_type=PAYMENT.CAPTURE.COMPLETED current=%s target=COMPLETED",
                    payload.get("id"),
                    existing.status,
                )
                self._log_timeline_event(
                    db,
                    transaction_id=existing.id,
                    event_type="WEBHOOK_STALE_IGNORED",
                    title="Out-of-order COMPLETED webhook ignored",
                    description=(
                        f"Webhook PAYMENT.CAPTURE.COMPLETED arrived while transaction status was already "
                        f"{existing.status}; preserved {existing.status}."
                    ),
                    details={"capture_id": capture_id, "preserved_status": existing.status},
                )
                db.flush()
                return {
                    "status": "ignored_stale_transition",
                    "transaction_id": existing.id,
                    "preserved_status": existing.status,
                }

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
            db.flush()
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
        db.flush()
        return {"status": "created", "transaction_id": new_txn.id}

    async def _handle_payment_capture_pending(self, payload: dict, db: Session) -> dict:
        resource = payload.get("resource", {})
        capture_id = resource.get("id")
        reason = (resource.get("status_details") or {}).get("reason")

        if not capture_id:
            logger.warning("webhook_capture_pending_missing_id event_id=%s", payload.get("id"))
            return {"status": "no_transaction_match", "capture_id": None}

        txn = db.query(Transaction).filter(
            Transaction.paypal_transaction_id == capture_id
        ).first()

        if not txn:
            logger.info(
                "webhook_capture_pending_unmatched event_id=%s capture_id=%s",
                payload.get("id"),
                capture_id,
            )
            return {"status": "no_transaction_match", "capture_id": capture_id}

        if not is_valid_webhook_status_transition(txn.status, "PENDING"):
            logger.info(
                "webhook_stale_transition_ignored event_id=%s event_type=PAYMENT.CAPTURE.PENDING current=%s target=PENDING",
                payload.get("id"),
                txn.status,
            )
            self._log_timeline_event(
                db,
                transaction_id=txn.id,
                event_type="WEBHOOK_STALE_IGNORED",
                title="Out-of-order PENDING webhook ignored",
                description=(
                    f"Webhook PAYMENT.CAPTURE.PENDING arrived while transaction status was already "
                    f"{txn.status}; preserved {txn.status}."
                ),
                details={
                    "capture_id": capture_id,
                    "preserved_status": txn.status,
                    **({"reason": reason} if reason else {}),
                },
            )
            db.flush()
            return {
                "status": "ignored_stale_transition",
                "transaction_id": txn.id,
                "capture_id": capture_id,
                "preserved_status": txn.status,
            }

        txn.status = "PENDING"
        txn.updated_at = datetime.utcnow()
        if reason:
            meta = dict(txn.metadata_ or {})
            meta["pending_reason"] = reason
            txn.metadata_ = meta

        desc = (
            f"Webhook PAYMENT.CAPTURE.PENDING marked capture {capture_id} as PENDING (reason: {reason})."
            if reason
            else f"Webhook PAYMENT.CAPTURE.PENDING marked capture {capture_id} as PENDING."
        )
        self._log_timeline_event(
            db,
            transaction_id=txn.id,
            event_type="PAYMENT_PENDING",
            title="Payment capture pending",
            description=desc,
            details={"capture_id": capture_id, **({"reason": reason} if reason else {})},
        )
        db.flush()
        return {"status": "pending", "transaction_id": txn.id, "capture_id": capture_id}

    @staticmethod
    def _parse_optional_amount(value) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_refund_capture_id(event_type: str, payload: dict, resource: dict) -> Optional[str]:
        if event_type == "PAYMENT.CAPTURE.REVERSED" or payload.get("resource_type") == "capture":
            return resource.get("id")

        related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
        if related.get("capture_id"):
            return related["capture_id"]

        for link in resource.get("links", []):
            if link.get("rel") == "up":
                href = (link.get("href") or "").split("?")[0].rstrip("/")
                if "/captures/" in href:
                    return href.split("/captures/")[-1].split("/")[0]
                if href:
                    return href.split("/")[-1]
        return None

    async def _handle_refund_completed(self, payload: dict, db: Session) -> dict:
        event_type = (payload.get("event_type") or "PAYMENT.CAPTURE.REFUNDED").strip()
        is_reversal = event_type == "PAYMENT.CAPTURE.REVERSED"
        resource = payload.get("resource", {})
        capture_id = self._extract_refund_capture_id(event_type, payload, resource)
        refund_id = resource.get("id", "unknown")

        if not capture_id:
            return {"status": "no_transaction_match", "refund_id": refund_id}

        txn = db.query(Transaction).filter(
            Transaction.paypal_transaction_id == capture_id
        ).first()
        if not txn:
            return {"status": "no_transaction_match", "refund_id": refund_id, "capture_id": capture_id}

        amount_obj = resource.get("amount") or {}
        refund_amount = self._parse_optional_amount(amount_obj.get("value"))
        currency = (amount_obj.get("currency_code") or txn.currency or "USD").upper()
        breakdown = resource.get("seller_payable_breakdown") or {}
        paypal_total_refunded = self._parse_optional_amount(
            (breakdown.get("total_refunded_amount") or {}).get("value")
        )
        refund_status = (resource.get("status") or "COMPLETED").upper()

        refund_state = record_refund_state(
            txn.amount,
            txn.status,
            txn.metadata_,
            refund_id=None if is_reversal else resource.get("id"),
            amount=refund_amount,
            currency=currency,
            source="webhook",
            refund_status=refund_status,
            paypal_total_refunded=paypal_total_refunded,
            webhook_confirmed=True,
            is_reversal=is_reversal,
        )

        if refund_state["action"] == "duplicate_ignored":
            logger.info(
                "webhook_duplicate_refund_ignored event_id=%s refund_id=%s txn_id=%s",
                payload.get("id"),
                refund_id,
                txn.id,
            )
            return {
                "status": "duplicate_refund_ignored",
                "transaction_id": txn.id,
                "refund_id": refund_id,
                "total_refunded": refund_state["total_refunded"],
            }

        txn.status = refund_state["new_status"]
        txn.metadata_ = refund_state["metadata"]
        txn.updated_at = datetime.utcnow()

        if refund_status == "PENDING" and not is_reversal:
            outcome = "refund_pending"
            title = "Refund pending via webhook"
            description = (
                f"PayPal webhook reported pending refund {refund_id} "
                f"({refund_state['refund_amount']:.2f} {currency})."
            )
        elif is_reversal:
            outcome = "reversed"
            title = "Capture reversed via webhook"
            description = f"PayPal webhook confirmed capture reversal for {capture_id}."
        elif refund_state["is_partial"]:
            outcome = "partially_refunded"
            title = "Partial refund completed via webhook"
            description = (
                f"PayPal webhook confirmed partial refund {refund_id} of "
                f"{refund_state['refund_amount']:.2f} {currency} "
                f"({refund_state['total_refunded']:.2f}/{txn.amount:.2f} {txn.currency} refunded total)."
            )
        else:
            outcome = "refunded"
            title = "Refund completed via webhook"
            description = f"PayPal webhook confirmed refund {refund_id}."

        self._log_timeline_event(
            db,
            transaction_id=txn.id,
            event_type="REFUND_ISSUED",
            title=title,
            description=description,
            details={
                "refund_id": refund_id,
                "capture_id": capture_id,
                "refund_amount": refund_state["refund_amount"],
                "total_refunded": refund_state["total_refunded"],
                "remaining_refundable": refund_state["remaining_refundable"],
                "is_partial": refund_state["is_partial"],
                "refund_status": refund_status,
            },
        )
        db.flush()
        return {
            "status": outcome,
            "transaction_id": txn.id,
            "refund_id": refund_id,
            "total_refunded": refund_state["total_refunded"],
            "remaining_refundable": refund_state["remaining_refundable"],
        }
