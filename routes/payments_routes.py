# routes/payments_routes.py — IntaSend-aligned (drop-in), CORS + hardening
from __future__ import annotations

from datetime import datetime
from time import sleep
import logging
import os

from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from flask_cors import cross_origin

from config import db, app
from models import PaymentTransaction, Subscription, User
from services.intasend_client import (
    get_intasend_client,
    IntaSendError,
)
from services.subscription_manager import activate, is_active
from services.usage_tracker import snapshot

logger = logging.getLogger(__name__)

# ---- CORS (route-level) ------------------------------------------------------
_CORS_ARGS = dict(
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "OPTIONS"],
)

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

# ---------------------------
# Checkout
# ---------------------------
class BillingCheckout(Resource):
    @cross_origin(**_CORS_ARGS)
    def options(self):
        return {}, 204

    @cross_origin(**_CORS_ARGS)
    @jwt_required()
    def post(self):
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        amount = int(app.config.get("BILLING_PLAN_MONTHLY_KES", 100))
        currency = str(app.config.get("BILLING_CURRENCY", "KES"))

        body = request.get_json(silent=True) or {}
        phone = (body.get("phone_number") or body.get("phone") or "").strip() or None

        redirect_url = (body.get("redirect_url") or "").strip()
        if not redirect_url:
            origin = (request.headers.get("Origin") or "").rstrip("/")
            redirect_url = f"{origin}/billing/return" if origin else None

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
        ready, dbg = _require_intasend_ready()
        if not ready:
            tx.status = "failed"
            tx.failure_reason = "missing IntaSend keys in backend environment"
            db.session.commit()
            logger.error("[BillingCheckout] Missing IntaSend env: %s", dbg)
            return {
                "error": "intasend_not_configured",
                "detail": "Backend is missing INTASEND_PUBLIC_KEY / INTASEND_SECRET_KEY",
                "debug": dbg,
            }, 500

        # Build client safely
        try:
            client = get_intasend_client()
        except Exception as e:
            tx.status = "failed"
            tx.failure_reason = f"client_init_error: {e}"
            db.session.commit()
            logger.exception("[BillingCheckout] IntaSend client init failed")
            return {"error": "intasend_client_init_failed", "detail": str(e), "debug": dbg}, 500

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

        # Optional: log the environment you used
        logger.warning(
            "[BillingCheckout] checkout_url=%s checkout_id=%s test_mode=%s api_base=%s",
            checkout_url, checkout_id, getattr(client, "test_mode", None), getattr(client, "base_url", None)
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
    @cross_origin(**_CORS_ARGS)
    def options(self):
        return {}, 204

    @cross_origin(**_CORS_ARGS)
    @jwt_required()
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
# Verify (redirect/poll flow) — choose TX by checkout_id + invoice backfill + immediate-by-invoice retry
# ---------------------------
class VerifyPayment(Resource):
    @cross_origin(**_CORS_ARGS)
    def options(self):
        return {}, 204

    @cross_origin(**_CORS_ARGS)
    @jwt_required()
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
        # Optional audit extras from FE
        tracking_id = (body.get("tracking_id") or "").strip() or None
        signature = (body.get("signature") or "").strip() or None

        logger.warning(
            "VerifyPayment user_id=%s invoice_id=%s checkout_id=%s tracking=%s",
            user_id, invoice_id, checkout_id, tracking_id
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
        except Exception as e:
            logger.exception("[VerifyPayment] IntaSend client init failed")
            return {"error": "intasend_client_init_failed", "detail": str(e)}, 500

        # Longer backoff — ~32s total (0.5 + 1 + 2 + 4 + 8 + 16)
        delays = [0.5, 1, 2, 4, 8, 16]
        last_raw_state = None

        for attempt, delay in enumerate(delays, 1):
            if attempt > 1:
                logger.info("Verify attempt %d/%d tx=%s (sleep %ss)", attempt, len(delays), tx.id, delay)
                sleep(delay)

            try:
                info = client.check_payment_status(
                    invoice_id=invoice_id or tx.provider_ref or None,
                    checkout_id=checkout_id or tx.api_ref or None,
                )

                # 🔁 Backfill invoice_id if we only had checkout_id
                discovered = _backfill_invoice_id_from_info(tx, info)
                if discovered:
                    invoice_id = tx.provider_ref  # use it immediately if we retry

                # IntaSend exposes state either at top-level or under "invoice"
                raw = info.get("raw") or {}
                inv = (raw.get("invoice") or {}) if isinstance(raw, dict) else {}
                raw_state = (inv.get("state") or info.get("state") or info.get("status"))
                norm = _normalize_state(raw_state)
                last_raw_state = raw_state
                logger.warning(
                    "[Verify] attempt=%s tx=%s state(raw)=%s norm=%s invoice_id=%s checkout_id=%s",
                    attempt, tx.id, raw_state, norm, tx.provider_ref, tx.api_ref
                )

                # ✅ Immediate-by-invoice retry: if we just discovered invoice_id and still have no state
                if discovered and not raw_state and tx.provider_ref:
                    try:
                        info2 = client.check_payment_status(invoice_id=tx.provider_ref, checkout_id=None)
                        raw2 = info2.get("raw") or {}
                        inv2 = (raw2.get("invoice") or {}) if isinstance(raw2, dict) else {}
                        raw_state2 = inv2.get("state") or info2.get("state") or info2.get("status")
                        norm2 = _normalize_state(raw_state2)
                        logger.warning("[Verify] immediate-by-invoice retry state=%s norm=%s", raw_state2, norm2)
                        if norm2 == "succeeded":
                            _mark_tx_and_activate(tx, info2)
                            active, sub = is_active(user_id)
                            return {
                                "status": "activated",
                                "subscription_status": "active" if active else "inactive",
                                "current_period_end": sub.end_date.isoformat() + "Z" if (sub and sub.end_date) else None,
                                "attempts": attempt,
                                "debug": {"user_id": user_id},
                            }, 200
                        if norm2 == "failed":
                            tx.mark_failed(reason="verify: FAILED from IntaSend", provider_status=raw_state2)
                            return {"status": "failed", "attempts": attempt}, 200
                    except Exception as e:
                        logger.warning("[Verify] immediate recheck error: %s", e)

                if norm == "succeeded":
                    _mark_tx_and_activate(tx, info)
                    active, sub = is_active(user_id)
                    return {
                        "status": "activated",
                        "subscription_status": "active" if active else "inactive",
                        "current_period_end": sub.end_date.isoformat() + "Z" if (sub and sub.end_date) else None,
                        "attempts": attempt,
                        "debug": {"user_id": user_id},
                    }, 200

                if norm == "failed":
                    tx.mark_failed(reason="verify: FAILED from IntaSend", provider_status=raw_state)
                    return {"status": "failed", "attempts": attempt}, 200

                # else still pending → continue

            except Exception as e:
                logger.warning("Verify attempt %d error: %s", attempt, e)
                # continue; final fall-through returns pending

        return {"status": "pending", "attempts": len(delays), "debug": {"last_raw_state": last_raw_state}}, 200


# ---------------------------
# Webhook (optional but useful) — with invoice backfill
# ---------------------------
# routes/payments_routes.py (replace the existing class)
# ---------------------------
# Webhook (robust) — match TX by: checkout_id OR invoice_id OR TX<ID> fallback
# ---------------------------
class IntaSendWebhook(Resource):
    @cross_origin(**_CORS_ARGS)
    def options(self):
        return {}, 204

    @cross_origin(**_CORS_ARGS)
    def post(self):
        # 1) Parse + auth (shared secret)
        payload = request.get_json(silent=True) or {}
        logger.info("[Webhook] received payload: %s", payload)

        expected = (os.getenv("INTASEND_WEBHOOK_CHALLENGE") or "").strip()
        got = (payload.get("challenge") or "").strip()
        if not expected:
            logger.warning("[Webhook] INTASEND_WEBHOOK_CHALLENGE not set in env")
            return {"error": "server_misconfigured"}, 500
        if got != expected:
            logger.warning(
                "[Webhook] challenge mismatch expected=%s got=%s",
                (expected[:6] + "…") if expected else "",
                (got[:6] + "…") if got else "",
            )
            return {"error": "unauthorized"}, 401

        # 2) Extract identifiers from webhook
        checkout_id = payload.get("checkout_id") or payload.get("id")
        api_ref     = payload.get("api_ref") or payload.get("reference")  # IntaSend 'api_ref' you sent (e.g., "TX25")
        invoice_id  = payload.get("invoice_id") or (payload.get("invoice") or {}).get("invoice_id")
        raw_state   = payload.get("state") or (payload.get("invoice") or {}).get("state")
        norm        = _normalize_state(raw_state)

        logger.info(
            "[Webhook] ids checkout_id=%s api_ref=%s invoice_id=%s state(raw)=%s norm=%s",
            checkout_id, api_ref, invoice_id, raw_state, norm
        )

        # 3) Ask IntaSend (Bearer /payment/status) for authoritative state
        status_info = None
        try:
            client = get_intasend_client()
            status_info = client.check_payment_status(
                invoice_id=invoice_id or None,
                checkout_id=checkout_id or None,  # harmless if it's TXxx; endpoint will ignore/404 that
            )
        except Exception as e:
            logger.warning("[Webhook] status check failed: %s", e)
            status_info = {"state": raw_state}

        # 4) Find local TX — try multiple strategies in order
        tx = None

        # (a) direct match by stored checkout UUID (your DB currently uses this in PaymentTransaction.api_ref)
        if checkout_id:
            tx = PaymentTransaction.query.filter_by(api_ref=checkout_id).first()

        # (b) match by provider_ref (invoice_id) if present
        if not tx and invoice_id:
            tx = PaymentTransaction.query.filter_by(provider_ref=invoice_id).first()

        # (c) NEW: if api_ref looks like "TX<id>", map it to the PaymentTransaction primary key
        if not tx and api_ref:
            api_ref_str = str(api_ref)
            if api_ref_str.upper().startswith("TX"):
                try:
                    tx_id = int(api_ref_str[2:])
                    tx = PaymentTransaction.query.get(tx_id)
                    if tx:
                        logger.info("[Webhook] matched TX by TX<ID> fallback: id=%s", tx_id)
                except Exception as _:
                    pass  # ignore parse errors

        if not tx:
            logger.warning("[Webhook] no local TX for checkout=%s api_ref=%s invoice=%s",
                           checkout_id, api_ref, invoice_id)
            # Accept to avoid IntaSend retry storms; FE polling/verify can still fix it.
            return {"status": "accepted"}, 202

        # If already done, exit early
        if tx.status == "succeeded":
            logger.info("[Webhook] tx id=%s already succeeded", tx.id)
            return {"status": "ok"}, 200

        # 5) Backfill invoice_id (critical for future status checks)
        if status_info:
            _backfill_invoice_id_from_info(tx, status_info)
        elif invoice_id and not tx.provider_ref:
            tx.provider_ref = invoice_id
            db.session.commit()

        # 6) Decide final state
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
            tx.mark_failed(reason=str(payload)[:500])
            db.session.commit()
        else:
            tx.status = "pending"
            tx.updated_at = datetime.utcnow()
            db.session.commit()

        return {"status": "ok"}, 200


# routes/payments_routes.py  (add at bottom)
class DebugIntaSendStatus(Resource):
    @jwt_required()
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
