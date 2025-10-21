"""Payment reconciliation service - FIXED VERSION"""

from __future__ import annotations

import logging
from datetime import datetime

from config import db
from services.payment_utils import (
    normalize_payment_state,
    resolve_transaction,
    check_payment_status_safe,
    activate_subscription_for_transaction,
    backfill_invoice_id,
)

logger = logging.getLogger(__name__)


def quick_reconcile_payment(user_id: int) -> bool:
    """
    FIXED: Quick reconciliation with better handling of "payment too new" scenario.
    
    Returns:
        True if we activated a subscription; False otherwise.
    """
    tx = resolve_transaction(user_id, checkout_id=None, invoice_id=None)
    if not tx:
        logger.debug("quick_reconcile: no pending transactions for user_id=%s", user_id)
        return False

    # Don't reconcile if transaction is brand new (< 3 seconds old)
    if tx.created_at:
        age = (datetime.utcnow() - tx.created_at).total_seconds()
        if age < 3:
            logger.debug("quick_reconcile: tx_id=%s too new (%ss), skipping", tx.id, age)
            return False

    # Query provider with retries for new payments
    status_info = check_payment_status_safe(tx, max_retries=2)
    raw_payload = status_info.get("raw") or {}
    
    # Opportunistically backfill invoice_id
    try:
        backfilled = backfill_invoice_id(tx, raw_payload)
        if backfilled:
            logger.info("quick_reconcile: backfilled invoice_id for tx_id=%s", tx.id)
    except Exception:
        logger.exception("quick_reconcile: backfill_invoice_id failed for tx_id=%s", tx.id)

    normalized = status_info.get("normalized_state") or normalize_payment_state(None)
    raw_state = (
        (raw_payload.get("invoice") or {}).get("state")
        or raw_payload.get("state")
        or status_info.get("status")
    ) or "PENDING"

    # Persist provider status
    try:
        tx.provider_status = raw_state or tx.provider_status
        tx.updated_at = datetime.utcnow()
        db.session.add(tx)
        db.session.commit()
    except Exception:
        logger.exception("quick_reconcile: failed to persist provider_status for tx_id=%s", tx.id)
        db.session.rollback()

    if normalized == "succeeded":
        success = activate_subscription_for_transaction(tx, raw_payload)
        if success:
            logger.info("quick_reconcile: activated subscription tx_id=%s user_id=%s", tx.id, user_id)
        else:
            logger.warning("quick_reconcile: activation reported false for tx_id=%s user_id=%s", tx.id, user_id)
        return success

    if normalized == "failed":
        try:
            tx.status = "failed"
            tx.failure_reason = "quick_reconcile: payment failed"
            tx.provider_status = raw_state or tx.provider_status
            tx.updated_at = datetime.utcnow()
            db.session.add(tx)
            db.session.commit()
        except Exception:
            logger.exception("quick_reconcile: commit failed when marking failed for tx_id=%s", tx.id)
            db.session.rollback()
        logger.info("quick_reconcile: payment marked failed user_id=%s tx_id=%s", user_id, tx.id)
        return False

    # Still pending
    logger.debug("quick_reconcile: still pending user_id=%s tx_id=%s state=%s", user_id, tx.id, raw_state)
    return False