"""Turns raw tool output into a short, readable observation for the LLM."""

MAX_LINES = 200
MAX_CHARS = 20000


def observe(result: dict) -> str:
    """Compact summary of a tool result: status line, then stdout/stderr."""
    if not isinstance(result, dict):
        return f"unexpected result: {result!r}"

    status = "OK" if result.get("ok") else "FAILED"
    code = result.get("exit_code", "?")
    lines = [f"status={status} exit_code={code}"]

    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()

    if out:
        lines.append("stdout:\n" + _clip(out))
    if err:
        lines.append("stderr:\n" + _clip(err))
    if not out and not err:
        lines.append("(no output)")

    return "\n".join(lines)


def _clip(text: str) -> str:
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n...[truncated]"
    parts = text.splitlines()
    if len(parts) > MAX_LINES:
        text = "\n".join(parts[:MAX_LINES]) + f"\n...[{len(parts) - MAX_LINES} more lines]"
    return text
