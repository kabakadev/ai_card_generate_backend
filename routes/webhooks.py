from __future__ import annotations
import logging
import os
import hmac
import hashlib
import base64
from flask import request, current_app
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import limiter
from models import PaymentTransaction
from services.payment_utils import resolve_transaction
from services.background_jobs import enqueue_intasend_webhook_job

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
    Authoritative payment state updater with resilient verification + tolerant tx matching.
    """

    @limiter.limit("60 per minute")
    def post(self):
        # Read RAW once; cache=True so request.get_json() can reuse it
        raw_body = request.get_data(cache=True) or b""

        signature_required = current_app.config.get("INTASEND_WEBHOOK_SIGNATURE_REQUIRED", True)
        signature_header = request.headers.get(_SIGNATURE_HEADER)

        # Optional debug logging (remove if too chatty)
        logger.info("[Webhook] headers=%s", dict(request.headers))
        logger.info("[Webhook] body[:200]=%r", raw_body[:200])

        payload = request.get_json(silent=True) or {}

        expected_challenge = (os.getenv("INTASEND_WEBHOOK_CHALLENGE") or "").strip()
        got_challenge = (payload.get("challenge") or "").strip()

        devhooks_q = (os.getenv("DEVHOOKS_QUERY_TOKEN") or "").strip()
        got_q = (request.args.get("vh") or "").strip()

        # --- Verification ---
        if signature_required:
            if signature_header:
                if not _verify_intasend_signature(raw_body, signature_header):
                    logger.warning("[Webhook] signature verification failed from %s", request.remote_addr)
                    return {"error": "invalid_signature"}, 401
            else:
                # No signature → accept if query token matches OR challenge matches
                if devhooks_q and got_q == devhooks_q:
                    pass  # accept
                elif expected_challenge and got_challenge == expected_challenge:
                    pass  # accept
                else:
                    logger.warning("[Webhook] missing signature and no valid vh/challenge from %s", request.remote_addr)
                    return {"error": "invalid_signature"}, 401
        else:
            if signature_header and not _verify_intasend_signature(raw_body, signature_header):
                logger.warning("[Webhook] signature mismatch while verification disabled (check configuration)")

        # --- Extract identifiers (tolerant) ---
        checkout_id = (
            payload.get("checkout_id")
            or payload.get("id")
            or payload.get("reference")
            or payload.get("api_ref")
        )

        invoice_id = (
            payload.get("invoice_id")
            or (payload.get("invoice") or {}).get("invoice_id")
            or (payload.get("invoice") or {}).get("id")
            or payload.get("invoice_number")
        )

        raw_state = (
            payload.get("state")
            or (payload.get("invoice") or {}).get("state")
            or payload.get("status")
        )
        paid_flag = bool(payload.get("paid"))

        # --- Try to infer user/tx quickly ---
        tx_candidate = None
        user_id = None

        # Pattern TX{id} in api_ref/reference/checkout_id
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

        # Fallback by invoice_id
        if not tx_candidate and invoice_id:
            t = (
                PaymentTransaction.query
                .filter_by(provider_ref=invoice_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )
            if t:
                tx_candidate = t
                user_id = t.user_id
                logger.info("[Webhook] Found tx via invoice_id: tx_id=%s", t.id)

        # Fallback by checkout_id/api_ref
        if not tx_candidate and checkout_id:
            t = (
                PaymentTransaction.query
                .filter_by(api_ref=checkout_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )
            if t:
                tx_candidate = t
                user_id = t.user_id
                logger.info("[Webhook] Found tx via checkout_id: tx_id=%s", t.id)

        # --- Resolve final tx (do NOT discard candidate if resolve_transaction returns None) ---
        tx = None
        if user_id:
            try:
                tx = resolve_transaction(user_id, checkout_id, invoice_id)
            except Exception:
                # Be defensive; do not fail the webhook because resolve_transaction is strict
                tx = None

        if not tx:
            tx = tx_candidate  # keep the candidate we already found

        if not tx:
            logger.info("[Webhook] no transaction matched checkout=%s invoice=%s - accepted", checkout_id, invoice_id)
            # (Optional: persist raw payload for reconciliation)
            return {"status": "accepted"}, 202

        # --- Enqueue processing (even if state is PENDING/PROCESSING) ---
        enqueue_ok = enqueue_intasend_webhook_job(
            tx_id=tx.id,
            checkout_id=checkout_id or tx.api_ref,
            invoice_id=invoice_id,
            raw_state=raw_state,
            paid_flag=paid_flag,
            payload=payload,
        )

        # after enqueue_ok is True in IntaSendWebhook.post
        try:
            from services.intasend_webhook_processor import process_intasend_webhook
            logger.info("[Webhook] debug-run processor for tx_id=%s", tx.id)
            process_intasend_webhook(
                tx_id=tx.id,
                checkout_id=checkout_id or tx.api_ref,
                invoice_id=invoice_id,
                raw_state=raw_state,
                paid_flag=paid_flag,
                payload=payload,
            )
        except Exception as e:
            logger.exception("[Webhook] debug-run failed: %s", e)


        if not enqueue_ok:
            logger.error("[Webhook] queue full, cannot enqueue tx_id=%s", tx.id)
            return {"error": "queue_full"}, 503

        return {"status": "processing", "tx_id": tx.id}, 200


class IntaSendWebhookStatus(Resource):
    @jwt_required()
    def get(self):
        tx_id = request.args.get("tx_id", type=int)
        checkout_id = request.args.get("checkout_id") or request.args.get("api_ref")
        invoice_id = request.get_json(silent=True) or request.args.get("invoice_id")

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
        user_id = identity.get("id") if isinstance(identity, dict) else identity
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
