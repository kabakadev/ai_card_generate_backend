# routes/payments_routes.py — FIXED VERSION
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import os
from urllib.parse import urlparse
from time import sleep

from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity, verify_jwt_in_request
from flask_limiter.util import get_remote_address

from config import db, app, limiter
from models import PaymentTransaction, User
from services.intasend_client import get_intasend_client, IntaSendError
from services.subscription_manager import is_active
from services.usage_tracker import snapshot
from services.payment_reconciliation import quick_reconcile_payment
from services.payment_utils import (
    resolve_transaction,
    backfill_invoice_id,
    activate_subscription_for_transaction,
    check_payment_status_safe,
)

logger = logging.getLogger(__name__)


def _resolve_user_id(identity):
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    return ("*" * max(0, len(s) - keep)) + s[-keep:]


def _require_intasend_ready() -> tuple[bool, dict]:
    pub = os.getenv("INTASEND_PUBLIC_KEY", "") or app.config.get("INTASEND_PUBLIC_KEY", "")
    sec = os.getenv("INTASEND_SECRET_KEY", "") or app.config.get("INTASEND_SECRET_KEY", "")
    mode = (os.getenv("INTASEND_MODE", "") or os.getenv("INTASEND_TEST_MODE", "true")).lower()
    ok = bool(pub) and bool(sec)
    return ok, {"public_key": _mask(pub), "secret_key": _mask(sec), "mode": mode}


def _allowed_redirect(url: str | None) -> str | None:
    if not url:
        return None
    try:
        allowed = [h.strip().lower() for h in (os.getenv("ALLOWED_REDIRECT_HOSTS", "")).split(",") if h.strip()]
        if not allowed:
            allowed = ["aiflashcard254.netlify.app", "localhost:5173", "127.0.0.1:5173"]
        host = urlparse(url).netloc.lower()
        return url if host in set(allowed) else None
    except Exception:
        return None


def _rl_key_user_or_ip():
    """Per-user limiter when JWT present, else IP-based."""
    try:
        verify_jwt_in_request(optional=True)
        uid = get_jwt_identity()
        if isinstance(uid, dict):
            uid = uid.get("id")
        if uid is not None:
            return f"user:{uid}"
    except Exception:
        pass
    return get_remote_address()


# ---------------------------
# Checkout (FIXED)
# ---------------------------
class BillingCheckout(Resource):
    def options(self):
        return {}, 204

    @jwt_required()
    @limiter.limit("3 per minute; 10 per hour", key_func=_rl_key_user_or_ip, override_defaults=False)
    def post(self):
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        body = request.get_json(silent=True) or {}
        phone = (body.get("phone_number") or body.get("phone") or "").strip() or None
        requested_plan = (body.get("plan_type") or "monthly").strip().lower()
        if requested_plan not in {"monthly", "daily"}:
            requested_plan = "monthly"

        if requested_plan == "daily":
            amount = int(app.config.get("BILLING_PLAN_DAILY_KES", app.config.get("BILLING_PLAN_MONTHLY_KES", 100)))
        else:
            amount = int(app.config.get("BILLING_PLAN_MONTHLY_KES", 100))
        currency = str(app.config.get("BILLING_CURRENCY", "KES"))

        redirect_url = _allowed_redirect((body.get("redirect_url") or "").strip())
        if not redirect_url:
            origin = (request.headers.get("Origin") or "").rstrip("/")
            redirect_url = _allowed_redirect(f"{origin}/billing/return") if origin else None

        user = User.query.get(user_id)
        user_email = (getattr(user, "email", "") or "").strip() if user else ""
        if not user_email:
            user_email = f"user{user_id}@flashlearn.local"

        # Create local tx first
        tx = PaymentTransaction(
            user_id=user_id,
            amount=amount,
            currency=currency,
            payment_method="mpesa",
            provider="intasend",
            plan_type=requested_plan,
            status="initiated",
        )
        db.session.add(tx)
        db.session.commit()

        ready, dbg = _require_intasend_ready()
        if not ready:
            tx.status = "failed"
            tx.failure_reason = "missing IntaSend keys in backend environment"
            db.session.commit()
            logger.error("[BillingCheckout] Missing IntaSend env: %s", dbg)
            return {"error": "intasend_not_configured"}, 500

        try:
            client = get_intasend_client()
        except Exception as e:
            tx.status = "failed"
            tx.failure_reason = f"client_init_error: {e}"
            db.session.commit()
            logger.exception("[BillingCheckout] IntaSend client init failed")
            return {"error": "intasend_client_init_failed", "detail": str(e)}, 500

        try:
            checkout = client.create_checkout(
                amount=amount,
                email=user_email,
                phone_number=phone,
                currency=currency,
                api_ref=f"TX{tx.id}",
                redirect_url=redirect_url,
            )
        except IntaSendError as e:
            tx.status = "failed"
            tx.failure_reason = str(e)
            db.session.commit()
            logger.warning("[BillingCheckout] create_checkout failed: %s", e)
            return {"error": "checkout_creation_failed", "detail": str(e)}, 502
        except Exception as e:
            tx.status = "failed"
            tx.failure_reason = f"unexpected: {e}"
            db.session.commit()
            logger.exception("[BillingCheckout] unexpected error")
            return {"error": "unexpected", "detail": str(e)}, 500

        checkout_url = checkout.get("checkout_url") or checkout.get("url")
        checkout_id = checkout.get("checkout_id") or checkout.get("id") or checkout.get("api_ref")
        invoice_id = checkout.get("invoice_id")

        if not checkout_url or not checkout_id:
            tx.status = "failed"
            tx.failure_reason = "missing checkout_url/checkout_id"
            db.session.commit()
            logger.error("[BillingCheckout] Invalid checkout response: %s", checkout)
            return {"error": "invalid_checkout_response"}, 502

        # CRITICAL FIX: Store both checkout_id AND invoice_id immediately
        tx.api_ref = checkout_id
        if invoice_id:
            tx.provider_ref = invoice_id
            logger.info("[BillingCheckout] Got invoice_id=%s immediately", _mask(invoice_id))
        else:
            logger.warning("[BillingCheckout] No invoice_id yet, will backfill later")
        
        tx.status = "pending"
        db.session.commit()

        logger.info(
            "[BillingCheckout] checkout_id=%s invoice_id=%s test_mode=%s api_base=%s",
            checkout_id, _mask(invoice_id), getattr(client, "test_mode", None), getattr(client, "base_url", None)
        )

        return {
            "checkout_url": checkout_url,
            "api_ref": checkout_id,
            "invoice_id": invoice_id,
            "amount": amount,
            "currency": currency,
            "plan": requested_plan,
        }, 200


