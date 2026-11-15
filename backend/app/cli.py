"""Terminal tool for Workelate_CLI.

Two modes, chosen by env ``CLI_MODE``:
    local  -> run commands with subprocess in WORKSPACE_DIR      (default)
    remote -> POST the command to a remote terminal server
              (the GitHub workspace / codespace running cli_server.py)

Remote env:
    CLI_REMOTE_URL=https://<codespace>-8000.app.github.dev
    CLI_TOKEN=<shared secret>          # must match the server's CLI_TOKEN
    CLI_GITHUB_TOKEN=<codespace token> # X-Github-Token for a private forwarded port
    CLI_REMOTE_TIMEOUT=45              # seconds; added on top of the command timeout

A Codespaces port is private by default. For a private port, copy the codespace's
``$GITHUB_TOKEN`` (starts with ``ghu_``) and set ``CLI_GITHUB_TOKEN``; otherwise make
the port public in the Codespaces Ports tab so the tunnel needs no auth.

Sandbox env (isolates every remote task in a Docker container):
    SANDBOX_CONTAINER=agent-sandbox     # empty = run directly on the codespace
    SANDBOX_WORKDIR=/workspace          # cwd inside the container
    SANDBOX_PYTHON=python3              # file-tool interpreter in the container
    SANDBOX_USER=1000:1000              # docker exec --user (rootless bind mounts)

Config is read on every call so ``.env`` edits take effect without a restart.
"""

import base64
import os
import shlex
import subprocess

import httpx

MAX_OUTPUT = 8000
LOCAL_TIMEOUT_CEILING = 1800

# --- Sandbox --------------------------------------------------------------
# When SANDBOX_CONTAINER is set, every remote command is run inside that
# Docker container (``docker exec``) instead of directly in the codespace, and
# the file tools read/write/list the container's workspace only.
#
#     SANDBOX_CONTAINER=agent-sandbox
#     SANDBOX_WORKDIR=/workspace          # cwd inside the container
#     SANDBOX_PYTHON=python3              # interpreter used by the file tools
SANDBOX_DEFAULT_WORKDIR = "/workspace"

# Small helpers executed inside the sandbox (argv-only, no shell interpolation).
_READ_SCRIPT = (
    "import os,sys,pathlib\n"
    "root=os.getcwd()\n"
    "p=os.path.realpath(os.path.join(root,sys.argv[1]))\n"
    "if p!=root and not p.startswith(root+os.sep):\n"
    "    sys.stderr.write('path outside workspace: '+sys.argv[1]);sys.exit(2)\n"
    "sys.stdout.write(pathlib.Path(p).read_text(encoding='utf-8',errors='replace'))\n"
)

_WRITE_SCRIPT = (
    "import base64,os,sys,pathlib\n"
    "root=os.getcwd()\n"
    "p=os.path.realpath(os.path.join(root,sys.argv[1]))\n"
    "if p!=root and not p.startswith(root+os.sep):\n"
    "    sys.stderr.write('path outside workspace: '+sys.argv[1]);sys.exit(2)\n"
    "data=base64.b64decode(sys.argv[2])\n"
    "pathlib.Path(p).parent.mkdir(parents=True,exist_ok=True)\n"
    "pathlib.Path(p).write_bytes(data)\n"
    "print('wrote '+sys.argv[1]+' ('+str(len(data))+' bytes)')\n"
)

_LIST_SCRIPT = (
    "import os,sys\n"
    "prefix=sys.argv[1] if len(sys.argv)>1 else ''\n"
    "skip={'.git','__pycache__','.venv','node_modules','.mypy_cache'}\n"
    "out=[]\n"
    "for dp,dn,fn in os.walk('.'):\n"
    "    dn[:]=[d for d in dn if d not in skip]\n"
    "    for f in fn:\n"
    "        rel=os.path.relpath(os.path.join(dp,f),'.').replace(os.sep,'/')\n"
    "        if rel.startswith(prefix): out.append(rel)\n"
    "print('\\n'.join(sorted(out)[:200]))\n"
)


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


