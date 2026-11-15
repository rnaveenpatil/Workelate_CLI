"""Thin LLM client. OpenAI-compatible. Keys are intentionally empty by default.

Set these in backend/.env to enable:
    LLM_API_KEY=
    LLM_BASE_URL=https://api.openai.com/v1
    LLM_MODEL=gpt-4o-mini
"""

import json
import os

import httpx

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")


def is_configured() -> bool:
    """True when an API key is present. Kept empty until the user adds one."""
    return bool(LLM_API_KEY.strip())


def chat(messages: list[dict], temperature: float = 0.2, timeout: float = 120.0) -> str:
    """Single completion call. Raises if the LLM is not configured."""
    if not is_configured():
        raise RuntimeError(
            "LLM not configured. Add LLM_API_KEY (and LLM_MODEL) to backend/.env"
        )
    resp = httpx.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": temperature,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def chat_json(messages: list[dict], **kw) -> dict:
    """Completion that must return JSON. Tolerates ```json fences."""
    raw = chat(messages, **kw).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    raw = raw.strip().removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)
