# services/intasend_webhook_processor.py
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import OperationalError, SQLAlchemyError

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
    """
    Process an IntaSend webhook delivery and update payment/subscription state.

    - Uses a normal transaction when called from a worker (no active tx).
    - Uses a NESTED transaction (SAVEPOINT) when called inline during a request, to
      avoid 'A transaction is already begun on this Session' errors.
    """
    payload = payload or {}

    # Decide transaction strategy up front
    already_in_tx = db.session.in_transaction()
    ctx = db.session.begin_nested() if already_in_tx else db.session.begin()
    logger.info(
        "[IntaSendJob] tx_id=%s begin %s transaction (in_tx=%s)",
        tx_id,
        "NESTED" if already_in_tx else "NORMAL",
        already_in_tx,
    )

    # We'll avoid 'return' inside the context so the context manager can exit cleanly.
    outcome = "noop"
    try:
        with ctx:
            # Lock the row; this requires being inside a tx.
            locked_tx = (
                PaymentTransaction.query
                .filter_by(id=tx_id)
                .with_for_update()
                .one_or_none()
            )

            if not locked_tx:
                logger.warning("[IntaSendJob] transaction not found tx_id=%s", tx_id)
                outcome = "not_found"
            else:
                if locked_tx.status == "succeeded":
                    logger.info("[IntaSendJob] tx_id=%s already succeeded", locked_tx.id)
                    outcome = "already_succeeded"
                else:
                    # Backfill invoice_id if webhook supplied one
                    if invoice_id and not locked_tx.provider_ref:
                        backfill_invoice_id(locked_tx, {"invoice_id": invoice_id}, auto_commit=False)

                    # Poll provider status (safe; handles retries)
                    status_info = check_payment_status_safe(locked_tx, max_retries=2)
                    raw_provider_payload = status_info.get("raw") or {}

                    # Backfill invoice_id from provider payload if present
                    backfill_invoice_id(locked_tx, raw_provider_payload, auto_commit=False)

                    # Normalize final state: use provider-derived state first, then webhook's raw_state
                    decided = status_info.get("normalized_state") or normalize_payment_state(raw_state)
                    provider_state = status_info.get("status") or raw_state

                    # If webhook explicitly said 'paid' but normalize says pending, prefer paid
                    if paid_flag and decided == "pending":
                        logger.info("[IntaSendJob] paid flag overrides pending for tx_id=%s", locked_tx.id)
                        decided = "succeeded"

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
                        outcome = "succeeded"
                    elif decided == "failed":
                        locked_tx.status = "failed"
                        locked_tx.failure_reason = "webhook: FAILED"
                        locked_tx.provider_status = provider_state or locked_tx.provider_status
                        locked_tx.updated_at = datetime.utcnow()
                        db.session.add(locked_tx)
                        outcome = "failed"
                    else:
                        # Treat everything else as in-flight
                        locked_tx.status = "pending"
                        locked_tx.provider_status = provider_state or locked_tx.provider_status
                        locked_tx.updated_at = datetime.utcnow()
                        db.session.add(locked_tx)
                        outcome = "pending"

        # Commit strategy:
        # - NORMAL tx: commit for real.
        # - NESTED tx: the nested block has released the SAVEPOINT; we only need to flush.
        if already_in_tx:
            db.session.flush()
            logger.info("[IntaSendJob] tx_id=%s nested transaction flushed (outcome=%s)", tx_id, outcome)
        else:
            db.session.commit()
            logger.info("[IntaSendJob] tx_id=%s committed (outcome=%s)", tx_id, outcome)

    except OperationalError as exc:
        # DB connectivity/lock issues
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.exception("[IntaSendJob] database error tx_id=%s: %s", tx_id, exc)
        raise
    except SQLAlchemyError as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.exception("[IntaSendJob] SQLAlchemy error tx_id=%s: %s", tx_id, exc)
        raise
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.exception("[IntaSendJob] unexpected failure tx_id=%s", tx_id)
        raise
