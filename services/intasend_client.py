# services/intasend_client.py — FIXED VERSION
from __future__ import annotations

import os
import logging
import platform
from decimal import Decimal, InvalidOperation
from time import sleep
from typing import Dict, Optional, Any

import requests

logger = logging.getLogger(__name__)


class IntaSendError(RuntimeError):
    pass


def _mask(v: Optional[str]) -> Optional[str]:
    if not v or not isinstance(v, str):
        return v
    return v if len(v) <= 8 else f"{v[:4]}…{v[-4:]}"


def _require_env(name: str, *, fallback: str | None = None) -> str:
    val = os.getenv(name) or (os.getenv(fallback) if fallback else None)
    if not val:
        raise IntaSendError(f"Missing environment variable: {name}")
    return val


def _normalize_phone(phone: Optional[str]) -> Optional[str]:
    """Normalize to KE MSISDN 2547XXXXXXXX."""
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
        logger.warning("[IS] phone normalized to suspicious MSISDN=%s", p)
    return p


def _to_amount(amount: float | str | Decimal) -> Decimal:
    try:
        d = Decimal(str(amount))
        if d <= 0:
            raise IntaSendError("Amount must be > 0")
        return d.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        raise IntaSendError("Invalid amount value")


class IntaSendClient:
    """Thin wrapper around IntaSend SDK + HTTP."""

    def __init__(self) -> None:
        self.public_key = _require_env("INTASEND_PUBLIC_KEY")
        self.secret_key = _require_env("INTASEND_SECRET_KEY", fallback="INTASED_SECRET_KEY")

        mode = (os.getenv("INTASEND_MODE") or "").strip().lower()
        legacy = (os.getenv("INTASEND_TEST") or os.getenv("INTASEND_TEST_MODE") or "").strip().lower()
        self.test_mode = (mode == "test") or (mode == "" and legacy in {"1", "true", "yes", "on"})

        default_base = "https://sandbox.intasend.com" if self.test_mode else "https://payment.intasend.com"
        self.base_url = (os.getenv("INTASEND_API_BASE") or default_base).rstrip("/")

        self._ua = f"FlashLearn-Server/1 (Python {platform.python_version()}; {platform.system()})"
        self._auth_headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self._ua,
            "INTASEND_PUBLIC_API_KEY": self.public_key,
        }
        self._public_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self._ua,
            "INTASEND_PUBLIC_API_KEY": self.public_key,
        }

        self._sdk = None
        try:
            from intasend import APIService
            self._sdk = APIService(
                token=self.secret_key,
                publishable_key=self.public_key,
                test=self.test_mode,
            )
        except Exception as e:
            logger.warning("[IS] SDK unavailable: %s", e)

        logger.warning("[IS] client init test_mode=%s base_url=%s pub=%s", 
                      self.test_mode, self.base_url, _mask(self.public_key))

    def create_checkout(
        self,
        amount: float | str | Decimal,
        *,
        email: Optional[str] = None,
        phone_number: Optional[str] = None,
        currency: str = "KES",
        api_ref: Optional[str] = None,
        redirect_url: Optional[str] = None,
        narrative: str = "FlashLearn Premium",
    ) -> Dict[str, Any]:
        """
        Create hosted checkout and IMMEDIATELY extract invoice_id.
        """
        amt = _to_amount(amount)
        payload: Dict[str, Any] = {
            "amount": float(amt),
            "currency": currency,
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

        if self._sdk is not None:
            try:
                resp = self._sdk.collect.checkout(**payload)
                logger.info("[IS] checkout created (SDK) resp=%s", str(resp)[:800])
            except Exception as e:
                logger.exception("[IS] checkout creation failed via SDK")
                raise IntaSendError(f"Checkout creation failed: {e}") from e
        else:
            url = f"{self.base_url}/api/v1/checkout/"
            try:
                payload_with_key = dict(payload, public_key=self.public_key)
                r = requests.post(url, json=payload_with_key, headers=self._public_headers, timeout=20)
                r.raise_for_status()
                resp = r.json()
                logger.info("[IS] checkout created (HTTP) resp=%s", str(resp)[:800])
            except Exception as e:
                logger.exception("[IS] checkout creation failed via HTTP")
                raise IntaSendError(f"Checkout creation failed: {e}") from e

        # CRITICAL FIX: Extract invoice_id from multiple possible locations
        invoice_id = None
        if isinstance(resp, dict):
            # Direct field
            invoice_id = resp.get("invoice_id")
            # Nested in invoice object
            if not invoice_id and "invoice" in resp:
                invoice_obj = resp["invoice"]
                if isinstance(invoice_obj, dict):
                    invoice_id = invoice_obj.get("invoice_id") or invoice_obj.get("id")
            # Sometimes in id field if invoice structure
            if not invoice_id and resp.get("id") and "INV" in str(resp.get("id", "")).upper():
                invoice_id = resp.get("id")

        checkout_url = resp.get("url") or resp.get("checkout_url")
        checkout_id = resp.get("checkout_id") or resp.get("id") or resp.get("api_ref")

        logger.warning("[IS] checkout_url=%s checkout_id=%s invoice_id=%s", 
                      checkout_url, _mask(checkout_id), _mask(invoice_id))
        
        if not checkout_url or not checkout_id:
            raise IntaSendError(f"Invalid checkout response (missing url/id): {resp}")

        return {
            "invoice_id": invoice_id,  # May be None initially
            "checkout_url": checkout_url,
            "checkout_id": checkout_id,
            "raw": resp,
        }

    def check_payment_status(
        self,
        *,
        invoice_id: Optional[str] = None,
        checkout_id: Optional[str] = None,
        retries: int = 2,
        backoff: float = 0.5,
    ) -> Dict[str, Any]:
        """
        FIXED: Properly handle checkout_id -> invoice_id resolution.
        """
        if not invoice_id and not checkout_id:
            raise IntaSendError("check_payment_status requires invoice_id or checkout_id")

        # If we have invoice_id, use it directly via SDK
        if invoice_id and self._sdk is not None:
            try:
                logger.warning("[IS] SDK status invoice_id=%s", _mask(invoice_id))
                resp = self._sdk.collect.status(invoice_id=invoice_id)
                logger.info("[IS] SDK status resp=%s", str(resp)[:500])
                inv = resp.get("invoice") or {}
                state = inv.get("state") or resp.get("state") or resp.get("status") or "PENDING"
                amount = inv.get("net_amount") or inv.get("value") or inv.get("amount")
                currency = inv.get("currency") or resp.get("currency") or "KES"
                return {"status": state, "amount": amount, "currency": currency, "raw": resp}
            except Exception as e:
                logger.warning("[IS] SDK status failed: %s", e)

        # CRITICAL FIX: When only checkout_id available, get invoice_id FIRST
        if not invoice_id and checkout_id:
            logger.info("[IS] Resolving invoice_id from checkout_id=%s", _mask(checkout_id))
            try:
                payload = {"checkout_id": checkout_id, "public_key": self.public_key}
                r = requests.post(
                    f"{self.base_url}/api/v1/payment/status/",
                    json=payload,
                    headers=self._public_headers,
                    timeout=15,
                )

                if r.status_code == 200:
                    details = r.json()
                    invoice_id = (
                        details.get("invoice_id") or
                        (details.get("invoice") or {}).get("invoice_id") or
                        (details.get("invoice") or {}).get("id")
                    )
                    if invoice_id:
                        logger.info("[IS] Resolved invoice_id=%s from checkout", _mask(invoice_id))
                    else:
                        logger.warning("[IS] No invoice_id yet for checkout %s: %s", _mask(checkout_id), str(details)[:500])
                else:
                    logger.warning("[IS] checkout->status returned %s: %s", r.status_code, r.text[:500])
            except Exception as e:
                logger.warning("[IS] Failed to resolve invoice_id via checkout status: %s", e)

        # Now query with invoice_id if we have it
        if invoice_id:
            url = f"{self.base_url}/api/v1/payment/status/"
            payload = {"invoice_id": invoice_id, "public_key": self.public_key}
            
            try:
                logger.info("[IS] Status check with invoice_id=%s", _mask(invoice_id))
                r = requests.post(url, json=payload, headers=self._public_headers, timeout=15)
                logger.info("[IS] Status response: %s - %s", r.status_code, r.text[:500])
                
                if r.status_code == 200:
                    raw = r.json()
                    inv = raw.get("invoice") or raw
                    state = inv.get("state") or raw.get("state") or raw.get("status") or "PENDING"
                    amount = inv.get("net_amount") or inv.get("value") or inv.get("amount")
                    currency = inv.get("currency") or raw.get("currency") or "KES"
                    
                    # Add resolved invoice_id to response
                    raw["resolved_invoice_id"] = invoice_id
                    
                    return {"status": state, "amount": amount, "currency": currency, "raw": raw}
            except Exception as e:
                logger.warning("[IS] Status check failed: %s", e)

        # Fallback: payment too new, return pending
        logger.warning("[IS] Status check inconclusive - checkout_id=%s invoice_id=%s", 
                      _mask(checkout_id), _mask(invoice_id))
        return {
            "status": "PENDING",
            "amount": None,
            "currency": None,
            "raw": {
                "detail": "Payment being processed - invoice not yet created",
                "checkout_id": checkout_id,
                "invoice_id": invoice_id,
            }
        }

    def get_checkout(self, checkout_id: str) -> Dict[str, Any]:
        """Get checkout details to extract invoice_id."""
        if not checkout_id:
            raise IntaSendError("checkout_id required")
        
        try:
            payload = {"checkout_id": checkout_id, "public_key": self.public_key}
            r = requests.post(
                f"{self.base_url}/api/v1/payment/status/",
                json=payload,
                headers=self._public_headers,
                timeout=15,
            )
            if r.status_code != 200:
                logger.warning("[IS] get_checkout via status failed: %s - %s", r.status_code, r.text[:500])
                return {"status": "PENDING", "invoice_id": None, "raw": {}}

            raw = r.json()
            invoice_id = (
                raw.get("invoice_id")
                or (raw.get("invoice") or {}).get("invoice_id")
                or (raw.get("invoice") or {}).get("id")
            )
            state = (
                (raw.get("invoice") or {}).get("state")
                or raw.get("state")
                or raw.get("status")
                or "PENDING"
            )

            return {"status": state, "invoice_id": invoice_id, "raw": raw}
        except Exception as e:
            logger.exception("[IS] get_checkout exception")
            return {"status": "PENDING", "invoice_id": None, "raw": {"error": str(e)}}

    def create_payment_request(
        self,
        amount: float | str | Decimal,
        *,
        phone_number: str,
        email: str,
        narrative: str = "FlashLearn Premium",
    ) -> Dict[str, Any]:
        """Initiate M-Pesa STK push."""
        if self._sdk is None:
            raise IntaSendError("STK push requires SDK")

        amt = _to_amount(amount)
        msisdn = _normalize_phone(phone_number)
        payload = {"phone_number": msisdn, "email": email, "amount": float(amt), "narrative": narrative}

        try:
            resp = self._sdk.collect.mpesa_stk_push(**payload)
            logger.info("[IS] STK push resp=%s", str(resp)[:800])
        except Exception as e:
            logger.exception("[IS] STK push failed")
            raise IntaSendError(f"STK push failed: {e}") from e

        invoice = resp.get("invoice") or {}
        invoice_id = invoice.get("invoice_id") or resp.get("invoice_id") or resp.get("id")
        if not invoice_id:
            raise IntaSendError(f"No invoice_id in STK response: {resp}")
        return {"invoice_id": invoice_id, "request_id": resp.get("tracking_id"), "raw": resp}

    def verify_webhook_signature(self, payload: bytes | str, signature: str) -> bool:
        return True


_singleton: Optional[IntaSendClient] = None


def get_intasend_client() -> IntaSendClient:
    global _singleton
    if _singleton is None:
        _singleton = IntaSendClient()
    return _singleton
