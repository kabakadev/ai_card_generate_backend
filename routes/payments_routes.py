# routes/payments_routes.py — IntaSend-aligned, global CORS, rate limits, redirect allowlist, fast verify (webhook moved out)
from __future__ import annotations

from datetime import datetime
import logging
import os
from urllib.parse import urlparse

from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import db, app, limiter
from models import PaymentTransaction, User
from services.intasend_client import get_intasend_client, IntaSendError
from services.subscription_manager import activate, is_active
from services.usage_tracker import snapshot

from flask_limiter.util import get_remote_address
from flask_jwt_extended import verify_jwt_in_request


logger = logging.getLogger(__name__)

# ---- helpers ----------------------------------------------------------------

def _resolve_user_id(identity):
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None

def _normalize_state(raw_status: str | None) -> str:
    s = (raw_status or "").strip().upper()
    # IntaSend state set includes COMPLETE/FAILED/PENDING/PROCESSING
    if s in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    if s in {"PENDING", "PROCESSING"}:
        return "pending"
    if s in {"FAILED", "CANCELLED", "CANCELED", "EXPIRED", "RETRY"}:
        return "failed"
    return "pending"

def _latest_pending_tx_for_user(user_id: int) -> PaymentTransaction | None:
    return (
        PaymentTransaction.query
        .filter_by(user_id=user_id, status="pending")
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )

def _mark_tx_and_activate(tx: PaymentTransaction, status_info: dict) -> None:
    # Try to capture a receipt/mpesa code if present
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
        amount=tx.amount or int(app.config.get("BILLING_PLAN_MONTHLY_KES", 100)),
        currency=tx.currency or str(app.config.get("BILLING_CURRENCY", "KES")),
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    return ("*" * max(0, len(s) - keep)) + s[-keep:]

def _require_intasend_ready() -> tuple[bool, dict]:
    """Sanity-check env before we even try to build the client."""
    pub = os.getenv("INTASEND_PUBLIC_KEY", "") or app.config.get("INTASEND_PUBLIC_KEY", "")
    sec = os.getenv("INTASEND_SECRET_KEY", "") or app.config.get("INTASEND_SECRET_KEY", "")
    test = str(os.getenv("INTASEND_TEST_MODE", app.config.get("INTASEND_TEST_MODE", "true"))).strip().lower()
    ok = bool(pub) and bool(sec)
    return ok, {"public_key": _mask(pub), "secret_key": _mask(sec), "test_mode": test}

def _backfill_invoice_id_from_info(tx: PaymentTransaction, info: dict) -> bool:
    """
    If IntaSend returned an invoice_id and our tx.provider_ref is empty,
    persist it so future lookups can prefer invoice_id.
    Returns True if we updated the tx.
    """
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
        return True
    return False

def _allowed_redirect(url: str | None) -> str | None:
    """
    Allow only hosts in ALLOWED_REDIRECT_HOSTS (comma-separated).
    Falls back to common local/known hosts if env not set.
    """
    if not url:
        return None
    try:
        allowed = [h.strip().lower() for h in (os.getenv("ALLOWED_REDIRECT_HOSTS","")).split(",") if h.strip()]
        if not allowed:
            allowed = ["aiflashcard254.netlify.app", "localhost:5173", "127.0.0.1:5173"]
        host = urlparse(url).netloc.lower()
        return url if host in set(allowed) else None
    except Exception:
        return None
    
