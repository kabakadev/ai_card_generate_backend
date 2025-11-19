# services/intasend_webhook_processor.py
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import OperationalError, SQLAlchemyError, InvalidRequestError

from config import db
from models import PaymentTransaction
from services.payment_utils import backfill_invoice_id, normalize_payment_state
from services.subscription_manager import activate as activate_subscription

logger = logging.getLogger(__name__)


def _derive_provider_state(raw_state: str | None, payload: dict | None) -> str | None:
    payload = payload or {}
    invoice_block = payload.get("invoice") or {}
    if raw_state:
        return raw_state
    if invoice_block.get("state"):
        return invoice_block.get("state")
    return payload.get("status")


def _normalize_state(raw_state: str | None, paid_flag: bool) -> str:
    normalized = normalize_payment_state(raw_state)
    if paid_flag and normalized != "failed":
        return "succeeded"
    return normalized


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

    Transaction handling:
      - Prefer a NESTED transaction (SAVEPOINT) so this works both inline (inside a
        request transaction) and in a background worker.
      - If the dialect/version rejects nested, fall back to a normal transaction.
    """
    payload = payload or {}

    # Choose a transaction context that works across SQLAlchemy/Flask-SQLAlchemy versions.
    # Prefer nested; fallback to normal.
    try:
        ctx = db.session.begin_nested()
        tx_mode = "NESTED"
    except (InvalidRequestError, AttributeError, TypeError):
        ctx = db.session.begin()
        tx_mode = "NORMAL"

    logger.info("[IntaSendJob] tx_id=%s begin %s transaction", tx_id, tx_mode)

    outcome = "noop"
    try:
        with ctx:
            # Lock the row; requires being in a transaction.
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
                    if checkout_id and not locked_tx.api_ref:
                        locked_tx.api_ref = checkout_id

                    provider_payload = payload or {}
                    provider_state = _derive_provider_state(raw_state, provider_payload)
                    normalized_state = _normalize_state(provider_state, paid_flag)

                    if invoice_id:
                        backfill_invoice_id(locked_tx, {"invoice_id": invoice_id}, auto_commit=False)
                    backfill_invoice_id(locked_tx, provider_payload, auto_commit=False)

                    locked_tx.provider_status = provider_state or locked_tx.provider_status
                    now = datetime.utcnow()

                    logger.info(
                        "[IntaSendWebhook] tx=%s normalized=%s provider_state=%s user_id=%s plan_type=%s",
                        locked_tx.id,
                        normalized_state,
                        provider_state,
                        locked_tx.user_id,
                        locked_tx.plan_type,
                    )

                    if normalized_state == "succeeded":
                        if locked_tx.status != "succeeded":
                            locked_tx.status = "succeeded"
                            locked_tx.completed_at = locked_tx.completed_at or now
                            locked_tx.failure_reason = None
                            plan_type = (locked_tx.plan_type or "monthly").lower()
                            sub = activate_subscription(
                                locked_tx.user_id,
                                plan=plan_type,
                                amount=locked_tx.amount,
                                currency=locked_tx.currency,
                                commit=False,
                            )
                            logger.info(
                                "[IntaSendWebhook] Activated subscription: sub_id=%s start=%s end=%s",
                                getattr(sub, "id", None),
                                getattr(sub, "start_date", None),
                                getattr(sub, "end_date", None),
                            )
                        outcome = "succeeded"

                    elif normalized_state == "failed":
                        if locked_tx.status != "succeeded":
                            locked_tx.status = "failed"
                            locked_tx.failure_reason = "webhook: FAILED"
                            locked_tx.updated_at = now
                        outcome = "failed"

                    else:
                        locked_tx.status = "pending"
                        locked_tx.updated_at = now
                        outcome = "pending"

                    db.session.add(locked_tx)

        # Commit/flush depending on transaction mode
        if tx_mode == "NESTED":
            # Nested block released the SAVEPOINT; outer scope manages final commit.
            db.session.flush()
            logger.info("[IntaSendJob] tx_id=%s nested transaction flushed (outcome=%s)", tx_id, outcome)
        else:
            db.session.commit()
            logger.info("[IntaSendJob] tx_id=%s committed (outcome=%s)", tx_id, outcome)

    except OperationalError as exc:
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
