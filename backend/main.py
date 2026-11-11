"""
main.py - FastAPI application for PayPal Transaction Resolution Copilot.

Core MVP workflow:
  - Transaction status inspection and grounded explanations
  - Real payment event timelines stored in DB / updated via actions & webhooks
  - Duplicate payment detection across verified records
  - Copy-ready support summaries (buyer / internal / escalation)
  - Real/simulated PayPal refunds & PayPal API proxy

Secondary workflow:
  - Dispute tracking, evidence submission, AI dispute guidance, and SLA cases
"""
import json as _json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

load_dotenv()

from database import get_db, init_db, SessionLocal
from models import (
    Transaction, TransactionEvent, Dispute, ResolutionCase, WebhookEvent, DuplicateReview,
    TransactionCreate, TransactionResponse, PaginatedTransactionsResponse,
    TransactionEventResponse, TransactionStateExplanation,
    DuplicateAnalysisResponse, DuplicateReviewRequest, DuplicateReviewResponse,
    DuplicateSideBySideTransaction, DuplicateInvestigationResponse,
    SupportSummaryRequest, SupportSummaryResponse,
    RefundRequest,
    DisputeCreate, DisputeResponse, PaginatedDisputesResponse,
    ResolutionCaseCreate, ResolutionCaseUpdate, ResolutionCaseResponse, PaginatedCasesResponse,
    AIRecommendationRequest, AIRecommendationResponse,
)
from paypal_service import PayPalService, PayPalAPIError
from ai_service import AIService
from status_engine import StatusEngine, extract_order_id, get_refund_summary, record_refund_state
from webhook_service import WebhookService, MalformedWebhookError, WebhookProcessingError

webhook_logger = logging.getLogger("paypal_copilot.webhooks")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("paypal_copilot").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Service singletons
# ---------------------------------------------------------------------------

paypal_service = PayPalService(
    client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
    client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
    base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
)
ai_service = AIService(api_key=os.getenv("OPENAI_API_KEY", ""))
status_engine = StatusEngine()
webhook_service = WebhookService()
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
PAYPAL_VERIFY_WEBHOOKS = os.getenv("PAYPAL_VERIFY_WEBHOOKS", "false").strip().lower() in {
    "1", "true", "yes", "on"
}


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _txn_to_dict(txn: Transaction) -> dict:
    return {
        "id": txn.id,
        "paypal_transaction_id": txn.paypal_transaction_id,
        "merchant_id": txn.merchant_id,
        "amount": txn.amount,
        "currency": txn.currency,
        "status": txn.status,
        "description": txn.description,
        "buyer_email": txn.buyer_email,
        "seller_email": txn.seller_email,
        "metadata": txn.metadata_ or {},
        "created_at": txn.created_at.isoformat() + "Z" if txn.created_at else None,
        "updated_at": txn.updated_at.isoformat() + "Z" if txn.updated_at else None,
    }


def _event_to_dict(ev: TransactionEvent) -> dict:
    return {
        "id": ev.id,
        "transaction_id": ev.transaction_id,
        "event_type": ev.event_type,
        "title": ev.title,
        "description": ev.description,
        "source": ev.source,
        "details": ev.details or {},
        "occurred_at": ev.occurred_at.isoformat() + "Z" if ev.occurred_at else None,
    }


def _dispute_to_dict(d: Dispute) -> dict:
    return {
        "id": d.id,
        "transaction_id": d.transaction_id,
        "paypal_dispute_id": d.paypal_dispute_id,
        "reason": d.reason,
        "status": d.status,
        "evidence_submitted": d.evidence_submitted,
        "resolution": d.resolution,
        "created_at": d.created_at.isoformat() + "Z" if d.created_at else None,
        "updated_at": d.updated_at.isoformat() + "Z" if d.updated_at else None,
    }


def _canonicalize_pair(
    txn_a: Transaction,
    txn_b: Transaction,
) -> tuple[Transaction, Transaction, str, str]:
    """
    Assign deterministic chronological roles (`original`, `extra`) using `created_at`
    with transaction `id` as tie-breaker, plus sorted `(pair_low_id, pair_high_id)`.
    """
    key_a = (txn_a.created_at or datetime.min, txn_a.id)
    key_b = (txn_b.created_at or datetime.min, txn_b.id)
    if key_a <= key_b:
        original_txn, extra_txn = txn_a, txn_b
    else:
        original_txn, extra_txn = txn_b, txn_a
    pair_low_id, pair_high_id = sorted([txn_a.id, txn_b.id])
    return original_txn, extra_txn, pair_low_id, pair_high_id


def _load_review_map(db: Session) -> dict[tuple[str, str], DuplicateReview]:
    return {
        (r.pair_low_id, r.pair_high_id): r
        for r in db.query(DuplicateReview).all()
    }


def _is_active_duplicate_candidate(cand: dict, review: Optional[DuplicateReview]) -> bool:
    """
    Determine whether a duplicate candidate should trigger active duplicate alerts.
    Persisted `SEPARATE_ORDERS` decisions always suppress alert counts while keeping
    candidates available in investigation responses.
    """
    if review and review.decision == "SEPARATE_ORDERS":
        return False
    if (
        review
        and review.decision == "CONFIRMED_DUPLICATE"
        and cand.get("classification") not in {"RETRY_AFTER_FAILURE", "ALREADY_REFUNDED"}
    ):
        return True
    return cand.get("classification") == "POSSIBLE_DUPLICATE"


def _analyze_duplicates_for_txn(
    txn: Transaction,
    all_txns: List[Transaction],
    review_map: Optional[dict[tuple[str, str], DuplicateReview]] = None,
) -> tuple[list[dict], bool]:
    """
    Run duplicate detection for `txn` and enrich each candidate with chronological
    role (`ORIGINAL` or `EXTRA_CANDIDATE`) and persisted review state.
    """
    txn_by_id = {t.id: t for t in all_txns}
    reviews = review_map or {}
    candidates = status_engine.detect_duplicate_transactions(txn, all_txns)
    has_possible = False

    for cand in candidates:
        other = txn_by_id.get(cand["transaction_id"])
        review = None
        if other:
            _, extra_txn, pair_low, pair_high = _canonicalize_pair(txn, other)
            review = reviews.get((pair_low, pair_high))
            cand["role"] = "EXTRA_CANDIDATE" if extra_txn.id == other.id else "ORIGINAL"
        cand["review_decision"] = review.decision if review else "PENDING_REVIEW"
        cand["reviewer_verified"] = bool(review.reviewer_verified) if review else False
        if _is_active_duplicate_candidate(cand, review):
            has_possible = True

    return candidates, has_possible


def _serialize_transaction(
    txn: Transaction,
    all_txns: List[Transaction],
    review_map: Optional[dict[tuple[str, str], DuplicateReview]] = None,
) -> TransactionResponse:
    candidates, _ = _analyze_duplicates_for_txn(txn, all_txns, review_map)
    reviews = review_map or {}
    txn_by_id = {t.id: t for t in all_txns}
    active_count = 0
    for c in candidates:
        other = txn_by_id.get(c["transaction_id"])
        review = None
        if other:
            _, _, low_id, high_id = _canonicalize_pair(txn, other)
            review = reviews.get((low_id, high_id))
        if _is_active_duplicate_candidate(c, review):
            active_count += 1
    return TransactionResponse(
        id=txn.id,
        paypal_transaction_id=txn.paypal_transaction_id,
        merchant_id=txn.merchant_id,
        amount=txn.amount,
        currency=txn.currency,
        status=txn.status,
        description=txn.description,
        buyer_email=txn.buyer_email,
        seller_email=txn.seller_email,
        metadata=txn.metadata_ or {},
        created_at=txn.created_at,
        updated_at=txn.updated_at,
        possible_duplicate_count=active_count,
        dispute_count=len(txn.disputes or []),
    )


def _serialize_dispute(d: Dispute) -> DisputeResponse:
    priority = None
    if d.transaction:
        priority = status_engine.compute_dispute_priority(d, d.transaction).value
    case_id = d.resolution_cases[0].id if d.resolution_cases else None
    return DisputeResponse(
        id=d.id,
        transaction_id=d.transaction_id,
        paypal_dispute_id=d.paypal_dispute_id,
        reason=d.reason,
        status=d.status,
        evidence_submitted=d.evidence_submitted,
        resolution=d.resolution,
        created_at=d.created_at,
        updated_at=d.updated_at,
        priority=priority,
        resolution_case_id=case_id,
    )