def _rl_key_user_or_ip():
    """
    Per-user key for rate limits:
    - If a valid JWT is present, use user id (so limits follow the account)
    - Otherwise fall back to client IP (for unauthenticated routes)
    """
    try:
        # Won't raise if missing/invalid when optional=True
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
# Checkout
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

        amount = int(app.config.get("BILLING_PLAN_MONTHLY_KES", 100))
        currency = str(app.config.get("BILLING_CURRENCY", "KES"))

        body = request.get_json(silent=True) or {}
        phone = (body.get("phone_number") or body.get("phone") or "").strip() or None

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
            plan_type="monthly",
            status="initiated",
        )
        db.session.add(tx)
        db.session.commit()

        # 🔎 Env sanity
        ready, _dbg = _require_intasend_ready()
        if not ready:
            tx.status = "failed"
            tx.failure_reason = "missing IntaSend keys in backend environment"
            db.session.commit()
            logger.error("[BillingCheckout] Missing IntaSend env")
            return {
                "error": "intasend_not_configured",
                "detail": "Backend is missing INTASEND_PUBLIC_KEY / INTASEND_SECRET_KEY",
            }, 500

        # Build client safely
        try:
            client = get_intasend_client()
        except Exception as e:
            tx.status = "failed"
            tx.failure_reason = f"client_init_error: {e}"
            db.session.commit()
            logger.exception("[BillingCheckout] IntaSend client init failed")
            return {"error": "intasend_client_init_failed", "detail": str(e)}, 500

        # Create checkout
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
        checkout_id = checkout.get("checkout_id") or checkout.get("id")
        invoice_id = checkout.get("invoice_id") or (checkout.get("invoice") or {}).get("invoice_id")

        if not checkout_url or not checkout_id:
            tx.status = "failed"
            tx.failure_reason = "missing checkout_url/checkout_id"
            db.session.commit()
            logger.error("[BillingCheckout] Invalid checkout response: %s", checkout)
            return {"error": "invalid_checkout_response"}, 502

        tx.api_ref = checkout_id
        if invoice_id and not tx.provider_ref:
            tx.provider_ref = invoice_id
        tx.status = "pending"
        db.session.commit()

        logger.info(
            "[BillingCheckout] checkout_id=%s test_mode=%s api_base=%s",
            checkout_id, getattr(client, "test_mode", None), getattr(client, "base_url", None)
        )

        return {
            "checkout_url": checkout_url,
            "api_ref": checkout_id,
            "amount": amount,
            "currency": currency,
            "plan": "monthly",
        }, 200


# ---------------------------
# Status (with safe auto-reconcile + invoice backfill)
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
            logger.info("BillingStatus check for user_id=%s", user_id)
            active, sub = is_active(user_id)

            reconcile_attempted = False
            tx = None

            if not active:
                tx = _latest_pending_tx_for_user(user_id)
                if tx:
                    reconcile_attempted = True
                    try:
                        client = get_intasend_client()
                    except Exception as e:
                        logger.warning("[BillingStatus] client init failed: %s", e)
                        client = None

                    if client:
                        try:
                            info = client.check_payment_status(
                                invoice_id=tx.provider_ref or None,
                                checkout_id=tx.api_ref or None,
                            )

                            # 🔁 Backfill invoice_id if we only had checkout_id
                            _backfill_invoice_id_from_info(tx, info)

                            raw_state = (
                                (info.get("invoice") or {}).get("state")
                                or info.get("state")
                                or info.get("status")
                            )
                            norm = _normalize_state(raw_state)
                            logger.info("Reconcile tx=%s raw_state=%s → %s", tx.id, raw_state, norm)

                            if norm == "succeeded":
                                _mark_tx_and_activate(tx, info)
                                active, sub = is_active(user_id)
                                logger.info("Auto-reconcile activation done. active=%s", active)
                            elif norm == "failed":
                                tx.mark_failed(reason="auto-reconcile: FAILED from IntaSend", provider_status=raw_state)
                                db.session.commit()
                            # else: still pending

                        except IntaSendError as e:
                            logger.warning("Auto-reconcile failed for tx id=%s: %s", getattr(tx, "id", None), e)

            usage = snapshot(user)
            pending_tx = _latest_pending_tx_for_user(user_id)

            return {
                "subscription_status": "active" if active else "inactive",
                "plan": (sub.plan_type if sub else "monthly"),
                "current_period_end": (sub.end_date.isoformat() + "Z") if (sub and sub.end_date) else None,
                "month_key": usage.get("month_key"),
                "month_free_limit": usage.get("free_quota"),
                "month_used": usage.get("used"),
                "month_remaining": usage.get("remaining"),
                "debug": {
                    "user_id": user_id,
                    "active_server_view": bool(active),
                    "reconcile_attempted": reconcile_attempted,
                    "pending_tx_id": pending_tx.id if pending_tx else None,
                    "pending_tx_status": pending_tx.status if pending_tx else None,
                    "pending_tx_provider_ref": pending_tx.provider_ref if pending_tx else None,
                    "pending_tx_api_ref": pending_tx.api_ref if pending_tx else None,
                },
            }, 200

        except Exception as e:
            logger.exception("[BillingStatus] unhandled error")
            return {"error": "server_error", "detail": str(e)}, 500


