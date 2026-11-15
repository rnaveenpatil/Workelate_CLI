"""Web tools: HTTP requests and readable web-page extraction.

Both helpers return the same shape the rest of the agent uses
(``ok`` / ``exit_code`` / ``stdout`` / ``stderr``) plus a couple of extras
(``status``, ``url``, ``content_type``) so observations stay uniform.
"""

import html as html_lib
import re

import httpx

MAX_BYTES = 2_000_000
DEFAULT_TIMEOUT = 30.0
ALLOWED_SCHEMES = ("http://", "https://")

_TEXT_CT = ("text/", "application/json", "application/xml", "application/javascript")


def _clip(text: str, limit: int = 20000) -> str:
    if text and len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text or ""


def _fail(reason: str, *, status: int = -1, url: str = "") -> dict:
    return {
        "ok": False,
        "exit_code": status,
        "stdout": "",
        "stderr": reason,
        "status": status,
        "url": url,
        "content_type": "",
    }


def _validate(url: str) -> str | None:
    if not str(url).strip():
        return "url is required"
    if not str(url).strip().lower().startswith(ALLOWED_SCHEMES):
        return "only http:// and https:// URLs are supported"
    return None


def http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Perform one HTTP request and return the response body as text."""
    problem = _validate(url)
    if problem:
        return _fail(problem, url=str(url))

    method = str(method or "GET").upper()
    payload = body.encode("utf-8") if isinstance(body, str) else body

    try:
        resp = httpx.request(
            method,
            str(url),
            headers=headers or {},
            content=payload,
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"request failed: {exc}", url=str(url))

    ctype = resp.headers.get("content-type", "")
    raw = resp.content[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    text = html_to_text(raw) if "html" in ctype.lower() else raw

    return {
        "ok": resp.status_code < 400,
        "exit_code": resp.status_code,
        "stdout": _clip(text),
        "stderr": "" if resp.status_code < 400 else f"HTTP {resp.status_code} {resp.reason_phrase}",
        "status": resp.status_code,
        "url": str(resp.url),
        "content_type": ctype,
    }


def fetch_page(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """GET a URL and return its readable text (HTML stripped, entities decoded)."""
    return http_request(url, method="GET", timeout=timeout)


def html_to_text(raw: str) -> str:
    """Best-effort HTML -> text without pulling in a parser dependency."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    # Preserve block boundaries as newlines before dropping tags.
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|header|footer)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def is_text_content(content_type: str) -> bool:
    """True when a content-type is worth showing to the model as text."""
    ct = (content_type or "").lower()
    return any(ct.startswith(prefix) or prefix in ct for prefix in _TEXT_CT)
