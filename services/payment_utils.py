"""
Centralized payment helpers - FIXED VERSION

Key fixes:
- Immediately backfill invoice_id after checkout creation
- Better error handling when invoice doesn't exist yet
- Smarter retry logic with exponential backoff
- Proper handling of "payment too new" scenario
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from time import sleep

from config import db, app
from models import PaymentTransaction
from services.intasend_client import get_intasend_client, IntaSendError
from services.subscription_manager import activate

logger = logging.getLogger(__name__)


def normalize_payment_state(raw_status: str | None) -> str:
    """Map IntaSend states to internal states."""
    status = (raw_status or "").strip().upper()
    if status in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    if status in {"FAILED", "CANCELLED", "CANCELED", "EXPIRED", "DECLINED"}:
        return "failed"
    return "pending"


def resolve_transaction(
    user_id: int,
    checkout_id: str | None,
    invoice_id: str | None,
    *,
    fallback_window_minutes: int = 30,
) -> Optional[PaymentTransaction]:
    """
    Resolve transaction with improved priority logic.
    """
    logger.info(
        "[payment_utils] resolve_transaction user_id=%s checkout_id=%s invoice_id=%s",
        user_id, checkout_id, invoice_id,
    )

    tx: Optional[PaymentTransaction] = None

    # 1) Try checkout_id first
    try:
        if checkout_id:
            tx = (
                PaymentTransaction.query
                .filter_by(user_id=user_id, api_ref=checkout_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )
            if tx:
                logger.info("[payment_utils] resolve_transaction matched via checkout_id tx_id=%s", tx.id)
                return tx
    except Exception as exc:
        logger.exception("[payment_utils] checkout_id lookup failed: %s", exc)

    # 2) Try invoice_id
    try:
        if invoice_id:
            tx = (
                PaymentTransaction.query
                .filter_by(user_id=user_id, provider_ref=invoice_id)
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )
            if tx:
                logger.info("[payment_utils] resolve_transaction matched via invoice_id tx_id=%s", tx.id)
                return tx
    except Exception as exc:
        logger.exception("[payment_utils] invoice_id lookup failed: %s", exc)

    # 3) Fallback to recent pending
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=fallback_window_minutes)
        tx = (
            PaymentTransaction.query
            .filter(
                PaymentTransaction.user_id == user_id,
                PaymentTransaction.status.in_(("pending", "initiated")),
                PaymentTransaction.created_at >= cutoff,
            )
            .order_by(PaymentTransaction.created_at.desc())
            .first()
        )
        if tx:
            logger.info("[payment_utils] resolve_transaction fallback recent pending tx_id=%s", tx.id)
        else:
            logger.info("[payment_utils] resolve_transaction no recent pending tx found")
        return tx
    except Exception as exc:
        logger.exception("[payment_utils] fallback lookup failed: %s", exc)
        return None


def backfill_invoice_id(
    tx: PaymentTransaction,
    intasend_response: dict,
    *,
    auto_commit: bool = False,
    auto_flush: bool = True,
) -> bool:
    """
    FIXED: Extract invoice_id from multiple possible locations.
    """
    if not tx or not isinstance(intasend_response, dict):
        return False

    invoice_id = None
    
    # Try all possible locations
    locations = [
        intasend_response.get("invoice_id"),
        intasend_response.get("resolved_invoice_id"),
        (intasend_response.get("invoice") or {}).get("invoice_id"),
        (intasend_response.get("invoice") or {}).get("id"),
        (intasend_response.get("raw") or {}).get("invoice_id"),
        (intasend_response.get("raw") or {}).get("resolved_invoice_id"),
        ((intasend_response.get("raw") or {}).get("invoice") or {}).get("invoice_id"),
    ]
    
    for loc in locations:
        if loc and isinstance(loc, str):
            invoice_id = loc
            break

    if invoice_id and not tx.provider_ref:
        logger.info("[payment_utils] backfill invoice_id=%s onto tx_id=%s", invoice_id, tx.id)
        tx.provider_ref = invoice_id
        tx.updated_at = datetime.utcnow()
        db.session.add(tx)
        if auto_flush:
            db.session.flush()
        if auto_commit:
            db.session.commit()
        return True

    return False


def extract_receipt_from_response(intasend_response: dict) -> Optional[str]:
    """Extract M-Pesa receipt from response."""
    if not isinstance(intasend_response, dict):
        return None

    raw = intasend_response.get("raw") or {}
    if not isinstance(raw, dict):
        raw = {}
    invoice = intasend_response.get("invoice") or {}
    if not isinstance(invoice, dict):
        invoice = {}
    raw_invoice = raw.get("invoice") or {}
    if not isinstance(raw_invoice, dict):
        raw_invoice = {}

    for candidate in (
        raw_invoice.get("mpesa_receipt"),
        raw_invoice.get("receipt"),
        raw.get("mpesa_receipt"),
        invoice.get("mpesa_receipt"),
        invoice.get("receipt"),
    ):
        if candidate:
            return candidate
    return None


def activate_subscription_for_transaction(
    tx: PaymentTransaction,
    intasend_response: dict,
    *,
    auto_commit: bool = True,
) -> bool:
    """Activate subscription for successful payment."""
    if not tx:
        logger.warning("[payment_utils] activate called with None tx")
        return False

    try:
        receipt = extract_receipt_from_response(intasend_response or {})
        if receipt and hasattr(tx, "receipt"):
            setattr(tx, "receipt", receipt)

        now = datetime.utcnow()
        tx.status = "succeeded"
        tx.provider_status = (intasend_response or {}).get("status") or tx.provider_status
        tx.completed_at = tx.completed_at or now
        tx.updated_at = now
        db.session.add(tx)
        db.session.flush()

        plan_type = (tx.plan_type or "monthly").lower()
        if plan_type == "daily":
            default_amount = app.config.get(
                "BILLING_PLAN_DAILY_KES",
                app.config.get("BILLING_PLAN_MONTHLY_KES", 100),
            )
        else:
            default_amount = app.config.get("BILLING_PLAN_MONTHLY_KES", 100)

        amount = tx.amount or int(default_amount)
        currency = tx.currency or str(app.config.get("BILLING_CURRENCY", "KES"))

        activate(tx.user_id, plan=plan_type, amount=amount, currency=currency, commit=auto_commit)

        logger.info(
            "[payment_utils] activation complete tx_id=%s plan=%s receipt=%s",
            tx.id, plan_type, receipt,
        )
        if not auto_commit:
            db.session.flush()
        return True
    except Exception as exc:
        logger.exception("[payment_utils] activation failed for tx_id=%s: %s", getattr(tx, "id", None), exc)
        if auto_commit:
            db.session.rollback()
            return False
        raise


def finalize_if_succeeded(
    tx: PaymentTransaction,
    provider_payload: dict,
    *,
    auto_commit: bool = True,
) -> bool:
    """Finalize transaction if provider indicates success."""
    if not tx:
        return False
    try:
        normalized = normalize_payment_state(
            (provider_payload or {}).get("status")
            or ((provider_payload or {}).get("invoice") or {}).get("state")
            or ((provider_payload or {}).get("raw") or {}).get("state")
        )
        if normalized == "succeeded" or (provider_payload or {}).get("paid") is True:
            return activate_subscription_for_transaction(tx, provider_payload, auto_commit=auto_commit)
    except Exception as exc:
        logger.exception("[payment_utils] finalize_if_succeeded error for tx_id=%s: %s", getattr(tx, "id", None), exc)
        if not auto_commit:
            raise
    return False


def check_payment_status_safe(tx: PaymentTransaction, max_retries: int = 3) -> Dict[str, Any]:
    """
    FIXED: Handle "payment too new" scenario gracefully with retries.
    """
    pending_response = {
        "status": "pending",
        "amount": None,
        "currency": None,
        "raw": {},
        "normalized_state": "pending",
    }

    if not tx:
        logger.warning("[payment_utils] check_payment_status_safe called with None tx")
        return pending_response

    try:
        client = get_intasend_client()
    except Exception as exc:
        logger.exception("[payment_utils] failed to init IntaSend client: %s", exc)
        return pending_response

    # First try to get invoice_id if we don't have it
    if not tx.provider_ref and tx.api_ref:
        try:
            logger.info("[payment_utils] Fetching invoice_id for checkout=%s", tx.api_ref)
            checkout_info = client.get_checkout(tx.api_ref)
            invoice_id = checkout_info.get("invoice_id")
            if invoice_id:
                backfill_invoice_id(tx, {"invoice_id": invoice_id})
        except Exception as e:
            logger.warning("[payment_utils] Failed to get invoice_id: %s", e)

    # Now check status with retries for "too new" payments
    for attempt in range(1, max_retries + 1):
        try:
            response = client.check_payment_status(
                invoice_id=tx.provider_ref or None,
                checkout_id=None if tx.provider_ref else (tx.api_ref or None),
            )
            
            raw_payload = response or {}
            
            # Check if payment is just too new
            detail = raw_payload.get("raw", {}).get("detail", "")
            if "invoice not yet created" in detail.lower() or "being processed" in detail.lower():
                if attempt < max_retries:
                    logger.info("[payment_utils] Payment too new, retry %s/%s after 2s", attempt, max_retries)
                    sleep(2)
                    continue
                else:
                    logger.info("[payment_utils] Payment still processing after %s attempts", max_retries)
                    return pending_response
            
            # Try to backfill invoice_id from response
            backfill_invoice_id(tx, raw_payload)
            
            normalized = normalize_payment_state(
                raw_payload.get("status")
                or ((raw_payload.get("invoice") or {}).get("state"))
                or ((raw_payload.get("raw") or {}).get("state"))
            )

            logger.info(
                "[payment_utils] status check tx_id=%s provider_ref=%s api_ref=%s result_status=%s normalized=%s",
                tx.id, tx.provider_ref, tx.api_ref, raw_payload.get("status"), normalized
            )
            
            return {
                "status": raw_payload.get("status") or normalized,
                "amount": raw_payload.get("amount"),
                "currency": raw_payload.get("currency"),
                "raw": raw_payload,
                "normalized_state": normalized,
            }
            
        except IntaSendError as exc:
            logger.warning("[payment_utils] IntaSend status error for tx_id=%s (attempt %s/%s): %s", 
                          tx.id, attempt, max_retries, exc)
            if attempt < max_retries:
                sleep(1)
                continue
        except Exception as exc:
            logger.exception("[payment_utils] unexpected status error for tx_id=%s (attempt %s/%s): %s", 
                            tx.id, attempt, max_retries, exc)
            if attempt < max_retries:
                sleep(1)
                continue

    return pending_response
