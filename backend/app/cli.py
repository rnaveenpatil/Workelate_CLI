"""Terminal tool for Workelate_CLI.

Two modes, chosen by env ``CLI_MODE``:
    local  -> run commands with subprocess in WORKSPACE_DIR      (default)
    remote -> POST the command to a remote terminal server
              (the GitHub workspace / codespace running cli_server.py)

Remote env:
    CLI_REMOTE_URL=https://<codespace>-8001.app.github.dev
    CLI_TOKEN=<shared secret>          # must match the server's CLI_TOKEN
    CLI_REMOTE_TIMEOUT=45              # seconds; added on top of the command timeout

Config is read on every call so ``.env`` edits take effect without a restart.
"""

import os
import subprocess

import httpx

MAX_OUTPUT = 8000
LOCAL_TIMEOUT_CEILING = 1800


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if text and len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text or ""


def _mode() -> str:
    return os.getenv("CLI_MODE", "local").strip().lower()


def _remote_url() -> str:
    return os.getenv("CLI_REMOTE_URL", "").strip().rstrip("/")


def _token() -> str:
    return os.getenv("CLI_TOKEN", "").strip()


def _workspace() -> str:
    return os.getenv("WORKSPACE_DIR", "").strip() or os.getcwd()


def run_local(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Run a command with the local shell, capturing stdout/stderr."""
    if not str(command).strip():
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "empty command"}

    timeout = max(1, min(int(timeout or 120), LOCAL_TIMEOUT_CEILING))
    workdir = cwd or _workspace()

    if not os.path.isdir(workdir):
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"cwd not found: {workdir}",
        }

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
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
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc)}


def run_remote(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Forward one command to the remote terminal server (cli_server.py)."""
    if not str(command).strip():
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "empty command"}

    url = _remote_url()
    if not url:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "CLI_REMOTE_URL not set (required when CLI_MODE=remote)",
        }

    timeout = max(1, int(timeout or 120))
    http_timeout = timeout + float(os.getenv("CLI_REMOTE_TIMEOUT", "45"))
    headers = {"X-CLI-Token": _token()} if _token() else {}

    try:
        resp = httpx.post(
            f"{url}/exec",
            headers=headers,
            json={"command": command, "cwd": cwd, "timeout": timeout},
            timeout=http_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"remote unreachable at {url}: {exc}",
        }

    if resp.status_code >= 400:
        detail = resp.text[:500]
        try:
            detail = resp.json().get("detail", detail)
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"remote HTTP {resp.status_code}: {detail}",
        }

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"remote returned non-JSON body: {exc}",
        }

    return {
        "ok": bool(data.get("ok")),
        "exit_code": data.get("exit_code", -1),
        "stdout": _clip(data.get("stdout", "")),
        "stderr": _clip(data.get("stderr", "")),
    }


def run(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Run one shell command on the workspace terminal (local or remote)."""
    if _mode() == "remote":
        return run_remote(command, cwd, timeout)
    return run_local(command, cwd, timeout)


def mode() -> str:
    """Effective terminal mode: ``local`` or ``remote``."""
    return _mode()


def status() -> dict:
    """Diagnostics for /health: which channel is wired and whether it looks usable."""
    configured = _mode() != "remote" or bool(_remote_url())
    return {
        "mode": _mode(),
        "workspace": _workspace(),
        "remote_url": _remote_url(),
        "token_set": bool(_token()),
        "configured": configured,
    }
