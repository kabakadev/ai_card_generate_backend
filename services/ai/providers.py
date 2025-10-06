# services/ai/providers.py
"""LLM provider API clients with retry logic and error handling."""

import logging
import requests
from typing import Optional, Tuple, Dict, Any, List

from .constants import API_TIMEOUT, DEFAULT_MAX_TOKENS, DEFAULT_MODEL_TEMPERATURE

logger = logging.getLogger(__name__)


class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""
    pass


def call_openai_compatible_api(
    api_url: str,
    api_key: str,
    prompt: str,
    model: str = "gpt-3.5-turbo",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_MODEL_TEMPERATURE,
    timeout: int = API_TIMEOUT,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Call OpenAI-compatible API endpoint.
    
    Args:
        api_url: API endpoint URL
        api_key: API authentication key
        prompt: User prompt
        model: Model identifier
        max_tokens: Maximum tokens in response
        temperature: Sampling temperature
        timeout: Request timeout in seconds
        
    Returns:
        Tuple of (response_text, error_dict)
    """
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
        resp = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=timeout
        )
        resp.raise_for_status()
        
        data = resp.json()
        if "choices" in data and data["choices"]:
            content = data["choices"][0].get("message", {}).get("content", "")
            return content, None
        
        return None, {
            "error": "unexpected_response_format",
            "data": data
        }
        
    except requests.Timeout:
        logger.error(f"API timeout for {api_url}")
        return None, {"error": "api_timeout", "url": api_url}
    
    except requests.HTTPError as e:
        logger.error(f"HTTP error from {api_url}: {e}")
        return None, {
            "error": "http_error",
            "status_code": e.response.status_code,
            "response": e.response.text[:500]
        }
    
    except requests.RequestException as e:
        logger.error(f"Request error for {api_url}: {e}")
        return None, {
            "error": "request_failed",
            "type": type(e).__name__,
            "message": str(e)
        }
    
    except Exception as e:
        logger.exception(f"Unexpected error calling {api_url}")
        return None, {
            "error": "unexpected_error",
            "type": type(e).__name__,
            "message": str(e)
        }


def call_groq_api(
    api_key: str,
    prompt: str,
    model: str = "llama-3.1-8b-instant"
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Call Groq API."""
    return call_openai_compatible_api(
        "https://api.groq.com/openai/v1/chat/completions",
        api_key,
        prompt,
        model=model
    )


def call_together_api(
    api_key: str,
    prompt: str,
    model: str = "meta-llama/Llama-2-7b-chat-hf"
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Call Together AI API."""
    return call_openai_compatible_api(
        "https://api.together.xyz/v1/chat/completions",
        api_key,
        prompt,
        model=model
    )


def call_openai_api(
    api_key: str,
    prompt: str,
    model: str = "gpt-3.5-turbo"
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Call OpenAI API."""
    return call_openai_compatible_api(
        "https://api.openai.com/v1/chat/completions",
        api_key,
        prompt,
        model=model
    )


def try_multiple_providers(
    prompt: str,
    groq_key: Optional[str] = None,
    together_key: Optional[str] = None,
    openai_key: Optional[str] = None
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Try multiple LLM providers in sequence until one succeeds.
    
    Args:
        prompt: User prompt
        groq_key: Optional Groq API key
        together_key: Optional Together API key
        openai_key: Optional OpenAI API key
        
    Returns:
        Tuple of (response_text, error_dict)
    """
    errors: List[Tuple[str, Dict[str, Any]]] = []
    
    # Try Groq
    if groq_key:
        try:
            result, err = call_groq_api(groq_key.strip(), prompt)
            if result:
                logger.info("Successfully used Groq API")
                return result, None
            if err:
                errors.append(("groq", err))
        except Exception as e:
            logger.error(f"Groq API exception: {e}")
            errors.append(("groq", {"error": str(e)}))
    
    # Try Together
    if together_key:
        try:
            result, err = call_together_api(together_key.strip(), prompt)
            if result:
                logger.info("Successfully used Together API")
                return result, None
            if err:
                errors.append(("together", err))
        except Exception as e:
            logger.error(f"Together API exception: {e}")
            errors.append(("together", {"error": str(e)}))
    
    # Try OpenAI
    if openai_key:
        try:
            result, err = call_openai_api(openai_key.strip(), prompt)
            if result:
                logger.info("Successfully used OpenAI API")
                return result, None
            if err:
                errors.append(("openai", err))
        except Exception as e:
            logger.error(f"OpenAI API exception: {e}")
            errors.append(("openai", {"error": str(e)}))
    
    logger.error(f"All LLM providers failed. Errors: {errors}")
    return None, {
        "error": "all_providers_failed",
        "details": errors
    }