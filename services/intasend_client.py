# services/intasend_client.py — INTASEND-DOCS-ALIGNED with deep logging (DROP-IN)
from __future__ import annotations

import os
import logging
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, Any
from time import sleep

import requests

try:
    from intasend import APIService
except Exception as _e:
    raise RuntimeError(
        "The 'intasend' package is required. Install with: pip install intasend-python"
    ) from _e

logger = logging.getLogger(__name__)


class IntaSendError(RuntimeError):
    pass


# ----------------------------- helpers -----------------------------

def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise IntaSendError(f"Missing environment variable: {name}")
    return val


def _normalize_phone(phone: str | None) -> str | None:
    """Return MSISDN in 2547XXXXXXXX format (Kenya)."""
    if not phone:
        return None
    p = phone.strip().replace(" ", "").replace("-", "")
    if not p:
        return None
    if p.startswith("+"):
        p = p[1:]
    if p.startswith("0"):
        p = "254" + p[1:]
    elif not p.startswith("254"):
        p = "254" + p
    if not (p.isdigit() and 11 <= len(p) <= 12):
        logger.warning("Phone looks odd after normalization: %s", p)
    return p


def _to_amount(amount: float | str | Decimal) -> Decimal:
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"))
        if d <= 0:
            raise IntaSendError("Amount must be > 0")
        return d
    except (InvalidOperation, TypeError):
        raise IntaSendError("Invalid amount value")


def status_normalize(status: str | None) -> str:
    """
    Normalize IntaSend state into 'succeeded' | 'failed' | 'pending'.
    Per docs: PENDING, PROCESSING, COMPLETE, FAILED
    """
    s = (status or "").strip().upper()
    if s in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    if s in {"FAILED", "CANCELLED", "CANCELED", "EXPIRED", "DECLINED"}:
        return "failed"
    # Default to pending (covers PENDING/PROCESSING/unknown)
    return "pending"


def _mask_id(v: Optional[str]) -> Optional[str]:
    """Mask long IDs while keeping them recognizable in logs."""
    if not v or not isinstance(v, str):
        return v
    if len(v) <= 8:
        return v
    return f"{v[:4]}…{v[-4:]}"


# ----------------------------- client -----------------------------