# ---------------------------
# Status (FIXED with better reconciliation)
# ---------------------------
class BillingStatus(Resource):
    def options(self):
        return {}, 204

    @jwt_required()
    @limiter.limit("60 per minute")
    def get(self):
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        user = User.query.get(user_id)
        if not user:
            return {"error": "user not found"}, 404

        try:
            q_checkout = (request.args.get("checkout_id") or request.args.get("api_ref") or "").strip() or None
            q_invoice = (request.args.get("invoice_id") or "").strip() or None

            logger.info("BillingStatus check for user_id=%s", user_id)
            
            # FIXED: Only reconcile if we have specific identifiers or very recent pending
            reconcile_attempted = False
            tx = resolve_transaction(user_id, q_checkout, q_invoice)
            if tx and tx.status in ("pending", "initiated"):
                # Only reconcile if tx is less than 5 minutes old to avoid hammering API
                if tx.created_at and (datetime.utcnow() - tx.created_at) < timedelta(minutes=5):
                    reconcile_attempted = quick_reconcile_payment(user_id)
            
            active, sub = is_active(user_id)
            if tx and tx.status == "succeeded" and not active:
                try:
                    activated_now = activate_subscription_for_transaction(tx, {})
                    if activated_now:
                        active, sub = is_active(user_id)
                        logger.info(
                            "[BillingStatus] backstop activation applied tx_id=%s user_id=%s",
                            tx.id,
                            user_id,
                        )
                except Exception:
                    logger.exception(
                        "[BillingStatus] backstop activation failed tx_id=%s user_id=%s",
                        getattr(tx, "id", None),
                        user_id,
                    )
            usage = snapshot(user)

            debug = {
                "user_id": user_id,
                "active_server_view": bool(active),
                "reconcile_attempted": reconcile_attempted,
                "pending_tx_id": getattr(tx, "id", None),
                "pending_tx_status": getattr(tx, "status", None),
                "pending_tx_provider_ref": getattr(tx, "provider_ref", None),
                "pending_tx_api_ref": getattr(tx, "api_ref", None),
            }

            return {
                "subscription_status": "active" if active else "inactive",
                "plan": (sub.plan_type if sub else "monthly"),
                "current_period_end": (sub.end_date.isoformat() + "Z") if (sub and sub.end_date) else None,
                "month_key": usage.get("month_key"),
                "month_free_limit": usage.get("free_quota"),
                "month_used": usage.get("used"),
                "month_remaining": usage.get("remaining"),
                "debug": debug,
            }, 200

        except Exception as e:
            logger.exception("[BillingStatus] unhandled error")
            return {"error": "server_error", "detail": str(e)}, 500


