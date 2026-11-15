"""Lists files in the connected GitHub workspace. Optional and read-only.

Needs (both empty by default):
    GITHUB_TOKEN=
    GITHUB_REPO=owner/name        # e.g. rnaveenpatil/Workelate_CLI
"""

import os

import httpx

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")


def is_configured() -> bool:
    return bool(GITHUB_TOKEN.strip() and GITHUB_REPO.strip())


def list_files(limit: int = 200, prefix: str = "") -> list[str]:
    """Flat list of repo paths. Empty list when not configured or on error."""
    if not is_configured():
        return []
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}",
            params={"recursive": "1"},
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
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
