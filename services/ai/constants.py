# services/ai/constants.py
"""Configuration constants for AI generation."""

# Input validation
MAX_TEXT_LENGTH = 10000
MIN_TEXT_LENGTH = 30
MIN_CARD_COUNT = 3
MAX_CARD_COUNT = 50

# API configuration
API_TIMEOUT = 30
MAX_RETRIES = 1
DEFAULT_MODEL_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 600

# Rate limiting
AI_GENERATION_RATE_LIMIT = "10 per minute"

# Prompt injection patterns to detect
DANGEROUS_PATTERNS = [
    r"ignore\s+previous\s+instructions",
    r"disregard\s+all\s+previous",
    r"new\s+instructions:",
    r"system\s*:",
    r"assistant\s*:",
    r"forget\s+everything",
    r"you\s+are\s+now",
]