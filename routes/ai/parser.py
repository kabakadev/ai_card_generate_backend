# services/ai/parser.py
"""JSON parsing utilities for AI responses."""

import json
import re
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences from text."""
    if not isinstance(text, str):
        return ""
    return re.sub(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        r"\1",
        text,
        flags=re.IGNORECASE
    ).strip()


def best_effort_json(text: str) -> Optional[Any]:
    """
    Attempt to extract valid JSON from potentially malformed text.
    
    Tries multiple strategies:
    1. Direct parsing after stripping code fences
    2. Finding first complete JSON object/array
    
    Args:
        text: Raw text that may contain JSON
        
    Returns:
        Parsed JSON object/array or None if parsing fails
    """
    if not isinstance(text, str) or not text.strip():
        return None
    
    # Strategy 1: Strip fences and try direct parse
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    
    # Strategy 2: Find first complete JSON structure
    opens = []
    start_idx = None
    
    for i, ch in enumerate(cleaned):
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
                    candidate = cleaned[start_idx : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start_idx = None
                        continue
    
    logger.warning(
        "Failed to extract JSON from response",
        extra={"text_sample": text[:200]}
    )
    return None


def normalize_flashcards(raw: Any) -> List[Dict[str, str]]:
    """
    Normalize various flashcard formats into standard structure.
    
    Accepts:
    - {"cards": [...]}
    - [...]
    - Individual card objects with various key names
    
    Args:
        raw: Raw parsed JSON data
        
    Returns:
        List of normalized flashcard dictionaries
    """
    def pick_value(card: dict, keys: List[str]) -> str:
        """Pick first non-empty value from list of possible keys."""
        for key in keys:
            val = card.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    
    # Extract card list
    if isinstance(raw, dict) and isinstance(raw.get("cards"), list):
        card_list = raw["cards"]
    elif isinstance(raw, list):
        card_list = raw
    else:
        logger.warning(
            "Unexpected flashcard format",
            extra={"type": type(raw).__name__}
        )
        return []
    
    # Normalize each card
    normalized = []
    for card in card_list:
        if not isinstance(card, dict):
            continue
        
        question = pick_value(card, ["question", "q", "front", "prompt"])
        answer = pick_value(card, ["answer", "a", "back", "response", "explanation"])
        
        if question and answer:
            normalized.append({
                "question": question,
                "answer": answer
            })
    
    logger.info(f"Normalized {len(normalized)} flashcards from raw data")
    return normalized