# models/billing/payment_transaction.py — INTASEND-ALIGNED (DROP-IN)
from __future__ import annotations

from datetime import datetime
from typing import Optional, Mapping, Any

from sqlalchemy import Enum as SAEnum
from config import db


def _utcnow() -> datetime:
    # store naive-UTC consistently if your DB columns are naive
    return datetime.utcnow()


def _normalize_intasend_state(raw: Optional[str]) -> str:
    """
    Map IntaSend collection 'state' → our status enum.
    Docs: PENDING | PROCESSING | COMPLETE | FAILED
    """
    s = (raw or "").strip().upper()
    if s in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    if s in {"FAILED", "CANCELLED", "CANCELED", "DECLINED", "EXPIRED"}:
        return "failed"
    # default covers PENDING/PROCESSING/unknown
    return "pending"


class PaymentTransaction(db.Model):
    __tablename__ = "payment_transactions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    amount = db.Column(db.Integer, nullable=False)  # KES integer
    currency = db.Column(db.String(3), nullable=False, server_default="KES")

    payment_method = db.Column(
        SAEnum("mpesa", "card", "bank", name="payment_methods_v1"),
        nullable=False,
        server_default="mpesa",
    )
    provider = db.Column(db.String(32), nullable=False, server_default="intasend")

    # Idempotency + references
    # IntaSend events reference your request under api_ref (we store checkout_id here).
    api_ref = db.Column(db.String(120), nullable=True, unique=True, index=True)  # checkout_id / api_ref
    # Generic provider reference (we first store invoice_id; later we might overwrite with mpesa receipt)
    provider_ref = db.Column(db.String(120), nullable=True, index=True)          # invoice_id or mpesa receipt
    plan_type = db.Column(db.String(20), nullable=True)  # 'monthly'

    status = db.Column(
        SAEnum("initiated", "pending", "succeeded", "failed", "refunded", name="transaction_status_v1"),
        nullable=False,
        server_default="initiated",
    )
    # Raw provider status/state string for diagnostics (e.g., 'PENDING','COMPLETE','FAILED')
    provider_status = db.Column(db.String(32), nullable=True)
    failure_reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), onupdate=db.func.now())
    completed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("payment_transactions", lazy="dynamic"))

    # ------------ helpers (idempotent, commit inside) ----------------

    def set_refs(self, *, checkout_id: Optional[str] = None, invoice_id: Optional[str] = None) -> None:
        """
        Save identifiers we learn along the way.
        - api_ref  := checkout_id (aka IntaSend api_ref in events)
        - provider_ref := invoice_id (later may become M-Pesa receipt if known)
        """
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
        """
        Mark the transaction as succeeded. If we have an authoritative receipt,
        store it in provider_ref (overwriting invoice_id is acceptable once paid).
        """
        # idempotency: avoid flipping a final state back and forth
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
            # don't downgrade a confirmed success
            return
        self.status = "failed"
        self.failure_reason = (reason or "")[:500] if reason else self.failure_reason
        if provider_status:
            self.provider_status = provider_status
        self.updated_at = _utcnow()
        db.session.add(self)
        db.session.commit()

    # ------------ provider-aware updater ----------------

    def apply_intasend_state(self, payload: Mapping[str, Any]) -> str:
        """
        Accepts either a status response or webhook payload from IntaSend and
        updates this row accordingly. Returns the normalized status:
        'succeeded' | 'failed' | 'pending'.

        Sources we look at (defensive because shapes vary):
          - payload['invoice']['state'] OR payload['state'] OR payload['status']
          - invoice_id under payload['invoice']['invoice_id'] or payload['invoice_id']
          - checkout/api ref under payload['api_ref'] or payload['checkout_id']
          - M-Pesa receipt under payload['invoice']['mpesa_receipt'] (if provided)

        Docs:
          - Status endpoint supports either invoice_id or checkout_id. :contentReference[oaicite:2]{index=2}
          - Collection/webhook state: PENDING | PROCESSING | COMPLETE | FAILED. :contentReference[oaicite:3]{index=3}
        """
        inv = (payload.get("invoice") or {}) if isinstance(payload, dict) else {}
        raw_state = inv.get("state") or payload.get("state") or payload.get("status")
        norm = _normalize_intasend_state(raw_state)

        # keep the raw provider state for debugging
        self.provider_status = raw_state or self.provider_status

        # backfill identifiers if present
        invoice_id = inv.get("invoice_id") or payload.get("invoice_id")
        api_ref = payload.get("api_ref") or payload.get("checkout_id") or payload.get("id")
        if api_ref and not self.api_ref:
            self.api_ref = api_ref
        if invoice_id and not self.provider_ref:
            # we store invoice_id early; may later be replaced by a final receipt
            self.provider_ref = invoice_id

        # possible M-Pesa receipt (if present in invoice)
        mpesa_receipt = inv.get("mpesa_receipt") or inv.get("receipt")

        if norm == "succeeded":
            self.status = "succeeded"
            self.completed_at = self.completed_at or _utcnow()
            if mpesa_receipt:
                self.provider_ref = mpesa_receipt  # upgrade to authoritative receipt
        elif norm == "failed":
            # don't downgrade a confirmed success
            if self.status != "succeeded":
                self.status = "failed"
                self.failure_reason = self.failure_reason or "intasend: FAILED"
        else:
            # pending or processing
            if self.status not in {"succeeded", "failed"}:
                self.status = "pending"

        self.updated_at = _utcnow()
        db.session.add(self)
        db.session.commit()
        return norm
