"""Terminal tool for Workelate_CLI.

Two modes, chosen by env ``CLI_MODE``:
    local   -> run commands with subprocess in WORKSPACE_DIR         (default)
    daytona -> run commands in a remote Daytona sandbox via the SDK

Daytona env:
    DAYTONA_API_KEY=<project api key>
    DAYTONA_API_URL=https://app.daytona.io/api
    DAYTONA_SANDBOX_ID=<sandbox id or name>   # optional; reused when set
    DAYTONA_WORKDIR=/home/daytona             # cwd inside the sandbox

When ``DAYTONA_SANDBOX_ID`` is empty, a running sandbox is reused if there is
one, otherwise a new sandbox is created. Config is read on every call so
``.env`` edits take effect without a restart.
"""

import base64
import os
import shlex
import subprocess

MAX_OUTPUT = 8000
LOCAL_TIMEOUT_CEILING = 1800

DAYTONA_DEFAULT_API_URL = "https://app.daytona.io/api"
DAYTONA_DEFAULT_WORKDIR = "/home/daytona"

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

_DAYTONA_CACHE: dict = {}


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if text and len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text or ""


def _mode() -> str:
    return os.getenv("CLI_MODE", "local").strip().lower()


def _workspace() -> str:
    return os.getenv("WORKSPACE_DIR", "").strip() or os.getcwd()


def _daytona_api_url() -> str:
    return os.getenv("DAYTONA_API_URL", DAYTONA_DEFAULT_API_URL).strip().rstrip("/")


def _daytona_api_key() -> str:
    return os.getenv("DAYTONA_API_KEY", "").strip()


def _daytona_sandbox_id() -> str:
    return os.getenv("DAYTONA_SANDBOX_ID", "").strip()


def _daytona_workdir() -> str:
    return os.getenv("DAYTONA_WORKDIR", DAYTONA_DEFAULT_WORKDIR).strip() or DAYTONA_DEFAULT_WORKDIR


def uses_remote() -> bool:
    """True when commands/files live in a remote sandbox (Daytona)."""
    return _mode() == "daytona"


def _reset_daytona_cache() -> None:
    """Drop cached client/sandbox (used by tests)."""
    _DAYTONA_CACHE.clear()


def _daytona_client():
    """Lazily build the Daytona client; recreated if the key/url changes."""
    if not _daytona_api_key():
        raise RuntimeError("DAYTONA_API_KEY is not set (CLI_MODE=daytona)")

    from daytona import Daytona, DaytonaConfig

    signature = (_daytona_api_url(), _daytona_api_key())
    if _DAYTONA_CACHE.get("client_signature") != signature:
        _DAYTONA_CACHE["client"] = Daytona(
            DaytonaConfig(api_key=signature[1], api_url=signature[0])
        )
        _DAYTONA_CACHE["client_signature"] = signature
        _DAYTONA_CACHE.pop("sandbox", None)
        _DAYTONA_CACHE.pop("sandbox_signature", None)
    return _DAYTONA_CACHE["client"]


def _daytona_sandbox():
    """Resolve the sandbox to use: configured id, a running one, or a new one."""
    client = _daytona_client()
    sandbox_id = _daytona_sandbox_id()
    signature = (sandbox_id, _daytona_workdir())
    if _DAYTONA_CACHE.get("sandbox_signature") == signature and _DAYTONA_CACHE.get("sandbox"):
        return _DAYTONA_CACHE["sandbox"]

    sandbox = client.get(sandbox_id) if sandbox_id else None
    if sandbox is None:
        running = None
        try:
            for candidate in client.list():
                if "STARTED" in str(getattr(candidate, "state", "")).upper():
                    running = candidate
                    break
        except Exception:  # noqa: BLE001  (empty account / list permission)
            running = None
        sandbox = running or client.create()

    if "STARTED" not in str(getattr(sandbox, "state", "")).upper():
        try:
            client.start(sandbox)
        except Exception:  # noqa: BLE001  (e.g. archived sandbox)
            sandbox.recover()

    _DAYTONA_CACHE["sandbox"] = sandbox
    _DAYTONA_CACHE["sandbox_signature"] = signature
    return sandbox


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


def run_daytona(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Run one command inside the Daytona sandbox."""
    if not str(command).strip():
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "empty command"}

    timeout = max(1, int(timeout or 120))
    try:
        sandbox = _daytona_sandbox()
        response = sandbox.process.exec(
            str(command), cwd=cwd or _daytona_workdir(), timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001  (auth, network, sandbox, timeout)
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"daytona error: {exc}",
        }

    output = getattr(response, "result", "") or ""
    exit_code = int(getattr(response, "exit_code", -1))
    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout": _clip(output),
        "stderr": "",
    }


def run(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Run one shell command on the workspace terminal (local or Daytona)."""
    if _mode() == "daytona":
        return run_daytona(command, cwd, timeout)
    return run_local(command, cwd, timeout)


def _remote_python(script: str, args: list[str], timeout: int = 120) -> dict:
    """Run a python helper inside the remote sandbox."""
    return run(shlex.join(["python3", "-c", script, *args]), timeout=timeout)


def read_file_remote(path: str) -> dict:
    """Read a workspace file from the Daytona sandbox."""
    return _remote_python(_READ_SCRIPT, [str(path or "")])


def write_file_remote(path: str, content) -> dict:
    """Write a workspace file in the Daytona sandbox."""
    raw = content if isinstance(content, str) else str(content)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return _remote_python(_WRITE_SCRIPT, [str(path or ""), encoded])


def list_files_remote(prefix: str = "") -> dict:
    """List workspace files in the Daytona sandbox."""
    return _remote_python(_LIST_SCRIPT, [str(prefix or "")])


def mode() -> str:
    """Effective terminal mode: ``local`` or ``daytona``."""
    return _mode()


def status() -> dict:
    """Diagnostics for /health: which channel is wired and whether it looks usable."""
    configured = _mode() != "daytona" or bool(_daytona_api_key())
    return {
        "mode": _mode(),
        "workspace": _workspace(),
        "daytona": {
            "api_url": _daytona_api_url(),
            "api_key_set": bool(_daytona_api_key()),
            "sandbox_id": _daytona_sandbox_id(),
            "workdir": _daytona_workdir(),
        },
        "configured": configured,
    }
