"""Thin LLM client. OpenAI-compatible. Keys are intentionally empty by default.

Set these in backend/.env to enable:
    LLM_API_KEY=
    LLM_BASE_URL=https://api.openai.com/v1
    LLM_MODEL=gpt-4o-mini
    LLM_TIMEOUT=120
    LLM_RETRIES=2
"""

import json
import os

import httpx

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


def base_url() -> str:
    return os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")


def api_key() -> str:
    return os.getenv("LLM_API_KEY", "").strip()


def model() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def is_configured() -> bool:
    """True when an API key is present. Kept empty until the user adds one."""
    return bool(api_key())


def _extract_content(payload: dict) -> str:
    """Pull the assistant text out of an OpenAI-style response."""
    try:
        choice = payload["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"malformed LLM response: {str(payload)[:300]}") from exc

    message = choice.get("message") or {}
    content = message.get("content")
    if content is None:
        content = choice.get("text")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"empty LLM message: {str(choice)[:300]}")
    return content


def chat(messages: list[dict], temperature: float = 0.2, timeout: float | None = None) -> str:
    """Single completion call with bounded retries on transient failures."""
    key = api_key()
    if not key:
        raise RuntimeError(
            "LLM not configured. Add LLM_API_KEY (and LLM_MODEL) to backend/.env"
        )

    timeout = float(timeout or os.getenv("LLM_TIMEOUT", "120"))
    retries = max(0, int(os.getenv("LLM_RETRIES", "2")))
    url = f"{base_url()}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model(), "messages": messages, "temperature": temperature}

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
            if resp.status_code >= 500:
                last_error = RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            if resp.status_code >= 400:
                # 4xx is not transient - fail fast with the provider's message.
                detail = resp.text[:300]
                try:
                    detail = resp.json().get("error", {}).get("message", detail)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(f"LLM HTTP {resp.status_code}: {detail}")
            return _extract_content(resp.json())
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001  (timeouts, connection resets)
            last_error = exc

    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last_error}")


def chat_json(messages: list[dict], **kw) -> dict:
    """Completion that must return JSON. Tolerates ```json fences.

    Prefer ``planner.parse_action`` for agent steps; kept for direct callers.
    """
    raw = chat(messages, **kw).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    raw = raw.strip().removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def status() -> dict:
    """Redacted diagnostics for /health."""
    return {
        "configured": is_configured(),
        "model": model(),
        "base_url": base_url(),
    }