class IntaSendClient:
    """Thin wrapper around IntaSend SDK with HTTP fallbacks where needed."""

    def __init__(self):
        self.public_key = _require_env("INTASEND_PUBLIC_KEY")
        self.secret_key = _require_env("INTASEND_SECRET_KEY")
        self.test_mode = os.getenv("INTASEND_TEST_MODE", "true").lower() in {"1", "true", "yes", "on"}

        # API base; sandbox when test_mode is true
        self.base_url = os.getenv(
            "INTASEND_API_BASE",
            "https://sandbox.intasend.com" if self.test_mode else "https://api.intasend.com",
        ).rstrip("/")

        # Official SDK
        self.service = APIService(
            token=self.secret_key,
            publishable_key=self.public_key,
            test=self.test_mode,
        )

        # One-time environment snapshot
        logger.warning(
            "[IS] client init test_mode=%s base_url=%s pub_key=%s",
            self.test_mode,
            self.base_url,
            _mask_id(self.public_key),
        )

    # -------------------- Hosted Checkout --------------------

    def create_checkout(
        self,
        amount: float | str | Decimal,
        email: Optional[str] = None,
        phone_number: Optional[str] = None,
        currency: str = "KES",
        api_ref: Optional[str] = None,       # your unique reference (e.g., "TX123")
        redirect_url: Optional[str] = None,  # hosted page return URL
        narrative: str = "Flashlearn Premium",
    ) -> Dict[str, Any]:
        """
        Create a hosted checkout using the SDK.
        Returns: { invoice_id, checkout_url, checkout_id, raw }
        """
        amt = _to_amount(amount)
        payload: Dict[str, Any] = {
            "amount": float(amt),
            "currency": currency,
            # SDK README shows 'comment' param; send our narrative there.
            "comment": narrative,
        }
        if email:
            payload["email"] = email
        msisdn = _normalize_phone(phone_number)
        if msisdn:
            payload["phone_number"] = msisdn
        if api_ref:
            payload["api_ref"] = api_ref
        if redirect_url:
            payload["redirect_url"] = redirect_url

        try:
            resp = self.service.collect.checkout(**payload)
            logger.info("[IS] checkout created resp=%s", resp)
        except Exception as e:
            logger.exception("[IS] checkout creation failed")
            raise IntaSendError(f"Checkout creation failed: {e}") from e

        # Common response fields across SDK / HTTP
        invoice_id   = resp.get("invoice_id") or (resp.get("invoice") or {}).get("invoice_id")
        checkout_url = resp.get("url") or resp.get("checkout_url")
        checkout_id  = resp.get("checkout_id") or resp.get("id") or resp.get("api_ref")
        logger.warning("[IS] checkout_url=%s checkout_id=%s", checkout_url, _mask_id(checkout_id))

        if not checkout_url or not checkout_id:
            raise IntaSendError(f"Invalid checkout response (missing url/id): {resp}")

        return {
            "invoice_id": invoice_id,      # may be None initially
            "checkout_url": checkout_url,  # open on the client
            "checkout_id": checkout_id,    # persist to PaymentTransaction.api_ref
            "raw": resp,
        }

    # -------------------- Status (invoice_id or checkout_id) --------------------

    def check_payment_status(
        self,
        invoice_id: Optional[str] = None,
        checkout_id: Optional[str] = None,
        retries: int = 1,
        backoff: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Query payment status using EITHER invoice_id OR checkout_id.
        Preferred: SDK when invoice_id is available; otherwise HTTP endpoint
        /api/v1/payment/status/ which supports either identifier.
        Returns: { status, amount, currency, raw }
        Never raises for normal "not ready" cases – will return PENDING.
        """
        if not invoice_id and not checkout_id:
            raise IntaSendError("check_payment_status requires invoice_id or checkout_id")

        # 1) Prefer SDK when invoice_id is present (matches official examples)
        if invoice_id:
            try:
                logger.warning("[IS] SDK status start invoice_id=%s", _mask_id(invoice_id))
                resp = self.service.collect.status(invoice_id=invoice_id)
                logger.warning("[IS] SDK status resp=%s", str(resp)[:800])
                # SDK typically returns {'invoice': {... 'state': 'PENDING|COMPLETE|FAILED', ...}}
                invoice = resp.get("invoice") or {}
                state = invoice.get("state") or resp.get("state") or resp.get("status") or "PENDING"
                amount = invoice.get("net_amount") or invoice.get("value") or invoice.get("amount")
                currency = invoice.get("currency") or resp.get("currency") or "KES"
                return {
                    "status": state,
                    "amount": amount,
                    "currency": currency,
                    "raw": resp,
                }
            except Exception as e:
                logger.warning("[IS] SDK status failed invoice_id=%s err=%s", _mask_id(invoice_id), e)
                # fall through to HTTP as a backup

        # 2) HTTP fallback — official status endpoint accepts invoice_id or checkout_id (Bearer)
        url = f"{self.base_url}/api/v1/payment/status/"
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {}
        if invoice_id:
            payload["invoice_id"] = invoice_id
        if checkout_id:
            payload["checkout_id"] = checkout_id

        masked_payload = {
            "invoice_id": _mask_id(invoice_id) if invoice_id else None,
            "checkout_id": _mask_id(checkout_id) if checkout_id else None,
        }

        for attempt in range(retries + 1):
            try:
                logger.warning("[IS] status POST %s payload=%s attempt=%s", url, masked_payload, attempt + 1)
                r = requests.post(url, json=payload, headers=headers, timeout=15)
                body_preview = (r.text or "")[:800]
                logger.warning("[IS] status http=%s body=%s", r.status_code, body_preview)

                if r.status_code >= 500:
                    raise IntaSendError(f"remote {r.status_code}: {body_preview}")

                # Try JSON parse; if it fails, return raw text
                try:
                    raw = r.json()
                except Exception:
                    return {"status": "PENDING", "amount": None, "currency": None, "raw": {"text": r.text}}

                invoice = raw.get("invoice") or raw
                state = invoice.get("state") or raw.get("state") or raw.get("status") or "PENDING"
                amount = invoice.get("net_amount") or invoice.get("value") or invoice.get("amount")
                currency = invoice.get("currency") or raw.get("currency") or "KES"

                # If we only had checkout_id and the response contains invoice_id, surface it
                if not invoice_id:
                    found_invoice = invoice.get("invoice_id") or raw.get("invoice_id")
                    if found_invoice:
                        raw["resolved_invoice_id"] = found_invoice

                return {"status": state, "amount": amount, "currency": currency, "raw": raw}

            except Exception as e:
                logger.warning("[IS] status error attempt=%s err=%s", attempt + 1, e)
                if attempt < retries:
                    sleep(backoff)
                else:
                    return {"status": "PENDING", "amount": None, "currency": None, "raw": {"error": str(e)}}

    # -------------------- Checkout details (helper via *status only*) --------------------

    def get_checkout(self, checkout_id: str) -> Dict[str, Any]:
        """
        Return details for a checkout using ONLY the /payment/status endpoint
        (Bearer auth). We DO NOT call /checkout/details from the backend.
        """
        if not checkout_id:
            raise IntaSendError("checkout_id required")

        info = self.check_payment_status(checkout_id=checkout_id)
        raw = info.get("raw") or {}
        invoice = raw.get("invoice") or raw if isinstance(raw, dict) else {}
        status = (invoice or {}).get("state") or info.get("status") or "PENDING"
        invoice_id = (invoice or {}).get("invoice_id") or raw.get("resolved_invoice_id")
        return {"status": status, "invoice_id": invoice_id, "raw": raw}

    # -------------------- STK Push (optional) --------------------

    def create_payment_request(
        self,
        amount: float | str | Decimal,
        phone_number: str,
        email: str,
        narrative: str = "Flashlearn Premium",
    ) -> Dict[str, Any]:
        """
        Initiate M-Pesa STK push via SDK.
        Returns: { invoice_id, request_id, raw }
        """
        amt = _to_amount(amount)
        msisdn = _normalize_phone(phone_number)

        payload = {
            "phone_number": msisdn,
            "email": email,
            "amount": float(amt),
            "narrative": narrative,  # SDK supports this per examples
        }

        try:
            resp = self.service.collect.mpesa_stk_push(**payload)
            logger.info("[IS] STK Push initiated resp=%s", resp)
        except Exception as e:
            logger.exception("[IS] STK push failed")
            raise IntaSendError(f"STK push failed: {e}") from e

        invoice = resp.get("invoice") or {}
        invoice_id = invoice.get("invoice_id") or resp.get("invoice_id") or resp.get("id")
        if not invoice_id:
            raise IntaSendError(f"Could not find invoice_id in response: {resp}")

        return {
            "invoice_id": invoice_id,
            "request_id": resp.get("tracking_id") or resp.get("request_id"),
            "raw": resp,
        }

    # -------------------- Webhooks --------------------

    def verify_webhook_signature(self, payload: bytes | str, signature: str) -> bool:
        """
        Placeholder – IntaSend currently emphasizes re-querying by invoice_id.
        Always treat the webhook as a hint and confirm via check_payment_status().
        """
        return True


# ----------------------------- singleton -----------------------------

_client: Optional[IntaSendClient] = None


def get_intasend_client() -> IntaSendClient:
    global _client
    if _client is None:
        _client = IntaSendClient()
    return _client