def _serialize_case(c: ResolutionCase) -> ResolutionCaseResponse:
    breached, sla_info = status_engine.check_sla_breach(c)
    sla_due = status_engine.get_sla_deadline(c)
    return ResolutionCaseResponse(
        id=c.id,
        dispute_id=c.dispute_id,
        assigned_agent=c.assigned_agent,
        priority=c.priority,
        ai_recommendation=c.ai_recommendation,
        steps_taken=c.steps_taken or [],
        outcome=c.outcome,
        created_at=c.created_at,
        updated_at=c.updated_at,
        sla_breached=breached,
        sla_info=sla_info,
        sla_due_at=sla_due,
    )


def _find_transaction(db: Session, transaction_id: str) -> Optional[Transaction]:
    txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not txn:
        txn = db.query(Transaction).filter(
            Transaction.paypal_transaction_id == transaction_id
        ).first()
    return txn


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

def _seed_database(db: Session) -> None:
    """Create realistic sample transactions, timelines, duplicate pair, disputes, and cases."""
    if db.query(Transaction).count() > 0:
        return

    now = datetime.utcnow()

    transactions_data = [
        {
            "id": str(uuid.uuid4()),
            "paypal_transaction_id": "TXN-2024-001",
            "merchant_id": "MERCH-001",
            "amount": 349.99,
            "currency": "USD",
            "status": "COMPLETED",
            "description": "MacBook Pro case and accessories",
            "buyer_email": "alice.johnson@example.com",
            "seller_email": "tech-store@merchant.com",
            "metadata_": {"order_id": "ORD-10001", "shipping": "express", "payment_method": "PayPal Balance"},
            "created_at": now - timedelta(days=10),
            "updated_at": now - timedelta(days=10) + timedelta(seconds=6),
        },
        {
            "id": str(uuid.uuid4()),
            "paypal_transaction_id": "TXN-2024-007",
            "merchant_id": "MERCH-001",
            "amount": 349.99,
            "currency": "USD",
            "status": "COMPLETED",
            "description": "MacBook Pro case and accessories (checkout retry)",
            "buyer_email": "alice.johnson@example.com",
            "seller_email": "tech-store@merchant.com",
            "metadata_": {"order_id": "ORD-10001-B", "shipping": "express", "payment_method": "PayPal Balance"},
            "created_at": now - timedelta(days=10) + timedelta(minutes=3),
            "updated_at": now - timedelta(days=10) + timedelta(minutes=3, seconds=4),
        },
        {
            "id": str(uuid.uuid4()),
            "paypal_transaction_id": "TXN-2024-002",
            "merchant_id": "MERCH-002",
            "amount": 89.99,
            "currency": "USD",
            "status": "DISPUTED",
            "description": "Designer handbag",
            "buyer_email": "bob.smith@example.com",
            "seller_email": "fashion-hub@merchant.com",
            "metadata_": {"order_id": "ORD-10002", "category": "fashion"},
            "created_at": now - timedelta(days=5),
            "updated_at": now - timedelta(days=1),
        },
        {
            "id": str(uuid.uuid4()),
            "paypal_transaction_id": "TXN-2024-003",
            "merchant_id": "MERCH-001",
            "amount": 1299.00,
            "currency": "USD",
            "status": "DISPUTED",
            "description": "Laptop computer - 16-inch",
            "buyer_email": "carol.white@example.com",
            "seller_email": "tech-store@merchant.com",
            "metadata_": {"order_id": "ORD-10003", "serial": "LPT2024XYZ"},
            "created_at": now - timedelta(days=8),
            "updated_at": now - timedelta(days=2),
        },
        {
            "id": str(uuid.uuid4()),
            "paypal_transaction_id": "TXN-2024-004",
            "merchant_id": "MERCH-003",
            "amount": 25.00,
            "currency": "USD",
            "status": "REFUNDED",
            "description": "Subscription - monthly plan",
            "buyer_email": "dave.brown@example.com",
            "seller_email": "saas-platform@merchant.com",
            "metadata_": {"plan": "basic", "billing_cycle": "monthly", "refund_id": "REF-2024-004"},
            "created_at": now - timedelta(days=15),
            "updated_at": now - timedelta(days=12),
        },
        {
            "id": str(uuid.uuid4()),
            "paypal_transaction_id": "TXN-2024-005",
            "merchant_id": "MERCH-002",
            "amount": 450.00,
            "currency": "EUR",
            "status": "PENDING",
            "description": "Luxury watch - pre-order",
            "buyer_email": "emma.davis@example.com",
            "seller_email": "luxury-goods@merchant.com",
            "metadata_": {"order_id": "ORD-10005", "expected_ship": "2026-12-01", "pending_reason": "ECHECK"},
            "created_at": now - timedelta(days=1),
            "updated_at": now - timedelta(hours=2),
        },
        {
            "id": str(uuid.uuid4()),
            "paypal_transaction_id": "TXN-2024-006",
            "merchant_id": "MERCH-004",
            "amount": 199.99,
            "currency": "USD",
            "status": "FAILED",
            "description": "Gaming headset",
            "buyer_email": "frank.miller@example.com",
            "seller_email": "gaming-gear@merchant.com",
            "metadata_": {"error_code": "INSUFFICIENT_FUNDS"},
            "created_at": now - timedelta(days=3),
            "updated_at": now - timedelta(days=3) + timedelta(seconds=3),
        },
    ]

    txn_by_pp_id: dict[str, Transaction] = {}
    for data in transactions_data:
        txn = Transaction(**data)
        db.add(txn)
        txn_by_pp_id[txn.paypal_transaction_id] = txn
    db.commit()

    # Seed realistic per-transaction timeline events
    events_seed: list[TransactionEvent] = []
    for pp_id, txn in txn_by_pp_id.items():
        base_t = txn.created_at
        events_seed.append(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=txn.id,
                event_type="PAYMENT_CREATED",
                title="Checkout order created",
                description=f"Order initiated by {txn.buyer_email} for {txn.amount:.2f} {txn.currency}.",
                source="database",
                details={"paypal_transaction_id": pp_id},
                occurred_at=base_t,
            )
        )
        if txn.status in {"COMPLETED", "DISPUTED", "REFUNDED"}:
            events_seed.append(
                TransactionEvent(
                    id=str(uuid.uuid4()),
                    transaction_id=txn.id,
                    event_type="PAYMENT_CAPTURED",
                    title="Payment captured",
                    description=f"Funds ({txn.amount:.2f} {txn.currency}) captured for merchant {txn.merchant_id}.",
                    source="database",
                    details={"status": "COMPLETED"},
                    occurred_at=base_t + timedelta(seconds=5),
                )
            )
        if txn.status == "PENDING":
            events_seed.append(
                TransactionEvent(
                    id=str(uuid.uuid4()),
                    transaction_id=txn.id,
                    event_type="PAYMENT_PENDING",
                    title="Settlement pending",
                    description="Payment awaiting eCheck / funding clearing before capture completion.",
                    source="database",
                    details=txn.metadata_ or {},
                    occurred_at=base_t + timedelta(seconds=4),
                )
            )
        if txn.status == "FAILED":
            err = (txn.metadata_ or {}).get("error_code", "PROCESSOR_DECLINE")
            events_seed.append(
                TransactionEvent(
                    id=str(uuid.uuid4()),
                    transaction_id=txn.id,
                    event_type="PAYMENT_FAILED",
                    title=f"Capture declined ({err})",
                    description=f"Authorization failed with recorded error code {err}. No funds captured.",
                    source="database",
                    details={"error_code": err},
                    occurred_at=base_t + timedelta(seconds=3),
                )
            )
        if txn.status == "REFUNDED":
            events_seed.append(
                TransactionEvent(
                    id=str(uuid.uuid4()),
                    transaction_id=txn.id,
                    event_type="REFUND_ISSUED",
                    title="Full refund completed",
                    description=f"Refunded {txn.amount:.2f} {txn.currency} to {txn.buyer_email}.",
                    source="database",
                    details={"refund_id": (txn.metadata_ or {}).get("refund_id")},
                    occurred_at=txn.updated_at,
                )
            )
        if txn.status == "DISPUTED":
            events_seed.append(
                TransactionEvent(
                    id=str(uuid.uuid4()),
                    transaction_id=txn.id,
                    event_type="DISPUTE_OPENED",
                    title="Buyer dispute opened",
                    description="Transaction placed on hold following PayPal dispute filing.",
                    source="webhook",
                    details={},
                    occurred_at=txn.updated_at,
                )
            )

    db.add_all(events_seed)
    db.commit()

    disputes_data = [
        {
            "id": str(uuid.uuid4()),
            "transaction_id": txn_by_pp_id["TXN-2024-002"].id,
            "paypal_dispute_id": "DSP-2024-001",
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            "status": "OPEN",
            "evidence_submitted": False,
            "resolution": None,
            "created_at": now - timedelta(days=4),
            "updated_at": now - timedelta(hours=12),
        },
        {
            "id": str(uuid.uuid4()),
            "transaction_id": txn_by_pp_id["TXN-2024-003"].id,
            "paypal_dispute_id": "DSP-2024-002",
            "reason": "SIGNIFICANTLY_NOT_AS_DESCRIBED",
            "status": "UNDER_REVIEW",
            "evidence_submitted": True,
            "resolution": None,
            "created_at": now - timedelta(days=7),
            "updated_at": now - timedelta(hours=6),
        },
    ]

    dispute_objects: list[Dispute] = []
    for data in disputes_data:
        d = Dispute(**data)
        db.add(d)
        dispute_objects.append(d)
    db.commit()

    cases_data = [
        {
            "id": str(uuid.uuid4()),
            "dispute_id": dispute_objects[0].id,
            "assigned_agent": "agent.sarah@company.com",
            "priority": "HIGH",
            "ai_recommendation": (
                "Request proof of delivery from seller. "
                "If tracking confirmation is unavailable within 48 hours, process full refund."
            ),
            "steps_taken": [
                {
                    "timestamp": (now - timedelta(days=3)).isoformat() + "Z",
                    "action": "CASE_OPENED",
                    "notes": "Dispute received and assigned to Sarah",
                    "agent": "system",
                },
                {
                    "timestamp": (now - timedelta(days=2)).isoformat() + "Z",
                    "action": "SELLER_CONTACTED",
                    "notes": "Requested shipping confirmation from seller",
                    "agent": "agent.sarah@company.com",
                },
            ],
            "outcome": None,
            "created_at": now - timedelta(days=3),
            "updated_at": now - timedelta(hours=12),
        },
        {
            "id": str(uuid.uuid4()),
            "dispute_id": dispute_objects[1].id,
            "assigned_agent": "agent.mike@company.com",
            "priority": "MEDIUM",
            "ai_recommendation": (
                "Evidence submitted by both parties. Compare buyer photos against original listing."
            ),
            "steps_taken": [
                {
                    "timestamp": (now - timedelta(days=6)).isoformat() + "Z",
                    "action": "CASE_OPENED",
                    "notes": "Dispute received and assigned to Mike",
                    "agent": "system",
                },
                {
                    "timestamp": (now - timedelta(days=5)).isoformat() + "Z",
                    "action": "EVIDENCE_REQUESTED",
                    "notes": "Requested evidence from both buyer and seller",
                    "agent": "agent.mike@company.com",
                },
                {
                    "timestamp": (now - timedelta(days=3)).isoformat() + "Z",
                    "action": "EVIDENCE_RECEIVED",
                    "notes": "Buyer submitted photos; seller submitted product listing",
                    "agent": "agent.mike@company.com",
                },
            ],
            "outcome": None,
            "created_at": now - timedelta(days=6),
            "updated_at": now - timedelta(hours=6),
        },
    ]

    for data in cases_data:
        db.add(ResolutionCase(**data))
    db.commit()


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    env = os.getenv("ENVIRONMENT", "development").lower()
    if env in {"production", "prod"}:
        verify_enabled = os.getenv("PAYPAL_VERIFY_WEBHOOKS", "false").strip().lower() in {"1", "true", "yes", "on"}
        webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
        if not verify_enabled or not webhook_id:
            raise RuntimeError(
                "CRITICAL SECURITY ERROR: Production deployment detected without PayPal webhook verification enabled "
                "('PAYPAL_VERIFY_WEBHOOKS=true' and 'PAYPAL_WEBHOOK_ID' are required). Refusing to start in insecure mode."
            )
    init_db()
    db = SessionLocal()
    try:
        _seed_database(db)
    finally:
        db.close()
    yield


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PayPal Transaction Resolution Copilot",
    description="Transaction state explanation, payment timelines, duplicate detection, support summaries, and dispute resolution",
    version="1.1.0",
    lifespan=lifespan,
)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health & Analytics
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "version": "1.1.0",
        "services": {
            "paypal": paypal_service.environment_label,
            "ai": "openai" if ai_service._has_key else "heuristic",
            "webhook_verification": (
                "paypal_api"
                if (PAYPAL_VERIFY_WEBHOOKS and not paypal_service._mock_mode and PAYPAL_WEBHOOK_ID)
                else "disabled_dev_mode"
            ),
        },
    }


