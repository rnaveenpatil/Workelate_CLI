"""Terminal tool for Workelate_CLI.

Two modes, chosen by env CLI_MODE:
    local  -> run commands with subprocess in WORKSPACE_DIR      (default)
    remote -> POST the command to a remote terminal server
              (the GitHub workspace / codespace running cli_server.py)

Remote env:
    CLI_REMOTE_URL=https://<codespace>-8001.app.github.dev
    CLI_TOKEN=<shared secret>
"""

import os
import subprocess

import httpx

CLI_MODE = os.getenv("CLI_MODE", "local").lower()
CLI_REMOTE_URL = os.getenv("CLI_REMOTE_URL", "").rstrip("/")
CLI_TOKEN = os.getenv("CLI_TOKEN", "")
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", os.getcwd())
MAX_OUTPUT = 8000


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if text and len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text or ""


def run_local(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd or WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": _clip(proc.stdout),
            "stderr": _clip(proc.stderr),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc)}


def run_remote(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    if not CLI_REMOTE_URL:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "CLI_REMOTE_URL not set"}
    try:
        resp = httpx.post(
            f"{CLI_REMOTE_URL}/exec",
            headers={"X-CLI-Token": CLI_TOKEN},
            json={"command": command, "cwd": cwd, "timeout": timeout},
            timeout=timeout + 15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "ok": bool(data.get("ok")),
            "exit_code": data.get("exit_code", -1),
            "stdout": _clip(data.get("stdout", "")),
            "stderr": _clip(data.get("stderr", "")),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"remote error: {exc}"}


def run(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Run one shell command on the workspace terminal."""
    if CLI_MODE == "remote":
        return run_remote(command, cwd, timeout)
    return run_local(command, cwd, timeout)


def mode() -> str:
    return CLI_MODE
