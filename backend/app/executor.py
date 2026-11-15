"""Runs the actions the planner chose. Deliberately tiny: 3 tools."""

import os

from . import cli

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", os.getcwd())

TOOLS = ("run_command", "read_file", "write_file")


def _safe_path(path: str) -> str:
    """Keep file access inside the workspace."""
    full = os.path.abspath(os.path.join(WORKSPACE_DIR, path))
    root = os.path.abspath(WORKSPACE_DIR)
    if not full.startswith(root):
        raise ValueError(f"path outside workspace: {path}")
    return full


def execute(action: dict) -> dict:
    tool = (action or {}).get("tool", "")

    if tool == "run_command":
        return cli.run(
            action.get("command", ""),
            cwd=action.get("cwd"),
            timeout=int(action.get("timeout", 120)),
        )

    if tool == "read_file":
        try:
            with open(_safe_path(action.get("path", "")), "r", encoding="utf-8", errors="replace") as fh:
                return {"ok": True, "stdout": fh.read()[:8000], "stderr": "", "exit_code": 0}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(exc), "exit_code": -1}

    if tool == "write_file":
        try:
            full = _safe_path(action.get("path", ""))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(action.get("content", ""))
            return {"ok": True, "stdout": f"wrote {action.get('path')}", "stderr": "", "exit_code": 0}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(exc), "exit_code": -1}

    return {"ok": False, "stdout": "", "stderr": f"unknown tool: {tool!r}", "exit_code": -1}