def _github_token() -> str:
    """Codespace ``GITHUB_TOKEN`` used by GitHub's tunnel on private ports."""
    return os.getenv("CLI_GITHUB_TOKEN", "").strip()


def _sandbox_container() -> str:
    return os.getenv("SANDBOX_CONTAINER", "").strip()


def _sandbox_workdir() -> str:
    return os.getenv("SANDBOX_WORKDIR", SANDBOX_DEFAULT_WORKDIR).strip() or SANDBOX_DEFAULT_WORKDIR


def _sandbox_python() -> str:
    return os.getenv("SANDBOX_PYTHON", "python3").strip() or "python3"


def _sandbox_user() -> str:
    """Container user for ``docker exec --user`` (empty = image default)."""
    return os.getenv("SANDBOX_USER", "").strip()


def sandbox_enabled() -> bool:
    """True when remote commands should be isolated in a Docker container."""
    return bool(_sandbox_container())


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if _token():
        headers["X-CLI-Token"] = _token()
    if _github_token():
        # Required by GitHub when the forwarded port is private (not public).
        headers["X-Github-Token"] = _github_token()
    return headers


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

    try:
        resp = httpx.post(
            f"{url}/exec",
            headers=_headers(),
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

    tunnel_auth = "tunnel" in resp.headers.get("www-authenticate", "").lower()
    if resp.is_redirect or "pf-signin" in resp.headers.get("location", "") or tunnel_auth:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": (
                f"remote port requires GitHub sign-in (HTTP {resp.status_code}). "
                "The Codespaces port is private: set CLI_GITHUB_TOKEN to the codespace's "
                "$GITHUB_TOKEN (starts with ghu_), or make the port public in the Ports tab."
            ),
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
    """Run one shell command on the workspace terminal (local or remote).

    In remote + sandbox mode the command is wrapped in ``docker exec`` so it only
    touches the sandbox container, never the codespace's own files.
    """
    if _mode() == "remote":
        if sandbox_enabled():
            command = _sandbox_command(command)
            cwd = None
        return run_remote(command, cwd, timeout)
    return run_local(command, cwd, timeout)


def _sandbox_command(command: str) -> str:
    """Build a shell-safe ``docker exec`` invocation for the sandbox container."""
    argv = ["docker", "exec", "-w", _sandbox_workdir()]
    if _sandbox_user():
        # Rootless Docker maps container root to a subuid, so use the workspace
        # owner (1000:1000) or writes to the bind mount fail with EACCES.
        argv += ["--user", _sandbox_user()]
    argv += [_sandbox_container(), "/bin/sh", "-lc", command]
    return shlex.join(argv)


def _remote_python(script: str, args: list[str], timeout: int = 120) -> dict:
    """Run a python helper on the remote side (inside the sandbox when enabled)."""
    return run(shlex.join([_sandbox_python(), "-c", script, *args]), timeout=timeout)


def read_file_remote(path: str) -> dict:
    """Read a workspace file from the remote terminal / sandbox."""
    return _remote_python(_READ_SCRIPT, [str(path or "")])


def write_file_remote(path: str, content) -> dict:
    """Write a workspace file on the remote terminal / sandbox."""
    raw = content if isinstance(content, str) else str(content)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return _remote_python(_WRITE_SCRIPT, [str(path or ""), encoded])


def list_files_remote(prefix: str = "") -> dict:
    """List workspace files on the remote terminal / sandbox."""
    return _remote_python(_LIST_SCRIPT, [str(prefix or "")])


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
        "github_token_set": bool(_github_token()),
        "sandbox": {
            "enabled": sandbox_enabled(),
            "container": _sandbox_container(),
            "workdir": _sandbox_workdir(),
            "user": _sandbox_user(),
        },
        "configured": configured,
    }
