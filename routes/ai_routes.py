# routes/ai_routes.py — subscription-first + robust CORS (credentials) + quick reconcile
from __future__ import annotations

import json
import re
import requests
from typing import Optional, Tuple

from flask import request, jsonify, make_response, current_app
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import db, app
from models import AIGeneration, Flashcard, Deck, User, PaymentTransaction
from services.usage_tracker import can_generate_now, increment_after_success
from services.subscription_manager import is_active, activate
from services.intasend_client import get_intasend_client, IntaSendError


# ---------------- CORS helpers (explicit headers for credentialed requests) ----------------

def _allowed_origin_from_request() -> str | None:
    origin = (request.headers.get("Origin") or "").rstrip("/")
    allowed = set((current_app.config.get("CORS_ALLOW_ORIGINS") or []) + (current_app.config.get("FRONTEND_ORIGINS") or []))
    # also include the ones passed to CORS() in config.py via FRONTEND_ORIGINS
    for o in allowed:
        if origin == o.rstrip("/"):
            return origin
    return None

def _corsify(resp):
    """Attach the right CORS headers for credentialed requests (cookies or auth headers)."""
    origin = _allowed_origin_from_request()
    # Only reflect a specific allowed origin; never '*'
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


# ---------------- small helpers ----------------

def _strip_code_fences(s: str) -> str:
    if not isinstance(s, str):
        return ""
    return re.sub(r"```(?:json)?\s*([\s\S]*?)\s*```", r"\1", s, flags=re.IGNORECASE).strip()

