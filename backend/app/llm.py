"""Thin OpenAI-compatible LLM client with provider fallback."""

import json
import os
from collections.abc import Callable

import httpx

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
MAX_FALLBACK_PROVIDERS = 3


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _global_base_url() -> str:
    return _env("LLM_BASE_URL").rstrip("/") or DEFAULT_BASE_URL


def _global_model() -> str:
    return _env("LLM_MODEL") or DEFAULT_MODEL


def providers() -> list[dict]:
    """Return configured providers in primary-then-fallback order."""
    found: list[dict] = []

    def add(name: str, key: str, base: str, model: str) -> None:
        if not key:
            return
        base = (base or _global_base_url()).rstrip("/") or DEFAULT_BASE_URL
        model = model or _global_model()
        signature = (base, key, model)
        if not any((p["base_url"], p["api_key"], p["model"]) == signature for p in found):
            found.append({"name": name, "base_url": base, "api_key": key, "model": model})

    add("primary", _env("LLM_API_KEY"), _env("LLM_BASE_URL"), _env("LLM_MODEL"))
    for index in range(1, MAX_FALLBACK_PROVIDERS + 1):
        add(
            f"LLM_{index}", _env(f"LLM_{index}_API_KEY"),
            _env(f"LLM_{index}_BASE_URL"), _env(f"LLM_{index}_MODEL"),
        )
    return found


def base_url() -> str:
    found = providers()
    return found[0]["base_url"] if found else _global_base_url()


def api_key() -> str:
    found = providers()
    return found[0]["api_key"] if found else ""


def model() -> str:
    found = providers()
    return found[0]["model"] if found else _global_model()


def is_configured() -> bool:
    return bool(providers())


def _extract_content(payload: dict) -> str:
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


def chat(
    messages: list[dict],
    temperature: float = 0.2,
    timeout: float | None = None,
    validator: Callable[[str], object] | None = None,
) -> str:
    """Call providers in order; invalid structured output also triggers fallback."""
    found = providers()
    if not found:
        raise RuntimeError(
            "LLM not configured. Add LLM_API_KEY (and numbered fallback keys) to backend/.env"
        )

    timeout = float(timeout or os.getenv("LLM_TIMEOUT", "120"))
    retries = max(0, int(os.getenv("LLM_RETRIES", "2")))
    errors: list[str] = []

    for provider in found:
        url = f"{provider['base_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        }
        body = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature,
        }
        last_error: Exception | str | None = None
        for _ in range(retries + 1):
            try:
                resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
                if resp.status_code >= 500:
                    last_error = RuntimeError(
                        f"LLM HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                    continue
                if resp.status_code >= 400:
                    detail = resp.text[:300]
                    try:
                        detail = resp.json().get("error", {}).get("message", detail)
                    except Exception:  # noqa: BLE001
                        pass
                    last_error = RuntimeError(f"LLM HTTP {resp.status_code}: {detail}")
                    break
                content = _extract_content(resp.json())
                if validator is not None:
                    validator(content)
                return content
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        errors.append(f"{provider['name']} ({provider['model']}): {last_error}")

    raise RuntimeError("all LLM providers failed: " + " | ".join(errors))


def chat_json(messages: list[dict], **kw) -> dict:
    raw = chat(messages, **kw).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    raw = raw.strip().removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def status() -> dict:
    """Redacted diagnostics; never exposes API keys."""
    found = providers()
    primary = found[0] if found else None
    return {
        "configured": bool(found),
        "api_key_set": bool(primary),
        "model": primary["model"] if primary else _global_model(),
        "base_url": primary["base_url"] if primary else _global_base_url(),
        "provider_count": len(found),
        "providers": [
            {
                "name": provider["name"], "base_url": provider["base_url"],
                "model": provider["model"], "api_key_set": bool(provider["api_key"]),
            }
            for provider in found
        ],
    }
