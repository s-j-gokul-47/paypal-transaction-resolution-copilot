"""
models.py - SQLAlchemy ORM models and Pydantic schemas for PayPal Transaction Resolution Copilot.
"""
import uuid
from datetime import datetime
from typing import Optional, Any, List

from sqlalchemy import (
    Column, String, Float, Boolean, Text, JSON, DateTime,
    ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import relationship
from pydantic import BaseModel, Field, ConfigDict

from database import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=_new_uuid)
    paypal_transaction_id = Column(String, unique=True, index=True, nullable=False)
    merchant_id = Column(String, index=True, nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(8), nullable=False, default="USD")
    status = Column(
        SAEnum("COMPLETED", "PENDING", "FAILED", "DISPUTED", "REFUNDED", name="transaction_status"),
        nullable=False,
        default="PENDING",
    )
    description = Column(Text, nullable=True)
    buyer_email = Column(String, nullable=True)
    seller_email = Column(String, nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True, default=dict)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    # Relationships
    disputes = relationship("Dispute", back_populates="transaction", cascade="all, delete-orphan")
    events = relationship(
        "TransactionEvent",
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="TransactionEvent.occurred_at.asc()",
    )


class TransactionEvent(Base):
    __tablename__ = "transaction_events"

    id = Column(String, primary_key=True, default=_new_uuid)
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    source = Column(String, nullable=False, default="database")
    details = Column(JSON, nullable=True, default=dict)
    occurred_at = Column(DateTime, nullable=False, default=_utcnow)

    # Relationships
    transaction = relationship("Transaction", back_populates="events")


class Dispute(Base):
    __tablename__ = "disputes"

    id = Column(String, primary_key=True, default=_new_uuid)
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True)
    paypal_dispute_id = Column(String, unique=True, index=True, nullable=True)
    reason = Column(String, nullable=False)
    status = Column(
        SAEnum("OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", name="dispute_status"),
        nullable=False,
        default="OPEN",
    )
    evidence_submitted = Column(Boolean, nullable=False, default=False)
    resolution = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    # Relationships
    transaction = relationship("Transaction", back_populates="disputes")
    resolution_cases = relationship("ResolutionCase", back_populates="dispute", cascade="all, delete-orphan")


class ResolutionCase(Base):
    __tablename__ = "resolution_cases"

    id = Column(String, primary_key=True, default=_new_uuid)
    dispute_id = Column(String, ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False, index=True)
    assigned_agent = Column(String, nullable=True)
    priority = Column(
        SAEnum("LOW", "MEDIUM", "HIGH", "CRITICAL", name="case_priority"),
        nullable=False,
        default="MEDIUM",
    )
    ai_recommendation = Column(Text, nullable=True)
    steps_taken = Column(JSON, nullable=False, default=list)
    outcome = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    # Relationships
    dispute = relationship("Dispute", back_populates="resolution_cases")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(String, primary_key=True)
    event_type = Column(String, nullable=False, index=True)
    payload = Column(JSON, nullable=False)
    verification_status = Column(String, nullable=False, default="unverified")
    processing_status = Column(String, nullable=False, default="RECEIVED")
    error_message = Column(Text, nullable=True)
    processed = Column(Boolean, nullable=False, default=False)
    received_at = Column(DateTime, nullable=False, default=_utcnow)
    processed_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class TransactionCreate(BaseModel):
    paypal_transaction_id: str
    merchant_id: str
    amount: float
    currency: str = "USD"
    status: str = "PENDING"
    description: Optional[str] = None
    buyer_email: Optional[str] = None
    seller_email: Optional[str] = None
    metadata_: Optional[dict] = Field(default_factory=dict, alias="metadata")

    model_config = ConfigDict(populate_by_name=True)


