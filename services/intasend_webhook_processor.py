# services/intasend_webhook_processor.py
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import OperationalError

from config import db
from models import PaymentTransaction
from services.payment_utils import (
    backfill_invoice_id,
    check_payment_status_safe,
    finalize_if_succeeded,
    normalize_payment_state,
)

logger = logging.getLogger(__name__)


def process_intasend_webhook(
    *,
    tx_id: int,
    checkout_id: str | None,
    invoice_id: str | None,
    raw_state: str | None,
    paid_flag: bool,
    payload: dict | None,
) -> None:
    payload = payload or {}

    try:
        with db.session.begin():
            locked_tx = (
                PaymentTransaction.query
                .filter_by(id=tx_id)
                .with_for_update()
                .one_or_none()
            )

            if not locked_tx:
                logger.warning("[IntaSendJob] transaction not found tx_id=%s", tx_id)
                return

            if locked_tx.status == "succeeded":
                logger.info("[IntaSendJob] tx_id=%s already succeeded", locked_tx.id)
                return

            if invoice_id and not locked_tx.provider_ref:
                backfill_invoice_id(locked_tx, {"invoice_id": invoice_id}, auto_commit=False)

            status_info = check_payment_status_safe(locked_tx, max_retries=2)
            raw_provider_payload = status_info.get("raw") or {}

            backfill_invoice_id(locked_tx, raw_provider_payload, auto_commit=False)

            decided = status_info.get("normalized_state") or normalize_payment_state(raw_state)

            if paid_flag and decided == "pending":
                logger.info("[IntaSendJob] paid flag overrides pending for tx_id=%s", locked_tx.id)
                decided = "succeeded"

            provider_state = status_info.get("status") or raw_state

            logger.info(
                "[IntaSendJob] tx=%s decided=%s provider_state=%s",
                locked_tx.id,
                decided,
                provider_state,
            )

            if decided == "succeeded":
                activated = finalize_if_succeeded(locked_tx, status_info, auto_commit=False)
                if not activated:
                    raise RuntimeError(f"Activation helper returned False for tx_id={locked_tx.id}")
                return

            if decided == "failed":
                locked_tx.status = "failed"
                locked_tx.failure_reason = "webhook: FAILED"
                locked_tx.provider_status = provider_state or locked_tx.provider_status
                locked_tx.updated_at = datetime.utcnow()
                db.session.add(locked_tx)
                return

            locked_tx.status = "pending"
            locked_tx.provider_status = provider_state or locked_tx.provider_status
            locked_tx.updated_at = datetime.utcnow()
            db.session.add(locked_tx)

    except OperationalError as exc:
        logger.exception("[IntaSendJob] database error tx_id=%s: %s", tx_id, exc)
        raise
    except Exception:
        logger.exception("[IntaSendJob] unexpected failure tx_id=%s", tx_id)
        raise
