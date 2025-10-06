# services/payment_reconciliation.py
"""Payment reconciliation service for quick status checks."""

import logging
from typing import Optional
from flask import current_app

from config import db
from models import PaymentTransaction
from services.intasend_client import get_intasend_client, IntaSendError
from services.subscription_manager import activate

logger = logging.getLogger(__name__)


def normalize_intasend_state(raw_state: Optional[str]) -> str:
    """
    Normalize IntaSend payment state to internal status.
    
    Args:
        raw_state: Raw state from IntaSend API
        
    Returns:
        Normalized state: 'succeeded', 'failed', or 'pending'
    """
    if not raw_state:
        return "pending"
    
    state = raw_state.strip().upper()
    
    if state in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    
    if state in {"FAILED", "CANCELLED", "CANCELED", "DECLINED", "EXPIRED"}:
        return "failed"
    
    return "pending"


def get_latest_pending_transaction(user_id: int) -> Optional[PaymentTransaction]:
    """
    Get the most recent pending transaction for a user.
    
    Args:
        user_id: User ID to check
        
    Returns:
        PaymentTransaction or None
    """
    return (
        PaymentTransaction.query
        .filter_by(user_id=user_id, status="pending")
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )


def quick_reconcile_payment(user_id: int) -> bool:
    """
    Quickly check and reconcile latest pending payment for user.
    
    This helps avoid stale 402 errors when a user has just paid
    but the webhook hasn't been processed yet.
    
    Args:
        user_id: User ID to reconcile
        
    Returns:
        True if payment was reconciled to 'succeeded', False otherwise
    """
    tx = get_latest_pending_transaction(user_id)
    
    if not tx:
        logger.debug(f"No pending transactions for user {user_id}")
        return False
    
    # Get IntaSend client
    try:
        client = get_intasend_client()
    except Exception as e:
        logger.error(f"Failed to get IntaSend client: {e}")
        return False
    
    # Check payment status
    try:
        status_info = client.check_payment_status(
            invoice_id=tx.provider_ref or None,
            checkout_id=tx.api_ref or None,
        )
    except IntaSendError as e:
        logger.warning(
            f"IntaSend status check failed for tx {tx.id}: {e}",
            extra={"user_id": user_id, "tx_id": tx.id}
        )
        return False
    except Exception as e:
        logger.exception(f"Unexpected error checking payment status: {e}")
        return False
    
    # Extract state from response
    invoice_data = status_info.get("invoice") or {}
    raw_state = (
        invoice_data.get("state") or
        status_info.get("state") or
        status_info.get("status")
    )
    
    normalized = normalize_intasend_state(raw_state)
    
    # Update transaction
    tx.provider_status = raw_state or tx.provider_status
    
    if normalized == "succeeded":
        # Mark as succeeded
        receipt = invoice_data.get("mpesa_receipt") or invoice_data.get("receipt")
        tx.mark_succeeded(provider_ref=receipt, provider_status=raw_state)
        
        # Activate subscription
        amount = tx.amount or int(
            current_app.config.get("BILLING_PLAN_MONTHLY_KES", 100)
        )
        currency = tx.currency or str(
            current_app.config.get("BILLING_CURRENCY", "KES")
        )
        
        activate(user_id, plan="monthly", amount=amount, currency=currency)
        
        logger.info(
            f"Payment reconciled and user activated",
            extra={
                "user_id": user_id,
                "tx_id": tx.id,
                "amount": amount,
                "currency": currency
            }
        )
        
        try:
            db.session.commit()
        except Exception as e:
            logger.error(f"Failed to commit reconciliation: {e}")
            db.session.rollback()
            return False
        
        return True
    
    elif normalized == "failed":
        tx.mark_failed(
            reason="quick_reconcile: payment failed",
            provider_status=raw_state
        )
        
        try:
            db.session.commit()
        except Exception as e:
            logger.error(f"Failed to commit failed status: {e}")
            db.session.rollback()
        
        logger.info(
            f"Payment marked as failed during reconciliation",
            extra={"user_id": user_id, "tx_id": tx.id}
        )
        return False
    
    else:
        # Still pending
        tx.mark_pending(provider_status=raw_state)
        
        try:
            db.session.commit()
        except Exception as e:
            logger.error(f"Failed to commit pending status: {e}")
            db.session.rollback()
        
        logger.debug(
            f"Payment still pending",
            extra={"user_id": user_id, "tx_id": tx.id}
        )
        return False