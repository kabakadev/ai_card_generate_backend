# models/billing/payment_transaction.py
from __future__ import annotations

from datetime import datetime
from typing import Optional, Mapping, Any

from sqlalchemy import Enum as SAEnum
from config import db

def _utcnow() -> datetime:
    return datetime.utcnow()

def _normalize_intasend_state(raw: Optional[str]) -> str:
    s = (raw or "").strip().upper()
    if s in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    if s in {"FAILED", "CANCELLED", "CANCELED", "DECLINED", "EXPIRED"}:
        return "failed"
    return "pending"

class PaymentTransaction(db.Model):
    __tablename__ = "payment_transactions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    amount = db.Column(db.Integer, nullable=False)  # KES integer
    currency = db.Column(db.String(3), nullable=False, server_default="KES")

    payment_method = db.Column(
        SAEnum("mpesa", "card", "bank", name="payment_methods_v1"),
        nullable=False,
        server_default="mpesa",
    )
    provider = db.Column(db.String(32), nullable=False, server_default="intasend")

    api_ref = db.Column(db.String(120), nullable=True, unique=True, index=True)   # checkout_id / api_ref
    provider_ref = db.Column(db.String(120), nullable=True, index=True)           # invoice_id or mpesa receipt
    plan_type = db.Column(db.String(20), nullable=True)  # 'monthly'

    status = db.Column(
        SAEnum("initiated", "pending", "succeeded", "failed", "refunded", name="transaction_status_v1"),
        nullable=False,
        server_default="initiated",
    )
    provider_status = db.Column(db.String(32), nullable=True)
    failure_reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), onupdate=db.func.now())
    completed_at = db.Column(db.DateTime, nullable=True)

    # NOTE: no child-side relationship; parent creates `tx.user` via backref.

    # ------------ helpers (idempotent, commit inside) ----------------
    def set_refs(self, *, checkout_id: Optional[str] = None, invoice_id: Optional[str] = None) -> None:
        changed = False
        if checkout_id and not self.api_ref:
            self.api_ref = checkout_id
            changed = True
        if invoice_id and not self.provider_ref:
            self.provider_ref = invoice_id
            changed = True
        if changed:
            self.updated_at = _utcnow()
            db.session.add(self)
            db.session.commit()

    def mark_pending(self, *, provider_status: Optional[str] = None) -> None:
        self.status = "pending"
        if provider_status:
            self.provider_status = provider_status
        self.updated_at = _utcnow()
        db.session.add(self)
        db.session.commit()

    def mark_succeeded(self, provider_ref: Optional[str] = None, *, provider_status: Optional[str] = None) -> None:
        if self.status == "succeeded":
            return
        self.status = "succeeded"
        self.completed_at = _utcnow()
        if provider_ref:
            self.provider_ref = provider_ref
        if provider_status:
            self.provider_status = provider_status
        self.updated_at = self.completed_at
        db.session.add(self)
        db.session.commit()

    def mark_failed(self, reason: Optional[str] = None, *, provider_status: Optional[str] = None) -> None:
        if self.status == "succeeded":
            return
        self.status = "failed"
        self.failure_reason = (reason or "")[:500] if reason else self.failure_reason
        if provider_status:
            self.provider_status = provider_status
        self.updated_at = _utcnow()
        db.session.add(self)
        db.session.commit()

    def apply_intasend_state(self, payload: Mapping[str, Any]) -> str:
        inv = (payload.get("invoice") or {}) if isinstance(payload, dict) else {}
        raw_state = inv.get("state") or payload.get("state") or payload.get("status")
        norm = _normalize_intasend_state(raw_state)

        self.provider_status = raw_state or self.provider_status

        invoice_id = inv.get("invoice_id") or payload.get("invoice_id")
        api_ref = payload.get("api_ref") or payload.get("checkout_id") or payload.get("id")
        if api_ref and not self.api_ref:
            self.api_ref = api_ref
        if invoice_id and not self.provider_ref:
            self.provider_ref = invoice_id

        mpesa_receipt = inv.get("mpesa_receipt") or inv.get("receipt")

        if norm == "succeeded":
            self.status = "succeeded"
            self.completed_at = self.completed_at or _utcnow()
            if mpesa_receipt:
                self.provider_ref = mpesa_receipt
        elif norm == "failed":
            if self.status != "succeeded":
                self.status = "failed"
                self.failure_reason = self.failure_reason or "intasend: FAILED"
        else:
            if self.status not in {"succeeded", "failed"}:
                self.status = "pending"

        self.updated_at = _utcnow()
        db.session.add(self)
        db.session.commit()
        return norm
