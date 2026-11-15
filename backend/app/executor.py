"""Runs the actions the planner chose.

Every branch returns the same result shape (``ok``/``exit_code``/``stdout``/
``stderr``) so the inspector and the LLM see one consistent format.
"""

import os

from . import cli, crawler, web

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", os.getcwd())

TOOLS = (
    "run_command",
    "read_file",
    "write_file",
    "list_files",
    "http_request",
    "fetch_web",
)

MAX_READ = 8000
LIST_LIMIT = 200


def _root() -> str:
    return os.path.abspath(WORKSPACE_DIR)


def _safe_path(path: str) -> str:
    """Resolve a workspace-relative path, refusing anything outside the workspace."""
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("path is required")

    root = _root()
    full = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw))

    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"path outside workspace: {path}")
    return full


def _local_listing(prefix: str = "", limit: int = LIST_LIMIT, max_depth: int = 6) -> list[str]:
    """Walk the workspace and return relative file paths."""
    root = _root()
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}
        ]
        depth = dirpath[len(root) :].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if prefix and not rel.startswith(prefix):
                continue
            found.append(rel)
            if len(found) >= limit:
                return sorted(found)
    return sorted(found)


def execute(action: dict) -> dict:
    tool = (action or {}).get("tool", "")

    if tool == "run_command":
        return cli.run(
            action.get("command", ""),
            cwd=action.get("cwd"),
            timeout=int(action.get("timeout", 120)),
        )

    if tool == "read_file":
        if cli.uses_remote():
            return cli.read_file_remote(action.get("path", ""))
        try:
            with open(
                _safe_path(action.get("path", "")), "r", encoding="utf-8", errors="replace"
            ) as fh:
                return {"ok": True, "stdout": fh.read()[:MAX_READ], "stderr": "", "exit_code": 0}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(exc), "exit_code": -1}

    if tool == "write_file":
        if cli.uses_remote():
            return cli.write_file_remote(action.get("path", ""), action.get("content", ""))
        try:
            full = _safe_path(action.get("path", ""))
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
            content = action.get("content", "")
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content if isinstance(content, str) else str(content))
            return {
                "ok": True,
                "stdout": f"wrote {action.get('path')} ({len(str(content))} chars)",
                "stderr": "",
                "exit_code": 0,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(exc), "exit_code": -1}

    if tool == "list_files":
        prefix = str(action.get("prefix", "") or "")
        if cli.uses_remote():
            return cli.list_files_remote(prefix)
        try:
            files = _local_listing(prefix=prefix)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(exc), "exit_code": -1}
        return {
            "ok": True,
            "stdout": "\n".join(files) or "(no files)",
            "stderr": "",
            "exit_code": 0,
        }

    if tool == "http_request":
        return web.http_request(
            action.get("url", ""),
            method=action.get("method", "GET"),
            headers=action.get("headers") or {},
            body=action.get("body"),
            timeout=float(action.get("timeout", web.DEFAULT_TIMEOUT)),
        )

    if tool == "fetch_web":
        return web.fetch_page(
            action.get("url", ""),
            timeout=float(action.get("timeout", web.DEFAULT_TIMEOUT)),
        )

    return {"ok": False, "stdout": "", "stderr": f"unknown tool: {tool!r}", "exit_code": -1}


def workspace_files() -> list[str]:
    """Grounding list: the remote sandbox when remote, else the local workspace."""
    if cli.uses_remote():
        result = cli.list_files_remote()
        if result.get("ok"):
            return [p for p in (result.get("stdout") or "").splitlines() if p.strip()]
        return []
    try:
        return _local_listing()
    except Exception:  # noqa: BLE001
        return []


__all__ = ["TOOLS", "execute", "workspace_files", "_safe_path", "_local_listing"]
