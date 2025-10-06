# services/ai/__init__.py
"""AI generation service module."""

from .constants import (
    MAX_TEXT_LENGTH,
    MIN_TEXT_LENGTH,
    MIN_CARD_COUNT,
    MAX_CARD_COUNT,
    AI_GENERATION_RATE_LIMIT,
)
from .security import sanitize_for_prompt, validate_generation_input
from .parser import best_effort_json, normalize_flashcards, strip_code_fences
from .providers import try_multiple_providers, LLMProviderError

__all__ = [
    # Constants
    "MAX_TEXT_LENGTH",
    "MIN_TEXT_LENGTH",
    "MIN_CARD_COUNT",
    "MAX_CARD_COUNT",
    "AI_GENERATION_RATE_LIMIT",
    # Security
    "sanitize_for_prompt",
    "validate_generation_input",
    # Parser
    "best_effort_json",
    "normalize_flashcards",
    "strip_code_fences",
    # Providers
    "try_multiple_providers",
    "LLMProviderError",
]