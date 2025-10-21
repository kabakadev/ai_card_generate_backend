from __future__ import annotations
import logging
import os
from datetime import datetime
from flask import request
from flask_restful import Resource

from config import db, limiter
from models import PaymentTransaction
from services.payment_utils import (
    resolve_transaction,
    backfill_invoice_id,
    check_payment_status_safe,
    normalize_payment_state,
    finalize_if_succeeded,
)

logger = logging.getLogger(__name__)


class IntaSendWebhook(Resource):
    """
    FIXED: Authoritative payment state updater with better error handling.
    """

    @limiter.limit("60 per minute")
    def post(self):
        payload = request.get_json(silent=True) or {}
        logger.info("[Webhook] received event keys=%s", sorted(list(payload.keys()))[:10])

        # Optional challenge validation
        expected = (os.getenv("INTASEND_WEBHOOK_CHALLENGE") or "").strip()
        got = (payload.get("challenge") or "").strip()
        if expected:
            if got != expected:
                logger.warning("[Webhook] challenge mismatch")
                return {"error": "unauthorized"}, 401
        else:
            logger.warning("[Webhook] challenge not configured")

        # Extract identifiers - be very tolerant of different payload shapes
        checkout_id = (
            payload.get("checkout_id") or 
            payload.get("id") or 
            payload.get("reference") or
            payload.get("api_ref")
        )
        
        # CRITICAL FIX: Look for invoice_id in multiple locations
        invoice_id = (
            payload.get("invoice_id") or
            (payload.get("invoice") or {}).get("invoice_id") or
            (payload.get("invoice") or {}).get("id") or
            payload.get("invoice_number")
        )
        
        raw_state = (
            payload.get("state") or 
            (payload.get("invoice") or {}).get("state") or
            payload.get("status")
        )
        paid_flag = bool(payload.get("paid"))

        # Try to discover user_id from api_ref pattern TX{id}
        tx_candidate = None
        user_id = None
        
        # Check all possible ref fields for TX pattern
        for ref_field in [checkout_id, payload.get("api_ref"), payload.get("reference")]:
            if ref_field and str(ref_field).upper().startswith("TX"):
                try:
                    tx_id = int(str(ref_field)[2:])
                    tx_candidate = PaymentTransaction.query.get(tx_id)
                    if tx_candidate:
                        user_id = tx_candidate.user_id
                        logger.info("[Webhook] Found tx via TX pattern: tx_id=%s", tx_id)
                        break
                except Exception:
                    pass

        # Fallback: search by invoice_id
        if not user_id and invoice_id:
            tx_candidate = (
                PaymentTransaction.query
                .filter_by(provider_ref=invoice_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )
            if tx_candidate:
                user_id = tx_candidate.user_id
                logger.info("[Webhook] Found tx via invoice_id: tx_id=%s", tx_candidate.id)

        # Fallback: search by checkout_id
        if not user_id and checkout_id:
            tx_candidate = (
                PaymentTransaction.query
                .filter_by(api_ref=checkout_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )
            if tx_candidate:
                user_id = tx_candidate.user_id
                logger.info("[Webhook] Found tx via checkout_id: tx_id=%s", tx_candidate.id)

        # Resolve to best match
        tx = resolve_transaction(user_id, checkout_id, invoice_id) if user_id else tx_candidate
        
        if not tx:
            logger.info("[Webhook] no transaction matched checkout=%s invoice=%s - accepted", 
                       checkout_id, invoice_id)
            return {"status": "accepted"}, 202

        # Idempotency: already succeeded
        if tx.status == "succeeded":
            logger.info("[Webhook] tx_id=%s already succeeded", tx.id)
            return {"status": "ok"}, 200

        # CRITICAL FIX: Backfill invoice_id from webhook payload first
        if invoice_id and not tx.provider_ref:
            logger.info("[Webhook] Backfilling invoice_id=%s for tx_id=%s", invoice_id, tx.id)
            backfill_invoice_id(tx, {"invoice_id": invoice_id})

        # Confirm with provider (authoritative check)
        status_info = check_payment_status_safe(tx, max_retries=2)
        
        # Try to backfill again from status response
        backfill_invoice_id(tx, status_info.get("raw") or {})

        # Decide state
        decided = status_info.get("normalized_state") or normalize_payment_state(raw_state)
        
        # Trust paid flag if provider says so
        if paid_flag and decided == "pending":
            logger.info("[Webhook] paid=True flag forces succeeded for tx_id=%s", tx.id)
            decided = "succeeded"

        logger.info("[Webhook] tx=%s provider_state=%s decided=%s paid=%s", 
                   tx.id, raw_state, decided, paid_flag)

        if decided == "succeeded":
            if finalize_if_succeeded(tx, status_info):
                logger.info("[Webhook] Successfully activated tx_id=%s", tx.id)
                return {"status": "ok"}, 200
            logger.error("[Webhook] activation failed for tx_id=%s", tx.id)
            return {"status": "error"}, 500

        if decided == "failed":
            try:
                tx.status = "failed"
                tx.failure_reason = "webhook: FAILED"
                tx.provider_status = status_info.get("status") or raw_state
                tx.updated_at = datetime.utcnow()
                db.session.add(tx)
                db.session.commit()
                logger.info("[Webhook] Marked tx_id=%s as failed", tx.id)
            except Exception:
                db.session.rollback()
                logger.exception("[Webhook] failed to mark tx_id=%s as failed", tx.id)
            return {"status": "ok"}, 200

        # Still pending
        try:
            tx.status = "pending"
            tx.provider_status = status_info.get("status") or raw_state
            tx.updated_at = datetime.utcnow()
            db.session.add(tx)
            db.session.commit()
            logger.info("[Webhook] Updated tx_id=%s to pending", tx.id)
        except Exception:
            db.session.rollback()
            logger.exception("[Webhook] failed to persist pending for tx_id=%s", tx.id)

        return {"status": "ok"}, 200