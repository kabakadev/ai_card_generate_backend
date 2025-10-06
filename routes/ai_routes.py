# routes/ai_routes.py — Refactored with security and modularity
"""AI generation routes with security hardening and better organization."""

from __future__ import annotations

import logging
from typing import Optional

from flask import request, jsonify, make_response, current_app
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import db, limiter
from models import AIGeneration, Flashcard, Deck, User
from services.usage_tracker import can_generate_now, increment_after_success
from services.subscription_manager import is_active
from services.payment_reconciliation import quick_reconcile_payment
from services.cors_helpers import add_cors_headers

# AI service imports
from services.ai import (
    validate_generation_input,
    sanitize_for_prompt,
    best_effort_json,
    normalize_flashcards,
    try_multiple_providers,
    AI_GENERATION_RATE_LIMIT,
)

logger = logging.getLogger(__name__)


def resolve_user_id(identity) -> Optional[int]:
    """
    Extract user ID from JWT identity payload.
    
    Args:
        identity: JWT identity (can be int or dict)
        
    Returns:
        User ID or None if invalid
    """
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


def build_generation_prompt(text: str, count: int) -> str:
    """
    Build LLM prompt for flashcard generation.
    
    Args:
        text: Sanitized input text
        count: Number of cards to generate
        
    Returns:
        Complete prompt string
    """
    return f"""You are a flashcard generator.

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
{text}
"""


