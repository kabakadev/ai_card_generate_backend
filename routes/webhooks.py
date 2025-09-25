from __future__ import annotations
import logging, os
from datetime import datetime
from flask import request
from flask_restful import Resource
from config import db, limiter
from models import PaymentTransaction
from services.intasend_client import get_intasend_client
from services.subscription_manager import activate

logger = logging.getLogger(__name__)

def _normalize_state(raw_status: str | None) -> str:
    s = (raw_status or "").strip().upper()
    if s in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    if s in {"PENDING", "PROCESSING"}:
        return "pending"
    if s in {"FAILED", "CANCELLED", "CANCELED", "EXPIRED", "RETRY"}:
        return "failed"
    return "pending"

def _backfill_invoice_id_from_info(tx: PaymentTransaction, info: dict) -> None:
    raw = info.get("raw") or {}
    found_invoice = (
        (raw.get("invoice") or {}).get("invoice_id")
        or raw.get("invoice_id")
        or info.get("invoice_id")
    )
    if found_invoice and not tx.provider_ref:
        tx.provider_ref = found_invoice
        db.session.commit()
        logger.info("Backfilled provider_ref(invoice_id) for tx id=%s → %s", tx.id, found_invoice)

def _mark_tx_and_activate(tx: PaymentTransaction, status_info: dict) -> None:
    receipt = None
    raw = status_info or {}
    if isinstance(raw.get("raw"), dict):
        receipt = raw["raw"].get("mpesa_receipt") or raw["raw"].get("receipt")
    if not receipt and isinstance(raw.get("invoice"), dict):
        receipt = raw["invoice"].get("mpesa_receipt") or raw["invoice"].get("receipt")
    tx.mark_succeeded(provider_ref=receipt)
    activate(
        tx.user_id,
        plan="monthly",
        amount=tx.amount,
        currency=tx.currency,
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

class IntaSendWebhook(Resource):
    @limiter.limit("60 per minute")  # small global burst
    def post(self):
        payload = request.get_json(silent=True) or {}
        logger.info("[Webhook] received payload")  # don't log body

        expected = (os.getenv("INTASEND_WEBHOOK_CHALLENGE") or "").strip()
        got = (payload.get("challenge") or "").strip()
        if not expected:
            logger.warning("[Webhook] challenge not configured")
            return {"error": "server_misconfigured"}, 500
        if got != expected:
            logger.warning("[Webhook] challenge mismatch")
            return {"error": "unauthorized"}, 401

        checkout_id = payload.get("checkout_id") or payload.get("id")
        api_ref     = payload.get("api_ref") or payload.get("reference")
        invoice_id  = payload.get("invoice_id") or (payload.get("invoice") or {}).get("invoice_id")
        raw_state   = payload.get("state") or (payload.get("invoice") or {}).get("state")
        norm        = _normalize_state(raw_state)

        # Resolve a local TX first (avoid provider calls if we don't have a match)
        tx = None
        if api_ref and str(api_ref).upper().startswith("TX"):
            try:
                tx = PaymentTransaction.query.get(int(str(api_ref)[2:]))
            except Exception:
                pass
        if not tx and invoice_id:
            tx = PaymentTransaction.query.filter_by(provider_ref=invoice_id).first()
        if not tx and checkout_id:
            tx = PaymentTransaction.query.filter_by(api_ref=checkout_id).first()

        if not tx:
            # Accept to prevent retry storms; FE verify/status can reconcile later
            return {"status": "accepted"}, 202

        if tx.status == "succeeded":
            return {"status": "ok"}, 200

        status_info = None
        try:
            client = get_intasend_client()
            status_info = client.check_payment_status(
                invoice_id=tx.provider_ref or None,
                checkout_id=tx.api_ref or None,
            )
        except Exception as e:
            logger.warning("[Webhook] status check failed: %s", e)
            status_info = {"state": raw_state}

        _backfill_invoice_id_from_info(tx, status_info or {})

        final_raw_state = (
            (status_info.get("invoice") or {}).get("state")
            or (status_info or {}).get("state")
            or raw_state
        )
        decided = _normalize_state(final_raw_state)
        logger.info("[Webhook] tx=%s state=%s → %s", tx.id, final_raw_state, decided)

        if decided == "succeeded":
            _mark_tx_and_activate(tx, status_info or {})
        elif decided == "failed":
            tx.mark_failed(reason="webhook: FAILED")
            db.session.commit()
        else:
            tx.status = "pending"
            tx.updated_at = datetime.utcnow()
            db.session.commit()

        return {"status": "ok"}, 200
