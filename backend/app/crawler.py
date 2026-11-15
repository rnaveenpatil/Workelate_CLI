"""Lists files in the connected GitHub workspace. Optional and read-only.

Needs (both empty by default):
    GITHUB_TOKEN=
    GITHUB_REPO=owner/name        # e.g. rnaveenpatil/Workelate_CLI
    GITHUB_BRANCH=main

When GitHub is not configured the executor's local walker is used instead, so the
planner always gets *some* grounding context.
"""

import os

import httpx


def token() -> str:
    return os.getenv("GITHUB_TOKEN", "").strip()


def repo() -> str:
    return os.getenv("GITHUB_REPO", "").strip()


def branch() -> str:
    return os.getenv("GITHUB_BRANCH", "main").strip() or "main"


def is_configured() -> bool:
    return bool(token() and repo())


def list_files(limit: int = 200, prefix: str = "") -> list[str]:
    """Flat list of repo paths. Empty list when not configured or on error."""
    if not is_configured():
        return []
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{repo()}/git/trees/{branch()}",
            params={"recursive": "1"},
            headers={
                "Authorization": f"Bearer {token()}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        paths = [n["path"] for n in resp.json().get("tree", []) if n.get("type") == "blob"]
        if prefix:
            paths = [p for p in paths if p.startswith(prefix)]
        return paths[:limit]
    except Exception:  # noqa: BLE001
        return []


def status() -> dict:
    """Redacted diagnostics for /health."""
    return {
        "configured": is_configured(),
        "repo": repo(),
        "branch": branch(),
    }
