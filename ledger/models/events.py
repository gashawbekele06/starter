"""
ledger/models/events.py - Complete event schema for The Ledger
"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import UUID
from pydantic import BaseModel, Field


# ─── Base Models ──────────────────────────────────────────────────────────────

class BaseEvent(BaseModel):
    """Base class for all domain events."""
    event_type: str = Field(default_factory=lambda: "UnnamedEvent")
    event_version: int = Field(default=1)
    
    class Config:
        use_enum_values = True


class StoredEvent(BaseModel):
    """Event as stored in database."""
    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict
    metadata: dict
    recorded_at: datetime


class StreamMetadata(BaseModel):
    """Aggregate-level stream metadata."""
    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)


# ─── Enumerations ────────────────────────────────────────────────────────────

class RiskTier(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApplicationState(str, Enum):
    SUBMITTED = "SUBMITTED"
    CREDIT_ANALYSIS_REQUESTED = "CREDIT_ANALYSIS_REQUESTED"
    CREDIT_ANALYSIS_COMPLETE = "CREDIT_ANALYSIS_COMPLETE"
    FRAUD_CHECK_REQUESTED = "FRAUD_CHECK_REQUESTED"
    FRAUD_CHECK_COMPLETE = "FRAUD_CHECK_COMPLETE"
    COMPLIANCE_CHECK_REQUESTED = "COMPLIANCE_CHECK_REQUESTED"
    COMPLIANCE_CHECK_COMPLETE = "COMPLIANCE_CHECK_COMPLETE"
    DECISION_PENDING = "DECISION_PENDING"
    DECISION_GENERATED = "DECISION_GENERATED"
    HUMAN_REVIEW_PENDING = "HUMAN_REVIEW_PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    REFERRED = "REFERRED"


class DecisionRecommendation(str, Enum):
    APPROVE = "APPROVE"
    DECLINE = "DECLINE"
    REFER = "REFER"


class ComplianceCheckStatus(str, Enum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    CONDITIONAL = "CONDITIONAL"


# ─── Domain Events ───────────────────────────────────────────────────────────

class ApplicationSubmitted(BaseEvent):
    """LoanApplication stream event #1."""
    application_id: str
    applicant_id: str
    requested_amount_usd: Decimal
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime


class CreditAnalysisRequested(BaseEvent):
    """LoanApplication stream event."""
    application_id: str
    assigned_agent_id: str
    requested_at: datetime


class CreditAnalysisCompleted(BaseEvent):
    """AgentSession stream event (v1: minimal, v2: with model_version)."""
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float
    risk_tier: RiskTier
    recommended_limit_usd: Decimal
    analysis_duration_ms: int
    input_data_hash: str
    event_version: int = 1


class FraudScreeningCompleted(BaseEvent):
    """AgentSession stream event."""
    application_id: str
    agent_id: str
    fraud_score: float
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str


class ComplianceCheckRequested(BaseEvent):
    """ComplianceRecord stream event."""
    application_id: str
    regulation_set_version: str
    checks_required: list[str]


class ComplianceRulePassed(BaseEvent):
    """ComplianceRecord stream event."""
    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: datetime
    evidence_hash: str


class ComplianceRuleFailed(BaseEvent):
    """ComplianceRecord stream event."""
    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool


class DecisionGenerated(BaseEvent):
    """LoanApplication stream event (v1: simple, v2: with model_versions dict)."""
    application_id: str
    orchestrator_agent_id: str
    recommendation: DecisionRecommendation
    confidence_score: float
    contributing_agent_sessions: list[str] = Field(default_factory=list)
    decision_basis_summary: str
    model_versions: dict = Field(default_factory=dict)
    event_version: int = 1


class HumanReviewCompleted(BaseEvent):
    """LoanApplication stream event."""
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: DecisionRecommendation
    override_reason: Optional[str] = None


class ApplicationApproved(BaseEvent):
    """LoanApplication stream event. Terminal state."""
    application_id: str
    approved_amount_usd: Decimal
    interest_rate: float
    conditions: list[str] = Field(default_factory=list)
    approved_by: str
    effective_date: datetime


class ApplicationDeclined(BaseEvent):
    """LoanApplication stream event. Terminal state."""
    application_id: str
    decline_reasons: list[str]
    declined_by: str
    adverse_action_notice_required: bool


class AgentContextLoaded(BaseEvent):
    """AgentSession stream event. Must be first event."""
    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int
    context_token_count: int
    model_version: str


class AuditIntegrityCheckRun(BaseEvent):
    """AuditLedger stream event."""
    entity_id: str
    check_timestamp: datetime
    events_verified_count: int
    integrity_hash: str
    previous_hash: Optional[str] = None