class AIGenerateFlashcards(Resource):
    """AI-powered flashcard generation endpoint."""
    
    # Apply rate limiting
    decorators = [limiter.limit(AI_GENERATION_RATE_LIMIT)]
    
    def options(self):
        """Handle CORS preflight requests."""
        resp = make_response(("", 204))
        return add_cors_headers(resp)
    
    @jwt_required()
    def post(self):
        """
        Generate flashcards from text using AI.
        
        Request body:
            - text: Input text (30-10000 chars)
            - count: Number of cards (3-50)
            - deck_id: Optional deck to add cards to
            
        Returns:
            - 200: Success with generated cards
            - 400: Invalid input
            - 401: Authentication error
            - 402: Payment required (rate limit)
            - 404: Deck not found
            - 500/502: Generation error
        """
        # Parse request
        body = request.get_json(force=True) or {}
        
        # Extract and validate inputs
        try:
            text_raw = str(body.get("text") or "").strip()
            count_raw = body.get("count", 12)
            deck_id = body.get("deck_id")
            
            # Convert deck_id to int if present
            if deck_id is not None:
                deck_id = int(deck_id)
            
            # parse, but do not clamp here, let validation enforce the range
            count = int(count_raw)
            
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid input types: {e}")
            resp = make_response(
                jsonify({"error": "invalid input types"}),
                400
            )
            return add_cors_headers(resp)
        
        # Validate inputs
        is_valid, error_msg = validate_generation_input(text_raw, count, deck_id)
        if not is_valid:
            resp = make_response(jsonify({"error": error_msg}), 400)
            return add_cors_headers(resp)
        
        # Get user identity
        identity = get_jwt_identity()
        user_id = resolve_user_id(identity)
        
        if user_id is None:
            logger.warning(f"Invalid JWT identity payload: {identity}")
            resp = make_response(
                jsonify({"error": "unauthorized"}),
                401
            )
            return add_cors_headers(resp)
        
        # Get user (consistent error response)
        user = User.query.get(user_id)
        if not user:
            logger.warning(f"User not found: {user_id}")
            resp = make_response(
                jsonify({"error": "unauthorized"}),
                401
            )
            return add_cors_headers(resp)
        
        # Validate deck ownership if provided
        deck_obj = None
        if deck_id is not None:
            deck_obj = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
            if deck_obj is None:
                logger.warning(
                    f"Deck access denied",
                    extra={"user_id": user_id, "deck_id": deck_id}
                )
                resp = make_response(
                    jsonify({"error": "deck not found or access denied"}),
                    404
                )
                return add_cors_headers(resp)
        
        # Subscription check with quick reconciliation
        db.session.expire_all()
        quick_reconcile_payment(user_id)
        server_active, sub = is_active(user_id)
        
        grace_used = False
        gate_ctx = {}
        
        if not server_active:
            # Check rate limits
            allowed, gate_ctx = can_generate_now(user)
            
            if not allowed:
                # Grace period: allow one generation if DB shows active
                active_now, _ = is_active(user_id)
                if active_now:
                    gate_ctx["grace"] = True
                    grace_used = True
                    logger.info(
                        f"Grace generation granted",
                        extra={"user_id": user_id}
                    )
                else:
                    logger.info(
                        f"Rate limit hit",
                        extra={"user_id": user_id, "gate_ctx": gate_ctx}
                    )
                    resp = make_response(
                        jsonify({
                            "code": "PAYWALL",
                            "usage": gate_ctx,
                            "debug": {
                                "user_id": user_id,
                                "server_active": False
                            },
                        }),
                        402
                    )
                    return add_cors_headers(resp)
        
        # Sanitize input text
        text_clean = sanitize_for_prompt(text_raw)
        
        # Create generation record
        gen = AIGeneration(
            user_id=user_id,
            deck_id=deck_id,
            source_type="text",
            source_excerpt=text_raw[:1000],
            prompt=f"Generate {count} flashcards as JSON",
            model="multi-provider",
            status="queued",
        )
        
        try:
            db.session.add(gen)
            db.session.commit()
        except Exception as e:
            logger.exception(f"Failed to create generation record: {e}")
            db.session.rollback()
            resp = make_response(
                jsonify({"error": "database error"}),
                500
            )
            return add_cors_headers(resp)
        
        # Build prompt
        prompt = build_generation_prompt(text_clean, count)
        
        # Call LLM providers
        out_text, err = try_multiple_providers(
            prompt,
            groq_key=current_app.config.get("GROQ_API_KEY"),
            together_key=current_app.config.get("TOGETHER_API_KEY"),
            openai_key=current_app.config.get("OPENAI_API_KEY"),
        )
        
        if err is not None:
            gen.status = "failed"
            gen.output = {"error": "AI request failed", "detail": err}
            
            try:
                db.session.commit()
            except Exception as e:
                logger.error(f"Failed to update generation status: {e}")
                db.session.rollback()
            
            logger.error(
                f"AI generation failed",
                extra={"user_id": user_id, "gen_id": gen.id, "error": err}
            )
            
            resp = make_response(
                jsonify({"error": "AI request failed", "detail": err}),
                502
            )
            return add_cors_headers(resp)
        
        # Parse response
        parsed = best_effort_json(out_text)
        
        # Retry with clearer prompt if parsing fails
        if not parsed:
            logger.warning("First parse attempt failed, retrying with clearer prompt")
            retry_prompt = prompt + "\nREMEMBER: return ONLY the JSON object."
            
            out_text2, _ = try_multiple_providers(
                retry_prompt,
                groq_key=current_app.config.get("GROQ_API_KEY"),
                together_key=current_app.config.get("TOGETHER_API_KEY"),
                openai_key=current_app.config.get("OPENAI_API_KEY"),
            )
            
            if out_text2:
                parsed = best_effort_json(out_text2)
        
        if not parsed:
            gen.status = "failed"
            gen.output = {"parse_error": (out_text or "")[-1000:]}
            
            try:
                db.session.commit()
            except Exception as e:
                logger.error(f"Failed to update generation status: {e}")
                db.session.rollback()
            
            logger.error(
                f"JSON parsing failed",
                extra={
                    "user_id": user_id,
                    "gen_id": gen.id,
                    "output_sample": (out_text or "")[-200:]
                }
            )
            
            resp = make_response(
                jsonify({
                    "error": "Could not parse JSON output",
                    "raw_output": (out_text or "")[-500:]
                }),
                500
            )
            return add_cors_headers(resp)
        
        # Normalize cards
        cards = normalize_flashcards(parsed)
        
        if not cards:
            gen.status = "failed"
            gen.output = {"no_cards": parsed}
            
            try:
                db.session.commit()
            except Exception as e:
                logger.error(f"Failed to update generation status: {e}")
                db.session.rollback()
            
            logger.error(
                f"No valid cards produced",
                extra={"user_id": user_id, "gen_id": gen.id}
            )
            
            resp = make_response(
                jsonify({"error": "No valid cards produced"}),
                500
            )
            return add_cors_headers(resp)
        
        # Limit to requested count
        cards = cards[:count]
        
        # Update generation record
        gen.status = "complete"
        gen.output = {"cards": cards}
        
        try:
            db.session.commit()
        except Exception as e:
            logger.error(f"Failed to mark generation complete: {e}")
            db.session.rollback()
        
        # Insert cards into deck if specified
        inserted = 0
        if deck_obj:
            new_rows = [
                Flashcard(
                    deck_id=deck_id,
                    front_text=c["question"],
                    back_text=c["answer"]
                )
                for c in cards
            ]
            
            try:
                db.session.add_all(new_rows)
                db.session.commit()
                inserted = len(new_rows)
                
                logger.info(
                    f"Inserted {inserted} flashcards",
                    extra={"user_id": user_id, "deck_id": deck_id}
                )
            except Exception as e:
                logger.error(f"Failed to insert flashcards: {e}")
                db.session.rollback()
        
        # Update usage tracking
        try:
            increment_after_success(user, n=1)
        except Exception as e:
            logger.error(f"Failed to update usage tracker: {e}")
        
        # Prepare response
        usage_plan = "premium" if (server_active or grace_used) else "free"
        if grace_used:
            gate_ctx["grace_used"] = True
        
        logger.info(
            f"AI generation successful",
            extra={
                "user_id": user_id,
                "gen_id": gen.id,
                "cards_count": len(cards),
                "inserted": inserted,
                "plan": usage_plan
            }
        )
        
        payload = {
            "deck_id": deck_id,
            "cards": cards,
            "inserted_count": inserted,
            "generation_id": gen.id,
            "usage": {"plan": usage_plan, **gate_ctx},
        }
        
        resp = make_response(jsonify(payload), 200)
        return add_cors_headers(resp)