@app.get("/api/analytics/summary", tags=["Analytics"])
def get_analytics_summary(db: Session = Depends(get_db)):
    all_txns = db.query(Transaction).all()
    all_disputes = db.query(Dispute).all()
    all_cases = db.query(ResolutionCase).all()
    recent_events = (
        db.query(TransactionEvent)
        .order_by(TransactionEvent.occurred_at.desc())
        .limit(8)
        .all()
    )

    txn_status_counts: dict[str, int] = {}
    for t in all_txns:
        txn_status_counts[t.status] = txn_status_counts.get(t.status, 0) + 1

    dispute_status_counts: dict[str, int] = {}
    for d in all_disputes:
        dispute_status_counts[d.status] = dispute_status_counts.get(d.status, 0) + 1

    # Count unique active duplicate pairs ordered chronologically (ORIGINAL -> EXTRA)
    review_map = _load_review_map(db)
    txn_by_id = {t.id: t for t in all_txns}
    seen_pairs: set[tuple[str, str]] = set()
    duplicate_alerts: list[dict] = []
    for t in all_txns:
        dups, _ = _analyze_duplicates_for_txn(t, all_txns, review_map)
        for dup in dups:
            other_txn = txn_by_id.get(dup["transaction_id"])
            if not other_txn:
                continue
            orig_txn, extra_txn, low_id, high_id = _canonicalize_pair(t, other_txn)
            review = review_map.get((low_id, high_id))
            if not _is_active_duplicate_candidate(dup, review):
                continue
            pair = (low_id, high_id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                duplicate_alerts.append({
                    "primary_id": orig_txn.id,
                    "primary_paypal_id": orig_txn.paypal_transaction_id,
                    "duplicate_id": extra_txn.id,
                    "duplicate_paypal_id": extra_txn.paypal_transaction_id,
                    "amount": extra_txn.amount,
                    "currency": extra_txn.currency,
                    "buyer_email": extra_txn.buyer_email,
                    "confidence": dup["confidence"],
                    "time_diff_seconds": dup["time_diff_seconds"],
                    "review_decision": review.decision if review else "PENDING_REVIEW",
                })

    sla_breached = sum(1 for c in all_cases if status_engine.check_sla_breach(c)[0])
    attention_needed = sum(
        1 for t in all_txns if t.status in {"PENDING", "FAILED", "DISPUTED"}
    )

    return {
        "total_transactions": len(all_txns),
        "attention_needed_count": attention_needed,
        "duplicate_alerts_count": len(duplicate_alerts),
        "duplicate_alerts": duplicate_alerts,
        "transaction_status_breakdown": [
            {"name": k, "value": v} for k, v in txn_status_counts.items()
        ],
        "open_disputes": sum(1 for d in all_disputes if d.status in {"OPEN", "UNDER_REVIEW"}),
        "total_disputes": len(all_disputes),
        "dispute_status_breakdown": [
            {"name": k, "value": v} for k, v in dispute_status_counts.items()
        ],
        "total_cases": len(all_cases),
        "sla_breached_cases": sla_breached,
        "recent_activity": [_event_to_dict(ev) for ev in recent_events],
        "services": {
            "paypal": paypal_service.environment_label,
            "ai": "openai" if ai_service._has_key else "heuristic",
        },
    }


# ---------------------------------------------------------------------------
# Transactions & Core Resolution Workflow
# ---------------------------------------------------------------------------

@app.get("/api/transactions", response_model=PaginatedTransactionsResponse, tags=["Transactions"])
def list_transactions(
    search: Optional[str] = Query(None, description="Search by ID, PayPal ID, buyer email, or description"),
    status: Optional[str] = Query(None, description="Filter by status"),
    merchant_id: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    all_txns = db.query(Transaction).all()
    q = db.query(Transaction)

    if status:
        q = q.filter(Transaction.status == status.upper())
    if merchant_id:
        q = q.filter(Transaction.merchant_id == merchant_id)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            or_(
                Transaction.id.ilike(term),
                Transaction.paypal_transaction_id.ilike(term),
                Transaction.buyer_email.ilike(term),
                Transaction.description.ilike(term),
            )
        )
    if date_from:
        try:
            start_dt = datetime.fromisoformat(date_from)
            q = q.filter(Transaction.created_at >= start_dt)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from format; expected YYYY-MM-DD")
    if date_to:
        try:
            end_dt = datetime.fromisoformat(date_to) + timedelta(days=1)
            q = q.filter(Transaction.created_at < end_dt)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to format; expected YYYY-MM-DD")

    total = q.count()
    items = q.order_by(Transaction.created_at.desc()).offset(skip).limit(limit).all()
    review_map = _load_review_map(db)

    return PaginatedTransactionsResponse(
        items=[_serialize_transaction(t, all_txns, review_map) for t in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@app.get("/api/transactions/{transaction_id}", response_model=TransactionResponse, tags=["Transactions"])
def get_transaction(transaction_id: str, db: Session = Depends(get_db)):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    all_txns = db.query(Transaction).all()
    review_map = _load_review_map(db)
    return _serialize_transaction(txn, all_txns, review_map)


@app.post("/api/transactions", response_model=TransactionResponse, status_code=201, tags=["Transactions"])
def create_transaction(payload: TransactionCreate, db: Session = Depends(get_db)):
    existing = db.query(Transaction).filter(
        Transaction.paypal_transaction_id == payload.paypal_transaction_id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Transaction with this PayPal ID already exists")

    now = datetime.utcnow()
    txn = Transaction(
        id=str(uuid.uuid4()),
        paypal_transaction_id=payload.paypal_transaction_id,
        merchant_id=payload.merchant_id,
        amount=payload.amount,
        currency=payload.currency.upper(),
        status=payload.status.upper(),
        description=payload.description,
        buyer_email=payload.buyer_email,
        seller_email=payload.seller_email,
        metadata_=payload.metadata_ or {},
        created_at=now,
        updated_at=now,
    )
    db.add(txn)
    db.flush()
    db.add(
        TransactionEvent(
            id=str(uuid.uuid4()),
            transaction_id=txn.id,
            event_type="TRANSACTION_RECORDED",
            title="Transaction recorded in Copilot",
            description=f"Recorded {txn.paypal_transaction_id} ({txn.status}) for {txn.amount:.2f} {txn.currency}.",
            source="database",
            details={"status": txn.status},
            occurred_at=now,
        )
    )
    db.commit()
    db.refresh(txn)
    all_txns = db.query(Transaction).all()
    review_map = _load_review_map(db)
    return _serialize_transaction(txn, all_txns, review_map)


@app.get("/api/transactions/{transaction_id}/timeline", response_model=List[TransactionEventResponse], tags=["Transactions"])
def get_transaction_timeline(transaction_id: str, db: Session = Depends(get_db)):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn.events


@app.get("/api/transactions/{transaction_id}/duplicates", response_model=DuplicateAnalysisResponse, tags=["Transactions"])
def get_transaction_duplicates(transaction_id: str, db: Session = Depends(get_db)):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    all_txns = db.query(Transaction).all()
    review_map = _load_review_map(db)
    candidates, has_possible = _analyze_duplicates_for_txn(txn, all_txns, review_map)
    return DuplicateAnalysisResponse(
        transaction_id=txn.id,
        has_possible_duplicates=has_possible,
        candidates=candidates,
    )


def _to_side_by_side_summary(txn: Transaction, role: str) -> DuplicateSideBySideTransaction:
    total_refunded, remaining, _ = get_refund_summary(txn.amount, txn.status, txn.metadata_)
    return DuplicateSideBySideTransaction(
        id=txn.id,
        paypal_transaction_id=txn.paypal_transaction_id,
        status=txn.status,
        amount=txn.amount,
        currency=txn.currency,
        order_id=extract_order_id(txn),
        description=txn.description,
        buyer_email=txn.buyer_email,
        merchant_id=txn.merchant_id,
        created_at=txn.created_at,
        total_refunded=total_refunded,
        remaining_refundable=remaining,
        role=role,
        refund_eligible=(txn.status == "COMPLETED" and remaining > 0.005),
    )


def _build_review_response(
    review: Optional[DuplicateReview],
    pair_low_id: str,
    pair_high_id: str,
    original_txn: Transaction,
    extra_txn: Transaction,
) -> DuplicateReviewResponse:
    if review is None:
        return DuplicateReviewResponse(
            id=None,
            pair_low_id=pair_low_id,
            pair_high_id=pair_high_id,
            original_transaction_id=original_txn.id,
            extra_transaction_id=extra_txn.id,
            decision="PENDING_REVIEW",
            reviewer=None,
            reviewer_verified=False,
            notes=None,
            decision_history=[],
            created_at=None,
            updated_at=None,
        )
    return DuplicateReviewResponse(
        id=review.id,
        pair_low_id=review.pair_low_id,
        pair_high_id=review.pair_high_id,
        original_transaction_id=review.original_transaction_id,
        extra_transaction_id=review.extra_transaction_id,
        decision=review.decision,
        reviewer=review.reviewer,
        reviewer_verified=bool(review.reviewer_verified),
        notes=review.notes,
        decision_history=list(review.decision_history or []),
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _build_side_by_side_comparison(
    orig_txn: Transaction,
    extra_txn: Transaction,
) -> dict:
    # Run detector on just this pair with a wide window so direct investigation always works
    pair_matches = status_engine.detect_duplicate_transactions(
        orig_txn,
        [orig_txn, extra_txn],
        window_hours=24 * 3650,
    )
    cand = pair_matches[0] if pair_matches else None

    orig_order = extract_order_id(orig_txn)
    extra_order = extract_order_id(extra_txn)
    diff_seconds = (
        int(abs((extra_txn.created_at - orig_txn.created_at).total_seconds()))
        if (orig_txn.created_at and extra_txn.created_at)
        else 0
    )

    def _row_status(val_a, val_b, case_insensitive: bool = False) -> str:
        if not val_a or not val_b:
            return "UNAVAILABLE"
        a = str(val_a).strip()
        b = str(val_b).strip()
        if case_insensitive:
            a, b = a.lower(), b.lower()
        return "MATCH" if a == b else "DIFFERENT"

    order_status = "UNAVAILABLE"
    if orig_order and extra_order:
        if cand and cand.get("order_relationship") == "EXACT_MATCH":
            order_status = "MATCH"
        elif cand and cand.get("order_relationship") == "POSSIBLE_RETRY_VARIANT":
            order_status = "VARIANT"
        elif orig_order.strip().upper() == extra_order.strip().upper():
            order_status = "MATCH"
        else:
            order_status = "DIFFERENT"

    field_matrix = [
        {
            "field": "PayPal Transaction ID",
            "original_value": orig_txn.paypal_transaction_id,
            "extra_value": extra_txn.paypal_transaction_id,
            "match_status": "DIFFERENT",
        },
        {
            "field": "Order ID",
            "original_value": orig_order,
            "extra_value": extra_order,
            "match_status": order_status,
        },
        {
            "field": "Amount",
            "original_value": f"{orig_txn.amount:.2f} {orig_txn.currency}",
            "extra_value": f"{extra_txn.amount:.2f} {extra_txn.currency}",
            "match_status": (
                "MATCH"
                if abs(orig_txn.amount - extra_txn.amount) <= 0.01
                and (orig_txn.currency or "").upper() == (extra_txn.currency or "").upper()
                else "DIFFERENT"
            ),
        },
        {
            "field": "Status",
            "original_value": orig_txn.status,
            "extra_value": extra_txn.status,
            "match_status": "MATCH" if orig_txn.status == extra_txn.status else "DIFFERENT",
        },
        {
            "field": "Buyer Email",
            "original_value": orig_txn.buyer_email,
            "extra_value": extra_txn.buyer_email,
            "match_status": _row_status(orig_txn.buyer_email, extra_txn.buyer_email, case_insensitive=True),
        },
        {
            "field": "Merchant ID",
            "original_value": orig_txn.merchant_id,
            "extra_value": extra_txn.merchant_id,
            "match_status": _row_status(orig_txn.merchant_id, extra_txn.merchant_id, case_insensitive=True),
        },
        {
            "field": "Description",
            "original_value": orig_txn.description,
            "extra_value": extra_txn.description,
            "match_status": _row_status(orig_txn.description, extra_txn.description, case_insensitive=True),
        },
        {
            "field": "Timestamp (UTC)",
            "original_value": orig_txn.created_at.isoformat() + "Z" if orig_txn.created_at else None,
            "extra_value": extra_txn.created_at.isoformat() + "Z" if extra_txn.created_at else None,
            "match_status": "MATCH" if diff_seconds == 0 else "DIFFERENT",
        },
    ]

    differences = [
        f"{row['field']}: {row['original_value'] or 'Unavailable'} vs {row['extra_value'] or 'Unavailable'}"
        for row in field_matrix
        if row["match_status"] in {"DIFFERENT", "VARIANT"}
    ]
    matching_fields = (
        list(cand["matching_fields"])
        if cand
        else [
            row["field"].lower().replace(" ", "_")
            for row in field_matrix
            if row["match_status"] == "MATCH"
        ]
    )

    return {
        "time_diff_seconds": diff_seconds,
        "classification": cand["classification"] if cand else "UNMATCHED_CRITERIA",
        "confidence": cand["confidence"] if cand else "LOW",
        "order_relationship": cand.get("order_relationship") if cand else order_status,
        "matching_fields": matching_fields,
        "differences": differences,
        "field_matrix": field_matrix,
        "explanation": (
            cand["explanation"]
            if cand
            else "Transactions differ on core matching attributes (buyer, merchant, amount, currency, or time window)."
        ),
        "recommended_action": (
            cand["recommended_action"]
            if cand
            else "Verify both transactions independently before taking action."
        ),
    }


def _resolve_pair_or_400(
    db: Session,
    transaction_id: str,
    other_transaction_id: str,
) -> tuple[Transaction, Transaction, str, str]:
    txn_a = _find_transaction(db, transaction_id)
    if not txn_a:
        raise HTTPException(status_code=404, detail=f"Transaction '{transaction_id}' not found")
    txn_b = _find_transaction(db, other_transaction_id)
    if not txn_b:
        raise HTTPException(status_code=404, detail=f"Transaction '{other_transaction_id}' not found")
    if txn_a.id == txn_b.id:
        raise HTTPException(
            status_code=422,
            detail="Cannot investigate or review a transaction against itself",
        )
    return _canonicalize_pair(txn_a, txn_b)


def _add_review_audit_events(
    db: Session,
    original_txn: Transaction,
    extra_txn: Transaction,
    decision: str,
    previous_decision: Optional[str],
    reviewer: str,
    override_reason: Optional[str],
    notes: Optional[str],
    now: datetime,
) -> None:
    is_override = previous_decision is not None and previous_decision != "PENDING_REVIEW"
    title = (
        f"Duplicate review overridden: {decision}"
        if is_override
        else f"Duplicate review: {decision}"
    )
    reason_part = f" Override reason: {override_reason}." if override_reason else ""
    notes_part = f" Notes: {notes}." if notes else ""
    description = (
        f"Pair ({original_txn.paypal_transaction_id} [ORIGINAL] / "
        f"{extra_txn.paypal_transaction_id} [EXTRA_CANDIDATE]) marked {decision} "
        f"by {reviewer} (verified=false).{reason_part}{notes_part}"
    )
    details = {
        "original_transaction_id": original_txn.id,
        "extra_transaction_id": extra_txn.id,
        "decision": decision,
        "previous_decision": previous_decision,
        "override_reason": override_reason,
        "reviewer": reviewer,
        "reviewer_verified": False,
        "notes": notes,
    }
    for txn_id in (original_txn.id, extra_txn.id):
        db.add(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=txn_id,
                event_type="DUPLICATE_REVIEWED",
                title=title,
                description=description,
                source="review",
                details=details,
                occurred_at=now,
            )
        )


@app.get(
    "/api/transactions/{transaction_id}/duplicates/{other_transaction_id}/investigation",
    response_model=DuplicateInvestigationResponse,
    tags=["Transactions"],
)
def get_duplicate_investigation(
    transaction_id: str,
    other_transaction_id: str,
    db: Session = Depends(get_db),
):
    orig_txn, extra_txn, low_id, high_id = _resolve_pair_or_400(
        db, transaction_id, other_transaction_id
    )
    review = (
        db.query(DuplicateReview)
        .filter(
            DuplicateReview.pair_low_id == low_id,
            DuplicateReview.pair_high_id == high_id,
        )
        .first()
    )
    return DuplicateInvestigationResponse(
        pair_low_id=low_id,
        pair_high_id=high_id,
        original_transaction=_to_side_by_side_summary(orig_txn, "ORIGINAL"),
        extra_candidate=_to_side_by_side_summary(extra_txn, "EXTRA_CANDIDATE"),
        comparison=_build_side_by_side_comparison(orig_txn, extra_txn),
        review=_build_review_response(review, low_id, high_id, orig_txn, extra_txn),
    )


@app.post(
    "/api/transactions/{transaction_id}/duplicates/{other_transaction_id}/review",
    response_model=DuplicateReviewResponse,
    tags=["Transactions"],
)
def submit_duplicate_review(
    transaction_id: str,
    other_transaction_id: str,
    payload: DuplicateReviewRequest,
    db: Session = Depends(get_db),
):
    """
    Persist a human duplicate investigation decision (`CONFIRMED_DUPLICATE` or
    `SEPARATE_ORDERS`) and append an audit entry to both transactions.
    Never triggers refunds. `reviewer_verified` is always False without auth.
    """
    orig_txn, extra_txn, low_id, high_id = _resolve_pair_or_400(
        db, transaction_id, other_transaction_id
    )
    decision = payload.decision
    reviewer = (payload.reviewer or "").strip() or "unauthenticated-agent"
    notes = (payload.notes or "").strip() or None
    override_reason = (payload.override_reason or "").strip() or None
    now = datetime.utcnow()

    existing = (
        db.query(DuplicateReview)
        .filter(
            DuplicateReview.pair_low_id == low_id,
            DuplicateReview.pair_high_id == high_id,
        )
        .first()
    )

    if existing is None:
        entry = {
            "decision": decision,
            "previous_decision": "PENDING_REVIEW",
            "override_reason": override_reason,
            "notes": notes,
            "reviewer": reviewer,
            "reviewer_verified": False,
            "recorded_at": now.isoformat() + "Z",
        }
        review = DuplicateReview(
            id=str(uuid.uuid4()),
            pair_low_id=low_id,
            pair_high_id=high_id,
            original_transaction_id=orig_txn.id,
            extra_transaction_id=extra_txn.id,
            decision=decision,
            reviewer=reviewer,
            reviewer_verified=False,
            notes=notes,
            decision_history=[entry],
            created_at=now,
            updated_at=now,
        )
        db.add(review)
        _add_review_audit_events(
            db, orig_txn, extra_txn, decision, "PENDING_REVIEW", reviewer, override_reason, notes, now
        )
        try:
            db.commit()
            db.refresh(review)
            return _build_review_response(review, low_id, high_id, orig_txn, extra_txn)
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(DuplicateReview)
                .filter(
                    DuplicateReview.pair_low_id == low_id,
                    DuplicateReview.pair_high_id == high_id,
                )
                .first()
            )
            if existing is None:
                raise HTTPException(status_code=409, detail="Concurrent review conflict; please retry")
            if existing.decision == decision:
                raise HTTPException(
                    status_code=409,
                    detail=f"Duplicate review decision '{decision}' was already recorded for this pair",
                )
            if not override_reason:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Pair was concurrently reviewed as '{existing.decision}'; "
                        "provide override_reason to reverse it"
                    ),
                )

    # Updating / overriding an existing review record
    prev_decision = existing.decision
    if prev_decision == decision:
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate review decision '{decision}' has already been recorded for this pair",
        )
    if prev_decision != "PENDING_REVIEW" and not override_reason:
        raise HTTPException(
            status_code=422,
            detail="override_reason is required when reversing an existing review decision",
        )

    entry = {
        "decision": decision,
        "previous_decision": prev_decision,
        "override_reason": override_reason,
        "notes": notes,
        "reviewer": reviewer,
        "reviewer_verified": False,
        "recorded_at": now.isoformat() + "Z",
    }
    new_history = [*list(existing.decision_history or []), entry]
    updated_rows = (
        db.query(DuplicateReview)
        .filter(
            DuplicateReview.id == existing.id,
            DuplicateReview.decision == prev_decision,
        )
        .update(
            {
                "original_transaction_id": orig_txn.id,
                "extra_transaction_id": extra_txn.id,
                "decision": decision,
                "reviewer": reviewer,
                "reviewer_verified": False,
                "notes": notes,
                "decision_history": new_history,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    if updated_rows == 0:
        db.rollback()
        raise HTTPException(status_code=409, detail="Concurrent review state changed; please reload and retry")

    _add_review_audit_events(
        db, orig_txn, extra_txn, decision, prev_decision, reviewer, override_reason, notes, now
    )
    db.commit()
    refreshed = db.query(DuplicateReview).filter(DuplicateReview.id == existing.id).first()
    return _build_review_response(refreshed, low_id, high_id, orig_txn, extra_txn)


@app.get("/api/transactions/{transaction_id}/resolution", tags=["Transactions"])
async def get_transaction_resolution(transaction_id: str, db: Session = Depends(get_db)):
    """
    Single cohesive endpoint powering the Transaction Resolution Workspace:
    verified record, payment timeline, duplicate detection, state explanation, and linked disputes.
    """
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    all_txns = db.query(Transaction).all()
    review_map = _load_review_map(db)
    txn_serialized = _serialize_transaction(txn, all_txns, review_map)
    timeline = [_event_to_dict(ev) for ev in txn.events]
    duplicates, has_possible = _analyze_duplicates_for_txn(txn, all_txns, review_map)
    disputes = [_dispute_to_dict(d) for d in txn.disputes]
    explanation = await ai_service.explain_transaction(
        _txn_to_dict(txn), timeline, duplicates, disputes
    )

    return {
        "transaction": txn_serialized,
        "timeline": timeline,
        "duplicates": {
            "transaction_id": txn.id,
            "has_possible_duplicates": has_possible,
            "candidates": duplicates,
        },
        "explanation": explanation,
        "disputes": [_serialize_dispute(d) for d in txn.disputes],
        "paypal_mode": paypal_service.environment_label,
    }


@app.post("/api/transactions/{transaction_id}/explain", response_model=TransactionStateExplanation, tags=["Transactions"])
async def explain_transaction(transaction_id: str, db: Session = Depends(get_db)):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    all_txns = db.query(Transaction).all()
    timeline = [_event_to_dict(ev) for ev in txn.events]
    duplicates = status_engine.detect_duplicate_transactions(txn, all_txns)
    disputes = [_dispute_to_dict(d) for d in txn.disputes]
    return await ai_service.explain_transaction(_txn_to_dict(txn), timeline, duplicates, disputes)


@app.post("/api/transactions/{transaction_id}/support-summary", response_model=SupportSummaryResponse, tags=["Transactions"])
async def create_support_summary(
    transaction_id: str,
    payload: SupportSummaryRequest = Body(default_factory=SupportSummaryRequest),
    db: Session = Depends(get_db),
):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    all_txns = db.query(Transaction).all()
    timeline = [_event_to_dict(ev) for ev in txn.events]
    duplicates = status_engine.detect_duplicate_transactions(txn, all_txns)
    disputes = [_dispute_to_dict(d) for d in txn.disputes]
    return await ai_service.generate_support_summary(
        transaction_data=_txn_to_dict(txn),
        timeline_events=timeline,
        duplicate_candidates=duplicates,
        disputes=disputes,
        audience=payload.audience,
        agent_notes=payload.agent_notes,
    )


@app.post("/api/transactions/{transaction_id}/refund", tags=["Transactions"])
async def refund_transaction(
    transaction_id: str,
    payload: RefundRequest = Body(default_factory=RefundRequest),
    db: Session = Depends(get_db),
):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if txn.status == "REFUNDED":
        raise HTTPException(status_code=409, detail="Transaction has already been refunded")
    if txn.status != "COMPLETED":
        raise HTTPException(
            status_code=422,
            detail=f"Only COMPLETED transactions can be refunded directly (current status: {txn.status})",
        )

    already_refunded, remaining, _ = get_refund_summary(txn.amount, txn.status, txn.metadata_)
    if remaining <= 0.005:
        raise HTTPException(status_code=409, detail="Transaction has no remaining refundable balance")

    refund_amount = round(float(payload.amount), 2) if payload.amount is not None else remaining
    if refund_amount <= 0:
        raise HTTPException(status_code=422, detail="Invalid refund amount")
    if refund_amount > remaining + 0.005:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Refund amount {refund_amount:.2f} {txn.currency} exceeds remaining refundable balance "
                f"({remaining:.2f} {txn.currency}; already refunded: {already_refunded:.2f} {txn.currency})"
            ),
        )

    try:
        paypal_res = await paypal_service.issue_refund(
            capture_id=txn.paypal_transaction_id,
            amount=refund_amount,
            currency=txn.currency,
            note_to_payer=payload.reason or "Refund issued by merchant",
        )
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    now = datetime.utcnow()
    refund_state = record_refund_state(
        txn.amount,
        txn.status,
        txn.metadata_,
        refund_id=paypal_res.get("id"),
        amount=refund_amount,
        currency=txn.currency,
        source="mock" if paypal_res.get("is_simulated") else "paypal_api",
        refund_status=paypal_res.get("status", "COMPLETED"),
        is_simulated=bool(paypal_res.get("is_simulated")),
        webhook_confirmed=False,
    )
    txn.status = refund_state["new_status"]
    txn.updated_at = now
    txn.metadata_ = refund_state["metadata"]

    is_partial = refund_state["is_partial"]
    event_title = (
        ("Partial refund issued" if is_partial else "Refund issued")
        + (" (Simulated)" if paypal_res.get("is_simulated") else "")
    )
    balance_note = (
        f" [{refund_state['total_refunded']:.2f}/{txn.amount:.2f} {txn.currency} refunded total]"
        if is_partial
        else ""
    )

    db.add(
        TransactionEvent(
            id=str(uuid.uuid4()),
            transaction_id=txn.id,
            event_type="REFUND_ISSUED",
            title=event_title,
            description=(
                f"Refund of {refund_amount:.2f} {txn.currency} recorded "
                f"(ID: {paypal_res.get('id')}, mode: {paypal_res.get('mode')}).{balance_note}"
            ),
            source="mock" if paypal_res.get("is_simulated") else "paypal_api",
            details={
                "paypal_response": paypal_res,
                "reason": payload.reason,
                "refund_amount": refund_amount,
                "total_refunded": refund_state["total_refunded"],
                "remaining_refundable": refund_state["remaining_refundable"],
                "is_partial": is_partial,
            },
            occurred_at=now,
        )
    )
    db.commit()
    db.refresh(txn)
    all_txns = db.query(Transaction).all()
    review_map = _load_review_map(db)

    return {
        "transaction": _serialize_transaction(txn, all_txns, review_map),
        "paypal_response": paypal_res,
    }


@app.get("/api/transactions/{transaction_id}/disputes", response_model=List[DisputeResponse], tags=["Transactions"])
def get_transaction_disputes(transaction_id: str, db: Session = Depends(get_db)):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return [_serialize_dispute(d) for d in txn.disputes]


# ---------------------------------------------------------------------------
# Disputes (Secondary workflow)
# ---------------------------------------------------------------------------

@app.get("/api/disputes", response_model=PaginatedDisputesResponse, tags=["Disputes"])
def list_disputes(
    status: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    q = db.query(Dispute)
    if status:
        q = q.filter(Dispute.status == status.upper())
    if reason:
        q = q.filter(Dispute.reason.ilike(f"%{reason.strip()}%"))

    total = q.count()
    items = q.order_by(Dispute.created_at.desc()).offset(skip).limit(limit).all()

    return PaginatedDisputesResponse(
        items=[_serialize_dispute(d) for d in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@app.get("/api/disputes/{dispute_id}", response_model=DisputeResponse, tags=["Disputes"])
def get_dispute(dispute_id: str, db: Session = Depends(get_db)):
    d = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not d:
        d = db.query(Dispute).filter(Dispute.paypal_dispute_id == dispute_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Dispute not found")
    return _serialize_dispute(d)


@app.post("/api/disputes", response_model=DisputeResponse, status_code=201, tags=["Disputes"])
def create_dispute(payload: DisputeCreate, db: Session = Depends(get_db)):
    txn = _find_transaction(db, payload.transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    now = datetime.utcnow()
    d = Dispute(
        id=str(uuid.uuid4()),
        transaction_id=txn.id,
        paypal_dispute_id=payload.paypal_dispute_id,
        reason=payload.reason,
        status=payload.status.upper(),
        evidence_submitted=payload.evidence_submitted,
        resolution=payload.resolution,
        created_at=now,
        updated_at=now,
    )
    db.add(d)
    txn.status = "DISPUTED"
    txn.updated_at = now
    db.add(
        TransactionEvent(
            id=str(uuid.uuid4()),
            transaction_id=txn.id,
            event_type="DISPUTE_OPENED",
            title="Dispute recorded",
            description=f"Dispute opened for reason {payload.reason}.",
            source="database",
            details={"paypal_dispute_id": payload.paypal_dispute_id},
            occurred_at=now,
        )
    )
    db.commit()
    db.refresh(d)
    return _serialize_dispute(d)


@app.put("/api/disputes/{dispute_id}/status", response_model=DisputeResponse, tags=["Disputes"])
def update_dispute_status(
    dispute_id: str,
    status: str = Body(..., embed=True),
    resolution: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
):
    d = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Dispute not found")

    valid = {"OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED"}
    if status.upper() not in valid:
        raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of {valid}")

    d.status = status.upper()
    if resolution:
        d.resolution = resolution
    d.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(d)
    return _serialize_dispute(d)


@app.post("/api/disputes/{dispute_id}/evidence", tags=["Disputes"])
async def submit_evidence(
    dispute_id: str,
    evidence: dict = Body(...),
    db: Session = Depends(get_db),
):
    d = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Dispute not found")

    paypal_dispute_id = d.paypal_dispute_id or dispute_id
    try:
        result = await paypal_service.submit_evidence(paypal_dispute_id, evidence)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    now = datetime.utcnow()
    d.evidence_submitted = True
    d.status = "UNDER_REVIEW"
    d.updated_at = now
    if d.transaction_id:
        db.add(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=d.transaction_id,
                event_type="EVIDENCE_SUBMITTED",
                title="Dispute evidence submitted",
                description=f"Evidence submitted for dispute {paypal_dispute_id} (mode: {result.get('mode')}).",
                source="mock" if result.get("is_simulated") else "paypal_api",
                details={"dispute_id": d.id},
                occurred_at=now,
            )
        )
    db.commit()
    db.refresh(d)
    return {"dispute": _serialize_dispute(d), "paypal_response": result}


@app.post("/api/disputes/{dispute_id}/escalate", tags=["Disputes"])
async def escalate_dispute(
    dispute_id: str,
    note: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    d = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Dispute not found")

    paypal_dispute_id = d.paypal_dispute_id or dispute_id
    try:
        result = await paypal_service.escalate_dispute(paypal_dispute_id, note)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    now = datetime.utcnow()
    d.status = "UNDER_REVIEW"
    d.updated_at = now
    if d.transaction_id:
        db.add(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=d.transaction_id,
                event_type="DISPUTE_ESCALATED",
                title="Dispute escalated to PayPal",
                description=f"Escalated {paypal_dispute_id}: {note}",
                source="mock" if result.get("is_simulated") else "paypal_api",
                details={"dispute_id": d.id},
                occurred_at=now,
            )
        )
    db.commit()
    return {"message": "Dispute escalated successfully", "paypal_response": result}


@app.post("/api/disputes/{dispute_id}/accept", tags=["Disputes"])
async def accept_dispute_claim(dispute_id: str, db: Session = Depends(get_db)):
    d = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Dispute not found")

    paypal_dispute_id = d.paypal_dispute_id or dispute_id
    try:
        result = await paypal_service.accept_claim(paypal_dispute_id)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    now = datetime.utcnow()
    d.status = "RESOLVED"
    d.resolution = "Claim accepted by merchant"
    d.updated_at = now
    if d.transaction:
        d.transaction.status = "REFUNDED"
        d.transaction.updated_at = now
        db.add(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=d.transaction.id,
                event_type="REFUND_ISSUED",
                title="Refund via dispute claim acceptance",
                description=f"Accepted claim for {paypal_dispute_id} (mode: {result.get('mode')}).",
                source="mock" if result.get("is_simulated") else "paypal_api",
                details={"dispute_id": d.id},
                occurred_at=now,
            )
        )
    db.commit()
    return {"message": "Claim accepted", "paypal_response": result}


# ---------------------------------------------------------------------------
# Resolution Cases (Secondary workflow)
# ---------------------------------------------------------------------------

@app.get("/api/cases", response_model=PaginatedCasesResponse, tags=["Cases"])
def list_cases(
    priority: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    q = db.query(ResolutionCase)
    if priority:
        q = q.filter(ResolutionCase.priority == priority.upper())
    if outcome:
        if outcome.upper() == "OPEN":
            q = q.filter(or_(ResolutionCase.outcome.is_(None), ResolutionCase.outcome == ""))
        else:
            q = q.filter(ResolutionCase.outcome.ilike(f"%{outcome.strip()}%"))

    total = q.count()
    items = q.order_by(ResolutionCase.created_at.desc()).offset(skip).limit(limit).all()

    return PaginatedCasesResponse(
        items=[_serialize_case(c) for c in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@app.get("/api/cases/{case_id}", response_model=ResolutionCaseResponse, tags=["Cases"])
def get_case(case_id: str, db: Session = Depends(get_db)):
    c = db.query(ResolutionCase).filter(ResolutionCase.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Resolution case not found")
    return _serialize_case(c)


@app.post("/api/cases", response_model=ResolutionCaseResponse, status_code=201, tags=["Cases"])
def create_case(payload: ResolutionCaseCreate, db: Session = Depends(get_db)):
    dispute = db.query(Dispute).filter(Dispute.id == payload.dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    priority = payload.priority
    if dispute.transaction:
        computed = status_engine.compute_dispute_priority(dispute, dispute.transaction)
        if priority == "MEDIUM":
            priority = computed.value

    c = ResolutionCase(
        id=str(uuid.uuid4()),
        dispute_id=payload.dispute_id,
        assigned_agent=payload.assigned_agent,
        priority=priority.upper(),
        ai_recommendation=payload.ai_recommendation,
        steps_taken=payload.steps_taken or [],
        outcome=payload.outcome,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return _serialize_case(c)


@app.put("/api/cases/{case_id}", response_model=ResolutionCaseResponse, tags=["Cases"])
def update_case(case_id: str, payload: ResolutionCaseUpdate, db: Session = Depends(get_db)):
    c = db.query(ResolutionCase).filter(ResolutionCase.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Resolution case not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(c, field, value)

    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _serialize_case(c)


@app.get("/api/cases/{case_id}/summary", tags=["Cases"])
async def get_case_summary(case_id: str, db: Session = Depends(get_db)):
    c = db.query(ResolutionCase).filter(ResolutionCase.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Resolution case not found")

    dispute = c.dispute
    if not dispute:
        raise HTTPException(status_code=404, detail="Associated dispute not found")

    transaction = dispute.transaction
    txn_dict = _txn_to_dict(transaction) if transaction else {}
    dispute_dict = _dispute_to_dict(dispute)

    is_breached, sla_info = status_engine.check_sla_breach(c)
    next_actions = status_engine.get_next_actions(dispute.status, c)

    summary = await ai_service.summarize_case(
        case_id=case_id,
        transaction=txn_dict,
        dispute=dispute_dict,
        resolution_steps=c.steps_taken or [],
    )

    return {
        "case_id": case_id,
        "summary_markdown": summary,
        "sla_breached": is_breached,
        "sla_info": sla_info,
        "next_actions": next_actions,
        "priority": c.priority,
    }


# ---------------------------------------------------------------------------
# AI Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/ai/analyze", response_model=AIRecommendationResponse, tags=["AI"])
async def analyze_dispute(payload: AIRecommendationRequest, db: Session = Depends(get_db)):
    dispute = db.query(Dispute).filter(Dispute.id == payload.dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    transaction = dispute.transaction
    dispute_dict = {
        "id": dispute.id,
        "reason": dispute.reason,
        "status": dispute.status,
        "evidence_submitted": dispute.evidence_submitted,
        "paypal_dispute_id": dispute.paypal_dispute_id,
        "additional_context": payload.additional_context,
    }
    txn_dict = _txn_to_dict(transaction) if transaction else {}

    recommendation = await ai_service.analyze_dispute(dispute_dict, txn_dict)

    cases = db.query(ResolutionCase).filter(ResolutionCase.dispute_id == dispute.id).all()
    for case in cases:
        case.ai_recommendation = (
            f"[{recommendation.recommended_action}] "
            f"Confidence: {recommendation.confidence:.0%} | "
            f"Risk: {recommendation.risk_score}/100\n\n"
            f"{recommendation.reasoning}"
        )
        case.updated_at = datetime.utcnow()
    db.commit()

    return recommendation


@app.post("/api/ai/template", tags=["AI"])
async def generate_response_template(
    dispute_reason: str = Body(..., embed=True),
    merchant_perspective: str = Body(..., embed=True),
):
    template = await ai_service.generate_response_template(dispute_reason, merchant_perspective)
    return {"template": template}


@app.post("/api/ai/risk", tags=["AI"])
async def classify_transaction_risk(
    transaction_id: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    txn = _find_transaction(db, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return await ai_service.classify_risk(_txn_to_dict(txn))


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@app.post("/api/webhooks/paypal", tags=["Webhooks"])
async def receive_paypal_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    webhook_logger.info("webhook_received bytes=%d", len(raw_body))

    # Verify signature against exact raw request bytes BEFORE trusting payload
    is_allowed, verification_status = await webhook_service.verify_webhook_signature(
        headers=dict(request.headers),
        raw_body=raw_body,
        webhook_id=PAYPAL_WEBHOOK_ID,
        paypal_service=paypal_service,
        verify_webhooks=PAYPAL_VERIFY_WEBHOOKS,
    )
    if not is_allowed:
        raise HTTPException(
            status_code=401,
            detail=f"Webhook signature verification failed ({verification_status})",
        )

    # Parse a separate copy for application processing (raw_body remains untouched)
    try:
        payload = _json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")

    event_type = payload.get("event_type")
    try:
        result = await webhook_service.process_event(
            event_type=event_type,
            payload=payload,
            db=db,
            verification_status=verification_status,
        )
    except MalformedWebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except WebhookProcessingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return {
        "received": True,
        "verification_status": verification_status,
        "result": result,
    }


# ---------------------------------------------------------------------------
# PayPal API proxy & Sandbox integration endpoints
# ---------------------------------------------------------------------------

def _map_paypal_status(pp_status: str) -> str:
    s = (pp_status or "").upper()
    if s in {"COMPLETED", "PARTIALLY_REFUNDED"}:
        return "COMPLETED"
    if s == "REFUNDED":
        return "REFUNDED"
    if s in {"DECLINED", "FAILED"}:
        return "FAILED"
    return "PENDING"


def _upsert_paypal_capture_in_db(
    db: Session,
    capture: dict,
    order_payload: Optional[dict] = None,
    description: Optional[str] = None,
    source: str = "paypal_api",
) -> Transaction:
    capture_id = capture.get("id")
    if not capture_id:
        raise HTTPException(status_code=502, detail="PayPal response did not include a capture ID")

    amount_obj = capture.get("amount", {})
    amount = float(amount_obj.get("value", 0) or 0)
    currency = (amount_obj.get("currency_code") or "USD").upper()
    raw_status = capture.get("status", "COMPLETED")
    mapped_status = _map_paypal_status(raw_status)
    payer_email = (order_payload or {}).get("payer", {}).get("email_address")

    now = datetime.utcnow()
    txn = db.query(Transaction).filter(Transaction.paypal_transaction_id == capture_id).first()
    is_new = txn is None

    resolved_order_id = (
        (order_payload or {}).get("id")
        or ((capture.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
        or ((txn.metadata_ or {}).get("paypal_order_id") if txn else None)
        or ((txn.metadata_ or {}).get("order_id") if txn else None)
    )

    metadata_payload = {
        "paypal_raw_status": raw_status,
        "paypal_capture": capture,
        "is_simulated": bool((order_payload or capture).get("is_simulated")),
    }
    if resolved_order_id:
        metadata_payload["paypal_order_id"] = resolved_order_id
    raw_upper = (raw_status or "").upper()
    if raw_upper == "PARTIALLY_REFUNDED":
        metadata_payload["partial_refund"] = True
    elif raw_upper == "REFUNDED":
        metadata_payload["partial_refund"] = False

    if is_new:
        txn = Transaction(
            id=str(uuid.uuid4()),
            paypal_transaction_id=capture_id,
            merchant_id="PAYPAL-SANDBOX" if not paypal_service._mock_mode else "MOCK-MERCHANT",
            amount=amount,
            currency=currency,
            status=mapped_status,
            description=description or "PayPal Sandbox capture",
            buyer_email=payer_email,
            metadata_=metadata_payload,
            created_at=now,
            updated_at=now,
        )
        db.add(txn)
        db.flush()
        db.add(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=txn.id,
                event_type="PAYMENT_CAPTURED" if mapped_status == "COMPLETED" else f"PAYMENT_{mapped_status}",
                title=f"PayPal capture stored ({raw_status})",
                description=f"Captured {amount:.2f} {currency} via PayPal ({capture.get('mode', paypal_service.environment_label)}).",
                source=source,
                details={"capture_id": capture_id, "order_id": resolved_order_id},
                occurred_at=now,
            )
        )
    else:
        txn.status = mapped_status
        txn.amount = amount
        txn.currency = currency
        if payer_email:
            txn.buyer_email = payer_email
        existing_meta = dict(txn.metadata_ or {})
        existing_meta.update(metadata_payload)
        txn.metadata_ = existing_meta
        txn.updated_at = now
        db.add(
            TransactionEvent(
                id=str(uuid.uuid4()),
                transaction_id=txn.id,
                event_type="PAYPAL_SYNCED",
                title=f"Synced from PayPal API ({raw_status})",
                description=f"Updated record from PayPal capture {capture_id}.",
                source=source,
                details={"capture_id": capture_id},
                occurred_at=now,
            )
        )

    db.commit()
    db.refresh(txn)
    return txn


@app.post("/api/paypal/orders", tags=["PayPal"])
async def create_paypal_order(
    amount: float = Body(19.99, embed=True),
    currency: str = Body("USD", embed=True),
    description: str = Body("PayPal Sandbox Test Order", embed=True),
):
    try:
        res = await paypal_service.create_order(
            amount=amount,
            currency=currency,
            description=description,
        )
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail={"message": e.message, "details": e.details})

    approve_url = None
    for link in res.get("links", []):
        if link.get("rel") in {"approve", "payer-action"}:
            approve_url = link.get("href")
            break

    return {
        "order_id": res.get("id"),
        "status": res.get("status"),
        "approve_url": approve_url,
        "mode": res.get("mode"),
        "is_simulated": res.get("is_simulated"),
        "paypal_response": res,
    }


@app.post("/api/paypal/orders/{order_id}/capture", tags=["PayPal"])
async def capture_paypal_order(order_id: str, db: Session = Depends(get_db)):
    try:
        res = await paypal_service.capture_order(order_id)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail={"message": e.message, "details": e.details})

    purchase_units = res.get("purchase_units") or []
    captures = (purchase_units[0].get("payments") or {}).get("captures") or [] if purchase_units else []
    if not captures:
        raise HTTPException(status_code=502, detail="No capture found in PayPal order capture response")

    capture = dict(captures[0])
    capture["mode"] = res.get("mode")
    capture["is_simulated"] = res.get("is_simulated")
    desc = purchase_units[0].get("description") if purchase_units else None
    source = "mock" if res.get("is_simulated") else "paypal_api"

    txn = _upsert_paypal_capture_in_db(db, capture=capture, order_payload=res, description=desc, source=source)
    all_txns = db.query(Transaction).all()
    review_map = _load_review_map(db)
    return {
        "transaction": _serialize_transaction(txn, all_txns, review_map),
        "paypal_response": res,
    }


@app.post("/api/paypal/sync/{capture_id}", tags=["PayPal"])
async def sync_paypal_capture(capture_id: str, db: Session = Depends(get_db)):
    try:
        capture = await paypal_service.get_transaction(capture_id)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail={"message": e.message, "details": e.details})

    source = "mock" if capture.get("is_simulated") else "paypal_api"
    txn = _upsert_paypal_capture_in_db(db, capture=capture, source=source)
    all_txns = db.query(Transaction).all()
    review_map = _load_review_map(db)
    return {
        "transaction": _serialize_transaction(txn, all_txns, review_map),
        "paypal_response": capture,
    }


@app.get("/api/paypal/transactions/{transaction_id}", tags=["PayPal"])
async def proxy_paypal_transaction(transaction_id: str):
    try:
        return await paypal_service.get_transaction(transaction_id)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail={"message": e.message, "details": e.details})


@app.get("/api/paypal/disputes/{dispute_id}", tags=["PayPal"])
async def proxy_paypal_dispute(dispute_id: str):
    try:
        return await paypal_service.get_dispute(dispute_id)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@app.get("/api/paypal/disputes", tags=["PayPal"])
async def proxy_paypal_disputes(
    status: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
):
    try:
        return await paypal_service.list_disputes(status=status, start_date=start_date)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
