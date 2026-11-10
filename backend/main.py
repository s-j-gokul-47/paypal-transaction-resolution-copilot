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
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import or_
from sqlalchemy.orm import Session

load_dotenv()

from database import get_db, init_db, SessionLocal
from models import (
    Transaction, TransactionEvent, Dispute, ResolutionCase, WebhookEvent,
    TransactionCreate, TransactionResponse, PaginatedTransactionsResponse,
    TransactionEventResponse, TransactionStateExplanation,
    DuplicateAnalysisResponse, SupportSummaryRequest, SupportSummaryResponse,
    RefundRequest,
    DisputeCreate, DisputeResponse, PaginatedDisputesResponse,
    ResolutionCaseCreate, ResolutionCaseUpdate, ResolutionCaseResponse, PaginatedCasesResponse,
    AIRecommendationRequest, AIRecommendationResponse,
)
from paypal_service import PayPalService, PayPalAPIError
from ai_service import AIService
from status_engine import StatusEngine
from webhook_service import WebhookService


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


def _serialize_transaction(txn: Transaction, all_txns: List[Transaction]) -> TransactionResponse:
    dups = status_engine.detect_duplicate_transactions(txn, all_txns)
    active_dups = [d for d in dups if d["classification"] == "POSSIBLE_DUPLICATE"]
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
        possible_duplicate_count=len(active_dups),
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

    # Count unique duplicate pairs
    seen_pairs: set[tuple[str, str]] = set()
    duplicate_alerts: list[dict] = []
    for t in all_txns:
        dups = status_engine.detect_duplicate_transactions(t, all_txns)
        for dup in dups:
            if dup["classification"] == "POSSIBLE_DUPLICATE":
                pair = tuple(sorted([t.id, dup["transaction_id"]]))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    duplicate_alerts.append({
                        "primary_id": t.id,
                        "primary_paypal_id": t.paypal_transaction_id,
                        "duplicate_id": dup["transaction_id"],
                        "duplicate_paypal_id": dup["paypal_transaction_id"],
                        "amount": t.amount,
                        "currency": t.currency,
                        "buyer_email": t.buyer_email,
                        "confidence": dup["confidence"],
                        "time_diff_seconds": dup["time_diff_seconds"],
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

    return PaginatedTransactionsResponse(
        items=[_serialize_transaction(t, all_txns) for t in items],
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
    return _serialize_transaction(txn, all_txns)


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
    return _serialize_transaction(txn, all_txns)


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
    candidates = status_engine.detect_duplicate_transactions(txn, all_txns)
    has_possible = any(c["classification"] == "POSSIBLE_DUPLICATE" for c in candidates)
    return DuplicateAnalysisResponse(
        transaction_id=txn.id,
        has_possible_duplicates=has_possible,
        candidates=candidates,
    )


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
    txn_serialized = _serialize_transaction(txn, all_txns)
    timeline = [_event_to_dict(ev) for ev in txn.events]
    duplicates = status_engine.detect_duplicate_transactions(txn, all_txns)
    disputes = [_dispute_to_dict(d) for d in txn.disputes]
    explanation = await ai_service.explain_transaction(
        _txn_to_dict(txn), timeline, duplicates, disputes
    )

    return {
        "transaction": txn_serialized,
        "timeline": timeline,
        "duplicates": {
            "transaction_id": txn.id,
            "has_possible_duplicates": any(
                c["classification"] == "POSSIBLE_DUPLICATE" for c in duplicates
            ),
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

    refund_amount = payload.amount if payload.amount is not None else txn.amount
    if refund_amount <= 0 or refund_amount > txn.amount:
        raise HTTPException(status_code=422, detail="Invalid refund amount")

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
    txn.status = "REFUNDED"
    txn.updated_at = now
    meta = dict(txn.metadata_ or {})
    meta["refund_id"] = paypal_res.get("id")
    meta["refund_simulated"] = bool(paypal_res.get("is_simulated"))
    txn.metadata_ = meta

    db.add(
        TransactionEvent(
            id=str(uuid.uuid4()),
            transaction_id=txn.id,
            event_type="REFUND_ISSUED",
            title="Refund issued" + (" (Simulated)" if paypal_res.get("is_simulated") else ""),
            description=(
                f"Refund of {refund_amount:.2f} {txn.currency} recorded "
                f"(ID: {paypal_res.get('id')}, mode: {paypal_res.get('mode')})."
            ),
            source="mock" if paypal_res.get("is_simulated") else "paypal_api",
            details={"paypal_response": paypal_res, "reason": payload.reason},
            occurred_at=now,
        )
    )
    db.commit()
    db.refresh(txn)
    all_txns = db.query(Transaction).all()

    return {
        "transaction": _serialize_transaction(txn, all_txns),
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
    body = await request.body()
    try:
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    is_allowed, verification_status = await webhook_service.verify_webhook_signature(
        headers=dict(request.headers),
        webhook_event=payload,
        webhook_id=PAYPAL_WEBHOOK_ID,
        paypal_service=paypal_service,
        verify_webhooks=PAYPAL_VERIFY_WEBHOOKS,
    )
    if not is_allowed:
        raise HTTPException(
            status_code=401,
            detail=f"Webhook signature verification failed ({verification_status})",
        )

    event_type = payload.get("event_type", "UNKNOWN")
    result = await webhook_service.process_event(
        event_type=event_type,
        payload=payload,
        db=db,
        verification_status=verification_status,
    )
    return {
        "received": True,
        "verification_status": verification_status,
        "result": result,
    }


# ---------------------------------------------------------------------------
# PayPal API proxy endpoints
# ---------------------------------------------------------------------------

@app.get("/api/paypal/transactions/{transaction_id}", tags=["PayPal"])
async def proxy_paypal_transaction(transaction_id: str):
    try:
        return await paypal_service.get_transaction(transaction_id)
    except PayPalAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


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