# ---------------------------
# Verify (redirect/poll flow) — single quick check (no long sleep)
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
        # Accept a variety of keys; tolerate having only checkout_id
        invoice_id = (body.get("invoice_id") or body.get("invoice") or body.get("id") or "").strip() or None
        checkout_id = (
            body.get("checkout_id") or body.get("checkoutId") or body.get("api_ref") or body.get("id") or ""
        ).strip() or None

        logger.warning(
            "VerifyPayment user_id=%s invoice_id=%s checkout_id=%s",
            user_id, invoice_id, checkout_id
        )

        # ---- choose the right TX (prefer the one matching checkout_id) ----
        tx = None
        if checkout_id:
            tx = (
                PaymentTransaction.query
                .filter_by(user_id=user_id, api_ref=checkout_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )

        if not tx:
            # fall back to latest pending for this user
            tx = (
                PaymentTransaction.query
                .filter_by(user_id=user_id, status="pending")
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )

        if not tx:
            return {"error": "no_pending_tx"}, 404

        # Backfill any refs we learned from FE onto THIS tx
        changed = False
        if checkout_id and not tx.api_ref:
            tx.api_ref = checkout_id; changed = True
        if invoice_id and not tx.provider_ref:
            tx.provider_ref = invoice_id; changed = True
        if changed:
            db.session.commit()

        try:
            client = get_intasend_client()
            info = client.check_payment_status(
                invoice_id=invoice_id or tx.provider_ref or None,
                checkout_id=checkout_id or tx.api_ref or None,
            )

            # 🔁 Backfill invoice_id if we only had checkout_id
            _backfill_invoice_id_from_info(tx, info)

            # IntaSend exposes state either at top-level or under "invoice"
            raw = info.get("raw") or {}
            inv = (raw.get("invoice") or {}) if isinstance(raw, dict) else {}
            raw_state = (inv.get("state") or info.get("state") or info.get("status"))
            norm = _normalize_state(raw_state)

            if norm == "succeeded":
                _mark_tx_and_activate(tx, info)
                active, sub = is_active(user_id)
                return {
                    "status": "activated",
                    "subscription_status": "active" if active else "inactive",
                    "current_period_end": sub.end_date.isoformat() + "Z" if (sub and sub.end_date) else None,
                    "attempts": 1,
                }, 200

            if norm == "failed":
                tx.mark_failed(reason="verify: FAILED from IntaSend", provider_status=raw_state)
                db.session.commit()
                return {"status": "failed", "attempts": 1}, 200

        except Exception as e:
            logger.warning("[Verify] quick check error: %s", e)

        return {"status": "pending", "attempts": 1}, 200


# routes/payments_routes.py  (debug)
class DebugIntaSendStatus(Resource):
    @jwt_required()
    @limiter.limit("30 per minute")
    def get(self):
        checkout_id = (request.args.get("checkout_id") or "").strip()
        invoice_id = (request.args.get("invoice_id") or "").strip() or None
        if not checkout_id and not invoice_id:
            return {"error": "pass ?checkout_id=... or ?invoice_id=..."}, 400

        tx = None
        if checkout_id:
            tx = PaymentTransaction.query.filter_by(api_ref=checkout_id).order_by(PaymentTransaction.created_at.desc()).first()
        if not tx and invoice_id:
            tx = PaymentTransaction.query.filter_by(provider_ref=invoice_id).order_by(PaymentTransaction.created_at.desc()).first()

        try:
            client = get_intasend_client()
            info = client.check_payment_status(invoice_id=invoice_id, checkout_id=checkout_id)
        except Exception as e:
            info = {"error": str(e)}

        db_view = None
        if tx:
            db_view = {
                "tx_id": tx.id,
                "status": tx.status,
                "provider_status": tx.provider_status,
                "provider_ref(invoice_id)": tx.provider_ref,
                "api_ref(checkout_id)": tx.api_ref,
                "updated_at": tx.updated_at.isoformat() + "Z" if tx.updated_at else None,
            }
        return {"db": db_view, "intasend": info}, 200
