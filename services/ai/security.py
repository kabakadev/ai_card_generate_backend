# services/ai/security.py
"""Security utilities for AI generation - prompt injection protection."""

import re
import logging
from typing import Tuple, Optional

from .constants import DANGEROUS_PATTERNS, MAX_TEXT_LENGTH

logger = logging.getLogger(__name__)


def sanitize_for_prompt(text: str) -> str:
    """
    Remove potential prompt injection attempts and limit length.
    
    Args:
        text: Raw user input text
        
    Returns:
        Sanitized text safe for LLM prompts
    """
    if not isinstance(text, str):
        return ""
    
    # Remove dangerous patterns
    cleaned = text
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            logger.warning(
                f"Detected potential prompt injection pattern: {pattern[:50]}",
                extra={"pattern": pattern, "text_sample": text[:100]}
            )
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    
    # Limit length
    cleaned = cleaned[:MAX_TEXT_LENGTH]
    
    # Remove excessive whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    return cleaned


def validate_generation_input(
    text: str,
    count: int,
    deck_id: Optional[int] = None
) -> Tuple[bool, Optional[str]]:
    """
    Validate all inputs for AI generation.
    
    Args:
        text: Input text for generation
        count: Number of cards to generate
        deck_id: Optional deck ID
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    from .constants import MIN_TEXT_LENGTH, MIN_CARD_COUNT, MAX_CARD_COUNT
    
    # Validate text
    if not isinstance(text, str):
        return False, "text must be a string"
    
    text = text.strip()
    if len(text) < MIN_TEXT_LENGTH:
        return False, f"text must be at least {MIN_TEXT_LENGTH} characters"
    
    if len(text) > MAX_TEXT_LENGTH:
        return False, f"text cannot exceed {MAX_TEXT_LENGTH} characters"
    
    # Validate count
    try:
        count = int(count)
    except (ValueError, TypeError):
        return False, "count must be an integer"
    
    if not (MIN_CARD_COUNT <= count <= MAX_CARD_COUNT):
        return False, f"count must be between {MIN_CARD_COUNT} and {MAX_CARD_COUNT}"
    
    # Validate deck_id if provided
    if deck_id is not None:
        try:
            deck_id = int(deck_id)
            if deck_id < 1:
                return False, "deck_id must be positive"
        except (ValueError, TypeError):
            return False, "deck_id must be an integer"
    
    return True, None