# ---------------------------
# Verify (FIXED with better retry logic)
# ---------------------------
class VerifyPayment(Resource):
    def options(self):
        return {}, 204

    @jwt_required()
    @limiter.limit("10 per minute", key_func=_rl_key_user_or_ip, override_defaults=False)
    def post(self):
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        body = request.get_json(silent=True) or {}
        invoice_id = (body.get("invoice_id") or body.get("invoice") or body.get("id") or "").strip() or None
        checkout_id = (body.get("checkout_id") or body.get("api_ref") or body.get("id") or "").strip() or None

        logger.warning("VerifyPayment user_id=%s invoice_id=%s checkout_id=%s", user_id, invoice_id, checkout_id)

        tx = resolve_transaction(user_id, checkout_id, invoice_id)
        if not tx:
            return {"status": "unknown", "message": "no_matching_transaction"}, 404

        # Backfill any missing IDs
        changed = False
        if checkout_id and not tx.api_ref:
            tx.api_ref = checkout_id
            changed = True
        if invoice_id and not tx.provider_ref:
            tx.provider_ref = invoice_id
            changed = True
        if changed:
            db.session.add(tx)
            db.session.commit()

        # Already succeeded
        if tx.status == "succeeded":
            active, sub = is_active(user_id)
            return {
                "status": "activated",
                "subscription_status": "active" if active else "inactive",
                "current_period_end": sub.end_date.isoformat() + "Z" if (sub and sub.end_date) else None,
            }, 200

        # CRITICAL FIX: Only check status if we have invoice_id
        # Otherwise, payment is too new - rely on webhook
        if not tx.provider_ref:
            logger.info("[VerifyPayment] No invoice_id yet for tx_id=%s - payment being processed", tx.id)
            return {"status": "pending", "message": "payment_processing"}, 200

        # We have invoice_id, safe to check
        status_info = check_payment_status_safe(tx, max_retries=1)
        backfill_invoice_id(tx, status_info.get("raw") or {})
        norm = status_info.get("normalized_state", "pending")

        if norm == "succeeded":
            activated = activate_subscription_for_transaction(tx, status_info.get("raw") or {})
            active, sub = is_active(user_id)
            if activated and active:
                return {
                    "status": "activated",
                    "subscription_status": "active",
                    "current_period_end": sub.end_date.isoformat() + "Z" if (sub and sub.end_date) else None,
                }, 200
            logger.warning("[VerifyPayment] activation failed for tx_id=%s", tx.id)
            return {"status": "error", "message": "activation_failed"}, 500

        if norm == "failed":
            try:
                tx.status = "failed"
                tx.failure_reason = "verify: payment failed"
                tx.provider_status = status_info.get("status") or tx.provider_status
                tx.updated_at = datetime.utcnow()
                db.session.add(tx)
                db.session.commit()
            except Exception as exc:
                logger.exception("[VerifyPayment] failed to mark transaction as failed: %s", exc)
                db.session.rollback()
            return {"status": "failed"}, 200

        return {"status": "pending"}, 200


# ---------------------------
# Debug endpoint
# ---------------------------
class DebugIntaSendStatus(Resource):
    @jwt_required()
    @limiter.limit("30 per minute")
    def get(self):
        checkout_id = (request.args.get("checkout_id") or request.args.get("api_ref") or "").strip()
        invoice_id = (request.args.get("invoice_id") or "").strip() or None
        if not checkout_id and not invoice_id:
            return {"error": "pass ?checkout_id=... or ?invoice_id=..."}, 400

        tx = None
        if checkout_id:
            tx = (PaymentTransaction.query
                  .filter_by(api_ref=checkout_id)
                  .order_by(PaymentTransaction.created_at.desc())
                  .first())
        if not tx and invoice_id:
            tx = (PaymentTransaction.query
                  .filter_by(provider_ref=invoice_id)
                  .order_by(PaymentTransaction.created_at.desc())
                  .first())

        info = check_payment_status_safe(tx) if tx else {"status": "pending", "raw": {}, "normalized_state": "pending"}
        db_view = None
        if tx:
            db_view = {
                "tx_id": tx.id,
                "status": tx.status,
                "provider_status": tx.provider_status,
                "provider_ref(invoice_id)": tx.provider_ref,
                "api_ref(checkout_id)": tx.api_ref,
                "updated_at": tx.updated_at.isoformat() + "Z" if tx.updated_at else None,
                "created_at": tx.created_at.isoformat() + "Z" if tx.created_at else None,
            }
        return {"db": db_view, "intasend": info}, 200
