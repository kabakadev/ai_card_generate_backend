from __future__ import annotations
import logging
import os
import hmac
import hashlib
from flask import request, current_app
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import limiter
from models import PaymentTransaction
from services.payment_utils import (
    resolve_transaction,
)
from services.background_jobs import enqueue_intasend_webhook_job
import base64

logger = logging.getLogger(__name__)


_SIGNATURE_HEADER = "X-IntaSend-Signature"


def _normalize_signature(sig: str | None) -> tuple[str, str] | None:
    """
    Accept raw hex/base64 or prefixed formats like 'sha256=<hex>' or 'hmac <hex>'.
    DO NOT lowercase the value (base64 is case-sensitive).
    Returns (prefix, value) where prefix may be ''.
    """
    if not sig:
        return None
    v = sig.strip()
    if not v:
        return None

    # Common formats:
    #   'sha256=<hex>'
    #   'hmac <hex>'
    #   '<hex or base64>'
    if "=" in v:
        prefix, _, val = v.partition("=")
        return (prefix.strip().lower(), val.strip())
    if " " in v:
        prefix, _, val = v.partition(" ")
        return (prefix.strip().lower(), val.strip())
    return ("", v)

def _verify_intasend_signature(raw_body: bytes, signature_header: str | None) -> bool:
    secret = (current_app.config.get("INTASEND_WEBHOOK_SECRET") or "").strip()
    if not secret or not signature_header:
        return False

    parsed = _normalize_signature(signature_header)
    if not parsed:
        return False
    prefix, provided = parsed  # keep original case

    # Compute HMAC once
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    hex_digest = mac.hex()  # lowercase hex by definition
    b64_digest = base64.b64encode(mac).decode("ascii")

    # Accept plain hex (case-insensitive)
    if provided.lower() == hex_digest:
        return True

    # Accept base64
    if hmac.compare_digest(provided, b64_digest):
        return True

    # If a prefix was given (sha256/hmac), still accept either encoding
    if prefix in {"sha256", "hmac"}:
        if provided.lower() == hex_digest or hmac.compare_digest(provided, b64_digest):
            return True

    return False
class IntaSendWebhook(Resource):
    """
    FIXED: Authoritative payment state updater with better error handling.
    """

    @limiter.limit("60 per minute")
    def post(self):
        raw_body = request.get_data(cache=False) or b""
        logger.info("[Webhook] headers=%s", dict(request.headers))
        logger.info("[Webhook] body[:200]=%r", raw_body[:200])


        signature_required = current_app.config.get("INTASEND_WEBHOOK_SIGNATURE_REQUIRED", True)
        signature_header = request.headers.get(_SIGNATURE_HEADER)

        # Optional debug logging (remove later)
        logger.info("[Webhook] headers=%s", dict(request.headers))
        logger.info("[Webhook] body[:200]=%r", raw_body[:200])



        payload = request.get_json(silent=True) or {}
        expected_challenge = (os.getenv("INTASEND_WEBHOOK_CHALLENGE") or "").strip()
        got_challenge = (payload.get("challenge") or "").strip()

        if signature_required:
            if signature_header:
                if not _verify_intasend_signature(raw_body, signature_header):
                    logger.warning("[Webhook] signature verification failed from %s", request.remote_addr)
                    return {"error": "invalid_signature"}, 401
            else:
                # Fallback: allow if the body challenge matches EXACTLY
                if not expected_challenge or got_challenge != expected_challenge:
                    logger.warning("[Webhook] missing signature AND challenge mismatch from %s", request.remote_addr)
                    return {"error": "invalid_signature"}, 401
        else:
            if signature_header and not _verify_intasend_signature(raw_body, signature_header):
                logger.warning("[Webhook] signature mismatch while verification disabled (check configuration)")

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

        enqueue_ok = enqueue_intasend_webhook_job(
            tx_id=tx.id,
            checkout_id=checkout_id or tx.api_ref,
            invoice_id=invoice_id,
            raw_state=raw_state,
            paid_flag=paid_flag,
            payload=payload,
        )

        if not enqueue_ok:
            logger.error("[Webhook] queue full, cannot enqueue tx_id=%s", tx.id)
            return {"error": "queue_full"}, 503

        return {"status": "processing", "tx_id": tx.id}, 200


class IntaSendWebhookStatus(Resource):
    @jwt_required()
    def get(self):
        tx_id = request.args.get("tx_id", type=int)
        checkout_id = request.args.get("checkout_id") or request.args.get("api_ref")
        invoice_id = request.args.get("invoice_id")

        if not any([tx_id, checkout_id, invoice_id]):
            return {"error": "missing_identifier"}, 400

        tx = None
        if tx_id:
            tx = PaymentTransaction.query.get(tx_id)
        if not tx and checkout_id:
            tx = (
                PaymentTransaction.query
                .filter_by(api_ref=checkout_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )
        if not tx and invoice_id:
            tx = (
                PaymentTransaction.query
                .filter_by(provider_ref=invoice_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )

        if not tx:
            return {"error": "not_found"}, 404

        identity = get_jwt_identity()
        if isinstance(identity, dict):
            user_id = identity.get("id")
        else:
            user_id = identity
        if user_id and tx.user_id != user_id:
            return {"error": "forbidden"}, 403

        data = {
            "tx_id": tx.id,
            "status": tx.status,
            "provider_status": tx.provider_status,
            "provider_ref": tx.provider_ref,
            "api_ref": tx.api_ref,
            "updated_at": tx.updated_at.isoformat() + "Z" if tx.updated_at else None,
        }

        return {"transaction": data}, 200
