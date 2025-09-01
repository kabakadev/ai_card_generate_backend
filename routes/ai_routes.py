# routes/ai_routes.py
import json
import re
import requests
from typing import Optional, Tuple

from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from flask_cors import cross_origin

from config import db
from models import AIGeneration, Flashcard, Deck

# -------- helpers --------

def _strip_code_fences(s: str) -> str:
    if not isinstance(s, str):
        return ""
    return re.sub(r"```(?:json)?\s*([\s\S]*?)\s*```", r"\1", s, flags=re.IGNORECASE).strip()

def _best_effort_json(s: str):
    """Try to parse JSON from messy model output (with or without fences)."""
    if not isinstance(s, str) or not s.strip():
        return None
    text = _strip_code_fences(s)

    try:
        return json.loads(text)
    except Exception:
        pass

    # Try to extract first valid object or array
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
    """Normalize model output into [{question, answer}, …]."""
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

# --- API call helpers ---

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
        "https://api.groq.com/openai/v1/chat/completions",
        api_key,
        prompt,
        model=model,
    )

def _call_together_api(api_key: str, prompt: str, model="meta-llama/Llama-2-7b-chat-hf"):
    return _call_openai_compatible_api(
        "https://api.together.xyz/v1/chat/completions",
        api_key,
        prompt,
        model=model,
    )

def _try_multiple_apis(prompt: str):
    from flask import current_app
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
            "https://api.openai.com/v1/chat/completions",
            openai_key.strip(),
            prompt,
            "gpt-3.5-turbo",
        )
        if result:
            return result, None
    return None, {"error": "no_working_api"}

# -------- resource --------

class AIGenerateFlashcards(Resource):
    @cross_origin()
    def options(self):
        return {}, 204

    @cross_origin()
    @jwt_required()
    def post(self):
        body = request.get_json(force=True) or {}
        text_in = (body.get("text") or "").strip()
        deck_id = body.get("deck_id")
        count = int(body.get("count") or 12)

        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        if len(text_in) < 30:
            return {"error": "text must be at least 30 characters"}, 400
        if not (3 <= count <= 50):
            return {"error": "count must be between 3 and 50"}, 400

        deck_obj = None
        if deck_id is not None:
            deck_obj = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
            if deck_obj is None:
                return {"error": "deck not found or not yours"}, 404

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

Read the content and produce EXACTLY this JSON object. DO NOT add any prose, notes, or markdown fences.

{{
  "cards":[
    {{"question":"...","answer":"..."}}
  ]
}}

Rules:
- Return ONLY a single JSON object with a top-level "cards" array.
- Create EXACTLY {count} cards.
- Questions should be clear and testable; answers complete.
- No duplicates, no placeholders.

Content:
{text_in}
"""

        out_text, err = _try_multiple_apis(prompt)
        if err is not None:
            gen.status = "failed"
            gen.output = {"error": "AI request failed", "detail": err}
            db.session.commit()
            return {"error": "AI request failed", "detail": err}, 502

        parsed = _best_effort_json(out_text)

        # Retry once with stricter reminder if needed
        if not parsed:
            alt_prompt = prompt + "\nREMEMBER: Only return the JSON object, nothing else."
            out_text2, err2 = _try_multiple_apis(alt_prompt)
            if out_text2:
                parsed = _best_effort_json(out_text2)

        if not parsed:
            gen.status = "failed"
            gen.output = {"parse_error": (out_text or "")[-1000:]}
            db.session.commit()
            return {"error": "Could not parse JSON output", "raw_output": (out_text or "")[-500:]}, 500

        cards = _normalize_cards(parsed)
        if not cards:
            gen.status = "failed"
            gen.output = {"no_cards": parsed}
            db.session.commit()
            return {"error": "No valid cards produced"}, 500

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

        return {
            "deck_id": deck_id,
            "cards": cards,
            "inserted_count": inserted,
            "generation_id": gen.id,
        }, 200