class TransactionEventResponse(BaseModel):
    id: str
    transaction_id: str
    event_type: str
    title: str
    description: Optional[str]
    source: str
    details: Optional[Any] = None
    occurred_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TransactionResponse(BaseModel):
    id: str
    paypal_transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    status: str
    description: Optional[str]
    buyer_email: Optional[str]
    seller_email: Optional[str]
    metadata_: Optional[Any] = Field(None, alias="metadata")
    created_at: datetime
    updated_at: datetime
    possible_duplicate_count: int = 0
    dispute_count: int = 0

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PaginatedTransactionsResponse(BaseModel):
    items: List[TransactionResponse]
    total: int
    skip: int
    limit: int


class TransactionStateExplanation(BaseModel):
    transaction_id: str
    paypal_transaction_id: str
    status: str
    headline: str
    plain_english_summary: str
    likely_causes: List[str]
    recommended_actions: List[str]
    verified_fields_used: List[str]
    source_evidence: dict = Field(default_factory=dict)
    mode: str
    data_source: str = "database"


class DuplicateCandidate(BaseModel):
    transaction_id: str
    paypal_transaction_id: str
    status: str
    amount: float
    currency: str
    created_at: datetime
    time_diff_seconds: int
    classification: str
    confidence: str
    matching_fields: List[str]
    explanation: str
    recommended_action: str
    order_id: Optional[str] = None
    description: Optional[str] = None
    order_relationship: Optional[str] = None


class DuplicateAnalysisResponse(BaseModel):
    transaction_id: str
    has_possible_duplicates: bool
    candidates: List[DuplicateCandidate]


class SupportSummaryRequest(BaseModel):
    audience: str = Field(default="internal", pattern="^(buyer|internal|escalation)$")
    agent_notes: Optional[str] = None


class SupportSummaryResponse(BaseModel):
    transaction_id: str
    paypal_transaction_id: str
    audience: str
    summary: str
    verified_facts: dict
    mode: str
    generated_at: datetime


class RefundRequest(BaseModel):
    amount: Optional[float] = None
    reason: Optional[str] = "Refund requested via Transaction Resolution Copilot"


class DisputeCreate(BaseModel):
    transaction_id: str
    paypal_dispute_id: Optional[str] = None
    reason: str
    status: str = "OPEN"
    evidence_submitted: bool = False
    resolution: Optional[str] = None


class DisputeResponse(BaseModel):
    id: str
    transaction_id: str
    paypal_dispute_id: Optional[str]
    reason: str
    status: str
    evidence_submitted: bool
    resolution: Optional[str]
    created_at: datetime
    updated_at: datetime
    priority: Optional[str] = None
    resolution_case_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PaginatedDisputesResponse(BaseModel):
    items: List[DisputeResponse]
    total: int
    skip: int
    limit: int


class ResolutionCaseCreate(BaseModel):
    dispute_id: str
    assigned_agent: Optional[str] = None
    priority: str = "MEDIUM"
    ai_recommendation: Optional[str] = None
    steps_taken: Optional[List[Any]] = Field(default_factory=list)
    outcome: Optional[str] = None


class ResolutionCaseUpdate(BaseModel):
    assigned_agent: Optional[str] = None
    priority: Optional[str] = None
    ai_recommendation: Optional[str] = None
    steps_taken: Optional[List[Any]] = None
    outcome: Optional[str] = None


class ResolutionCaseResponse(BaseModel):
    id: str
    dispute_id: str
    assigned_agent: Optional[str]
    priority: str
    ai_recommendation: Optional[str]
    steps_taken: List[Any]
    outcome: Optional[str]
    created_at: datetime
    updated_at: datetime
    sla_breached: bool = False
    sla_info: Optional[str] = None
    sla_due_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class PaginatedCasesResponse(BaseModel):
    items: List[ResolutionCaseResponse]
    total: int
    skip: int
    limit: int


class AIRecommendationRequest(BaseModel):
    dispute_id: str
    transaction_id: Optional[str] = None
    additional_context: Optional[str] = None


class AIRecommendationResponse(BaseModel):
    recommended_action: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str
    suggested_steps: List[str]
    risk_score: int = Field(..., ge=0, le=100)
    estimated_resolution_time: str
    mode: str = "heuristic"