def _best_effort_json(s: str):
    if not isinstance(s, str) or not s.strip():
        return None
    text = _strip_code_fences(s)
    try:
        return json.loads(text)
    except Exception:
        pass

    opens, start_idx = [], None
    for i, ch in enumerate(text):
        if ch in "{[":
            if not opens:
                start_idx = i
            opens.append(ch)
        elif ch in "}]":
            if not opens:
                continue
            last = opens[-1]
            if (last == "{" and ch == "}") or (last == "[" and ch == "]"):
                opens.pop()
                if not opens and start_idx is not None:
                    candidate = text[start_idx : i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        start_idx = None
                        continue
    return None

def _normalize_cards(raw):
    def pick(src: dict, keys):
        for k in keys:
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    if isinstance(raw, dict) and isinstance(raw.get("cards"), list):
        raw_list = raw["cards"]
    elif isinstance(raw, list):
        raw_list = raw
    else:
        return []

    items = []
    for c in raw_list:
        if not isinstance(c, dict):
            continue
        q = pick(c, ["question", "q", "front", "prompt"])
        a = pick(c, ["answer", "a", "back", "response", "explanation"])
        if q and a:
            items.append({"question": q, "answer": a})
    return items

def _resolve_user_id(identity):
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


# ---------------- IntaSend quick reconcile (avoid stale 402 after payment) ----------------

def _normalize_intasend_state(raw: Optional[str]) -> str:
    s = (raw or "").strip().upper()
    if s in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "succeeded"
    if s in {"FAILED", "CANCELLED", "CANCELED", "DECLINED", "EXPIRED"}:
        return "failed"
    return "pending"

def _latest_pending_tx_for_user(user_id: int) -> PaymentTransaction | None:
    return (
        PaymentTransaction.query
        .filter_by(user_id=user_id, status="pending")
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )

def _quick_reconcile_if_needed(user_id: int) -> bool:
    tx = _latest_pending_tx_for_user(user_id)
    if not tx:
        return False

    client = get_intasend_client()
    try:
        info = client.check_payment_status(
            invoice_id=tx.provider_ref or None,
            checkout_id=tx.api_ref or None,
        )
    except IntaSendError:
        return False

    inv = info.get("invoice") or {}
    raw_state = inv.get("state") or info.get("state") or info.get("status")
    norm = _normalize_intasend_state(raw_state)

    tx.provider_status = raw_state or tx.provider_status
    if norm == "succeeded":
        receipt = inv.get("mpesa_receipt") or inv.get("receipt")
        tx.mark_succeeded(provider_ref=receipt, provider_status=raw_state)

        amount = tx.amount or int(app.config.get("BILLING_PLAN_MONTHLY_KES", 100))
        currency = tx.currency or str(app.config.get("BILLING_CURRENCY", "KES"))
        activate(user_id, plan="monthly", amount=amount, currency=currency)
        return True

    if norm == "failed":
        tx.mark_failed(reason="quick_reconcile: FAILED from IntaSend", provider_status=raw_state)
        return False

    tx.mark_pending(provider_status=raw_state)
    return False


# ---------------- LLM provider wrappers ----------------

def _call_openai_compatible_api(
    api_url: str,
    api_key: str,
    prompt: str,
    model: str = "gpt-3.5-turbo",
    max_tokens: int = 600,
    temperature: float = 0.2,
    timeout: int = 30,
) -> Tuple[Optional[str], Optional[dict]]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if "choices" in data and data["choices"]:
                return data["choices"][0].get("message", {}).get("content", ""), None
            return None, {"error": "unexpected_response_format", "data": data}
        return None, {"status_code": resp.status_code, "response_text": resp.text[:500]}
    except requests.RequestException as e:
        return None, {"type": type(e).__name__, "message": str(e)}

def _call_groq_api(api_key: str, prompt: str, model="llama-3.1-8b-instant"):
    return _call_openai_compatible_api(
        "https://api.groq.com/openai/v1/chat/completions", api_key, prompt, model=model
    )

def _call_together_api(api_key: str, prompt: str, model="meta-llama/Llama-2-7b-chat-hf"):
    return _call_openai_compatible_api(
        "https://api.together.xyz/v1/chat/completions", api_key, prompt, model=model
    )

def _try_multiple_apis(prompt: str):
    groq_key = current_app.config.get("GROQ_API_KEY")
    if groq_key:
        result, err = _call_groq_api(groq_key.strip(), prompt)
        if result:
            return result, None
    together_key = current_app.config.get("TOGETHER_API_KEY")
    if together_key:
        result, err = _call_together_api(together_key.strip(), prompt)
        if result:
            return result, None
    openai_key = current_app.config.get("OPENAI_API_KEY")
    if openai_key:
        result, err = _call_openai_compatible_api(
            "https://api.openai.com/v1/chat/completions", openai_key.strip(), prompt, "gpt-3.5-turbo"
        )
        if result:
            return result, None
    return None, {"error": "no_working_api"}


# ---------------- Resource ----------------

class AIGenerateFlashcards(Resource):
    def options(self):
        # Preflight must include allow-credentials:true and a reflected allowed origin
        resp = make_response(("", 204))
        return _corsify(resp)

    @jwt_required()
    def post(self):
        body = request.get_json(force=True) or {}
        text_in = (body.get("text") or "").strip()
        deck_id = body.get("deck_id")
        count = int(body.get("count") or 12)

        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            resp = make_response(jsonify({"error": "invalid token payload"}), 401)
            return _corsify(resp)

        user = User.query.get(user_id)
        if not user:
            resp = make_response(jsonify({"error": "user not found"}), 404)
            return _corsify(resp)

        # validation
        if len(text_in) < 30:
            resp = make_response(jsonify({"error": "text must be at least 30 characters"}), 400)
            return _corsify(resp)
        if not (3 <= count <= 50):
            resp = make_response(jsonify({"error": "count must be between 3 and 50"}), 400)
            return _corsify(resp)

        deck_obj = None
        if deck_id is not None:
            deck_obj = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
            if deck_obj is None:
                resp = make_response(jsonify({"error": "deck not found or not yours"}), 404)
                return _corsify(resp)

        # ---- subscription-first (quick reconcile, then gate) ----
        db.session.expire_all()
        _quick_reconcile_if_needed(user_id)
        server_active, sub = is_active(user_id)

        grace_used = False  # <- we'll set this if DB truth shows active but cache says no
        gate_ctx = {}

        if not server_active:
            allowed, gate_ctx = can_generate_now(user)
            if not allowed:
                # ✨ GRACE: if DB says the user is active (even if cache says not), allow one generation
                active_now, _ = is_active(user_id)
                if active_now:
                    gate_ctx["grace"] = True
                    grace_used = True
                else:
                    resp = make_response(jsonify({
                        "code": "PAYWALL",
                        "usage": gate_ctx,
                        "debug": {"user_id": user_id, "server_active": False},
                    }), 402)
                    return _corsify(resp)

        # ---- record generation ----
        gen = AIGeneration(
            user_id=user_id,
            deck_id=deck_id,
            source_type="text",
            source_excerpt=text_in[:1000],
            prompt=f"Generate {count} flashcards as JSON",
            model="multi-provider",
            status="queued",
        )
        db.session.add(gen)
        db.session.commit()

        prompt = f"""You are a flashcard generator.

Return EXACTLY this JSON (no prose, no fences):

{{
  "cards":[
    {{"question":"...","answer":"..."}}
  ]
}}

Rules:
- one JSON object with top-level "cards"
- EXACTLY {count} cards
- clear questions, complete answers
- no duplicates/placeholders

Content:
{text_in}
"""

        out_text, err = _try_multiple_apis(prompt)
        if err is not None:
            gen.status = "failed"
            gen.output = {"error": "AI request failed", "detail": err}
            db.session.commit()
            resp = make_response(jsonify({"error": "AI request failed", "detail": err}), 502)
            return _corsify(resp)

        parsed = _best_effort_json(out_text)
        if not parsed:
            alt_prompt = prompt + "\nREMEMBER: return ONLY the JSON object."
            out_text2, _ = _try_multiple_apis(alt_prompt)
            if out_text2:
                parsed = _best_effort_json(out_text2)

        if not parsed:
            gen.status = "failed"
            gen.output = {"parse_error": (out_text or "")[-1000:]}
            db.session.commit()
            resp = make_response(jsonify(
                {"error": "Could not parse JSON output", "raw_output": (out_text or "")[-500:]}
            ), 500)
            return _corsify(resp)

        cards = _normalize_cards(parsed)
        if not cards:
            gen.status = "failed"
            gen.output = {"no_cards": parsed}
            db.session.commit()
            resp = make_response(jsonify({"error": "No valid cards produced"}), 500)
            return _corsify(resp)

        cards = cards[:count]
        gen.status = "complete"
        gen.output = {"cards": cards}
        db.session.commit()

        inserted = 0
        if deck_obj:
            new_rows = [Flashcard(deck_id=deck_id, front_text=c["question"], back_text=c["answer"]) for c in cards]
            db.session.add_all(new_rows)
            db.session.commit()
            inserted = len(new_rows)

        increment_after_success(user, n=1)

        # Usage context: treat as premium if already active OR grace was used
        usage_plan = "premium" if (server_active or grace_used) else "free"
        if grace_used:
            gate_ctx["grace_used"] = True

        payload = {
            "deck_id": deck_id,
            "cards": cards,
            "inserted_count": inserted,
            "generation_id": gen.id,
            "usage": {"plan": usage_plan, **gate_ctx},
        }
        resp = make_response(jsonify(payload), 200)
        return _corsify(resp)
