"""Terminal tool for Workelate_CLI.

Two modes, chosen by env ``CLI_MODE``:
    local -> run commands with subprocess in WORKSPACE_DIR (default)
    modal -> run commands in a Modal sandbox via the SDK

Modal env:
    MODAL_TOKEN_ID=<token id>              # optional when ~/.modal.toml exists
    MODAL_TOKEN_SECRET=<token secret>      # optional when ~/.modal.toml exists
    MODAL_PROFILE=rnpvip685
    MODAL_ENVIRONMENT=main
    MODAL_IMAGE=main                       # named image, not a workspace path
    MODAL_APP_NAME=workelate-cli
    MODAL_WORKDIR=/workspace               # optional cwd inside the sandbox
    MODAL_TIMEOUT=86400                    # sandbox lifetime in seconds
    MODAL_IDLE_TIMEOUT=                    # optional idle shutdown in seconds

The sandbox is cached per process and recreated automatically when it has
terminated. Config is read on every call so ``.env`` edits take effect without
a restart.
"""

import base64
import importlib.util
import os
import posixpath
import shlex
import subprocess
import threading
from pathlib import Path

MAX_OUTPUT = 8000
LOCAL_TIMEOUT_CEILING = 1800

MODAL_DEFAULT_ENVIRONMENT = "main"
MODAL_DEFAULT_IMAGE = "main"
MODAL_DEFAULT_APP_NAME = "workelate-cli"
MODAL_DEFAULT_TIMEOUT = 86400

# Small helpers executed inside the sandbox (argv-only, no shell interpolation).
_READ_SCRIPT = (
    "import os,sys,pathlib\n"
    "root=os.getcwd()\n"
    "base=root if root.endswith(os.sep) else root+os.sep\n"
    "p=os.path.realpath(os.path.join(root,sys.argv[1]))\n"
    "if p!=root and not p.startswith(base):\n"
    "    sys.stderr.write('path outside workspace: '+sys.argv[1]);sys.exit(2)\n"
    "sys.stdout.write(pathlib.Path(p).read_text(encoding='utf-8',errors='replace'))\n"
)

_WRITE_SCRIPT = (
    "import base64,os,sys,pathlib\n"
    "root=os.getcwd()\n"
    "base=root if root.endswith(os.sep) else root+os.sep\n"
    "p=os.path.realpath(os.path.join(root,sys.argv[1]))\n"
    "if p!=root and not p.startswith(base):\n"
    "    sys.stderr.write('path outside workspace: '+sys.argv[1]);sys.exit(2)\n"
    "data=base64.b64decode(sys.argv[2])\n"
    "pathlib.Path(p).parent.mkdir(parents=True,exist_ok=True)\n"
    "pathlib.Path(p).write_bytes(data)\n"
    "print('wrote '+sys.argv[1]+' ('+str(len(data))+' bytes)')\n"
)

_LIST_SCRIPT = (
    "import os,sys\n"
    "prefix=sys.argv[1] if len(sys.argv)>1 else ''\n"
    "skip={'.git','__pycache__','.venv','node_modules','.mypy_cache','__modal'}\n"
    "out=[]\n"
    "for dp,dn,fn in os.walk('.'):\n"
    "    dn[:]=[d for d in dn if d not in skip]\n"
    "    for f in fn:\n"
    "        rel=os.path.relpath(os.path.join(dp,f),'.').replace(os.sep,'/')\n"
    "        if rel.startswith(prefix): out.append(rel)\n"
    "print('\\n'.join(sorted(out)[:200]))\n"
)

_MODAL_CACHE: dict = {}


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if text and len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text or ""


def _mode() -> str:
    return os.getenv("CLI_MODE", "local").strip().lower()


def _workspace() -> str:
    return os.getenv("WORKSPACE_DIR", "").strip() or os.getcwd()


def _modal_token_id() -> str:
    return os.getenv("MODAL_TOKEN_ID", "").strip()


def _modal_token_secret() -> str:
    return os.getenv("MODAL_TOKEN_SECRET", "").strip()


def _modal_profile() -> str:
    return os.getenv("MODAL_PROFILE", "").strip()


def _modal_environment() -> str:
    return os.getenv("MODAL_ENVIRONMENT", MODAL_DEFAULT_ENVIRONMENT).strip() or MODAL_DEFAULT_ENVIRONMENT


def _modal_app_name() -> str:
    return os.getenv("MODAL_APP_NAME", MODAL_DEFAULT_APP_NAME).strip() or MODAL_DEFAULT_APP_NAME


def _modal_workdir_env() -> str:
    return os.getenv("MODAL_WORKDIR", "").strip()


def _modal_timeout() -> int:
    try:
        return max(60, int(os.getenv("MODAL_TIMEOUT", str(MODAL_DEFAULT_TIMEOUT))))
    except (TypeError, ValueError):
        return MODAL_DEFAULT_TIMEOUT


def _modal_idle_timeout() -> int:
    raw = os.getenv("MODAL_IDLE_TIMEOUT", "").strip()
    if not raw:
        return 0
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 0


def _modal_image_ref() -> str:
    """Return the named-image reference passed to ``Image.from_name``.

    Accepts a plain name (``main``) or the dashboard URL shown by Modal
    (``https://modal.com/images/<workspace>/<name>``). The workspace segment
    is not part of a Modal image name, so it is dropped.
    """
    raw = os.getenv("MODAL_IMAGE", MODAL_DEFAULT_IMAGE).strip() or MODAL_DEFAULT_IMAGE
    if raw.startswith(("http://", "https://")):
        parts = [p for p in raw.split("?", 1)[0].split("#", 1)[0].split("/") if p]
        if "images" in parts:
            index = parts.index("images")
            tail = parts[index + 1 :]
            if tail:
                return tail[-1]
    if "/" in raw and not raw.startswith("/"):
        workspace = _modal_profile()
        head, _, tail = raw.partition("/")
        if tail and head and (not workspace or head == workspace):
            return tail
    return raw


def _modal_config_path() -> Path:
    configured = os.getenv("MODAL_CONFIG_PATH", "").strip()
    return Path(configured) if configured else Path.home() / ".modal.toml"


def _modal_token_configured() -> bool:
    if _modal_token_id() and _modal_token_secret():
        return True
    path = _modal_config_path()
    if not path.is_file():
        return False
    profile = _modal_profile()
    if not profile:
        return True
    try:
        return f"[{profile}]" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def modal_available() -> bool:
    """True when the backend interpreter can import the Modal SDK."""
    return importlib.util.find_spec("modal") is not None


def uses_remote() -> bool:
    """True when commands/files live in a Modal sandbox."""
    return _mode() == "modal"


def _reset_modal_cache() -> None:
    """Drop cached Modal client/sandbox (used by tests)."""
    _MODAL_CACHE.clear()


def _modal_signature() -> tuple:
    return (
        _modal_image_ref(),
        _modal_app_name(),
        _modal_environment(),
        _modal_timeout(),
        _modal_idle_timeout(),
        _modal_workdir_env(),
    )


def _modal_app():
    import modal

    signature = (_modal_app_name(), _modal_environment())
    if _MODAL_CACHE.get("app_signature") == signature and _MODAL_CACHE.get("app"):
        return _MODAL_CACHE["app"]
    app = modal.App.lookup(
        _modal_app_name(),
        create_if_missing=True,
        environment_name=_modal_environment() or None,
    )
    _MODAL_CACHE["app"] = app
    _MODAL_CACHE["app_signature"] = signature
    return app


def _read_stream(stream) -> str:
    try:
        return stream.read() or ""
    except Exception:  # noqa: BLE001
        return ""


def _read_process(process) -> tuple[str, str]:
    output: dict[str, str] = {}

    def drain(name: str, stream) -> None:
        output[name] = _read_stream(stream)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return output.get("stdout", ""), output.get("stderr", "")


def _probe_workdir(sandbox) -> str:
    process = sandbox.exec("pwd", timeout=30)
    stdout, _ = _read_process(process)
    process.wait()
    return stdout.strip()


def _create_modal_sandbox():
    import modal

    image = modal.Image.from_name(_modal_image_ref())
    app = _modal_app()
    timeout = _modal_timeout()
    kwargs: dict = {
        "image": image,
        "app": app,
        "timeout": timeout,
    }
    idle_timeout = _modal_idle_timeout()
    if idle_timeout:
        kwargs["idle_timeout"] = idle_timeout

    sandbox = modal.Sandbox.create("sleep", str(timeout), **kwargs)
    if not _modal_workdir_env():
        try:
            _MODAL_CACHE["default_workdir"] = _probe_workdir(sandbox)
        except Exception:  # noqa: BLE001  (pwd probe is best-effort)
            _MODAL_CACHE["default_workdir"] = ""
    return sandbox


def _modal_sandbox():
    signature = _modal_signature()
    sandbox = _MODAL_CACHE.get("sandbox")
    if sandbox is not None and _MODAL_CACHE.get("sandbox_signature") == signature:
        try:
            if sandbox.poll() is None:
                return sandbox
        except Exception:  # noqa: BLE001  (stale handle -> recreate)
            pass

    sandbox = _create_modal_sandbox()
    _MODAL_CACHE["sandbox"] = sandbox
    _MODAL_CACHE["sandbox_signature"] = signature
    return sandbox


def _modal_workdir(sandbox=None) -> str:
    configured = _modal_workdir_env()
    if configured:
        return configured
    if sandbox is not None and not _MODAL_CACHE.get("default_workdir"):
        try:
            _MODAL_CACHE["default_workdir"] = _probe_workdir(sandbox)
        except Exception:  # noqa: BLE001
            _MODAL_CACHE["default_workdir"] = ""
    return _MODAL_CACHE.get("default_workdir", "")


def _modal_cwd(sandbox, cwd: str | None) -> str | None:
    raw = str(cwd or "").strip()
    if raw:
        if raw.startswith("/"):
            return raw
        base = _modal_workdir(sandbox)
        if base:
            return posixpath.normpath(posixpath.join(base, raw))
        return None
    return _modal_workdir(sandbox) or None


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


def run_modal(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Run one command inside the Modal sandbox."""
    if not str(command).strip():
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "empty command"}

    timeout = max(1, int(timeout or 120))
    try:
        sandbox = _modal_sandbox()
        process = sandbox.exec(
            "bash",
            "-lc",
            str(command),
            timeout=timeout,
            workdir=_modal_cwd(sandbox, cwd),
        )
        stdout, stderr = _read_process(process)
        exit_code = int(process.wait())
    except ModuleNotFoundError as exc:
        if exc.name == "modal":
            message = (
                "backend runtime is missing the 'modal' package. This is not a "
                "sandbox problem: restart the backend with backend/.venv/bin/uvicorn "
                "(or ./run.sh) instead of another Python environment."
            )
        else:
            message = f"backend runtime is missing Python module {exc.name!r}"
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": message}
    except Exception as exc:  # noqa: BLE001  (auth, image, network, sandbox, timeout)
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"modal error: {exc}",
        }

    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout": _clip(stdout),
        "stderr": _clip(stderr),
    }


def run(command: str, cwd: str | None = None, timeout: int = 120) -> dict:
    """Run one shell command on the workspace terminal (local or Modal)."""
    if _mode() == "modal":
        return run_modal(command, cwd, timeout)
    return run_local(command, cwd, timeout)


def _remote_python(script: str, args: list[str], timeout: int = 120) -> dict:
    """Run a python helper inside the remote sandbox."""
    return run(shlex.join(["python3", "-c", script, *args]), timeout=timeout)


def read_file_remote(path: str) -> dict:
    """Read a workspace file from the Modal sandbox."""
    return _remote_python(_READ_SCRIPT, [str(path or "")])


def write_file_remote(path: str, content) -> dict:
    """Write a workspace file in the Modal sandbox."""
    raw = content if isinstance(content, str) else str(content)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return _remote_python(_WRITE_SCRIPT, [str(path or ""), encoded])


def list_files_remote(prefix: str = "") -> dict:
    """List workspace files in the Modal sandbox."""
    return _remote_python(_LIST_SCRIPT, [str(prefix or "")])


def mode() -> str:
    """Effective terminal mode: ``local`` or ``modal``."""
    return _mode()


def status() -> dict:
    """Diagnostics for /health: which channel is wired and whether it looks usable."""
    installed = modal_available()
    configured = _mode() != "modal" or (_modal_token_configured() and installed)
    sandbox = _MODAL_CACHE.get("sandbox")
    return {
        "mode": _mode(),
        "workspace": _workspace(),
        "modal": {
            "profile": _modal_profile(),
            "environment": _modal_environment(),
            "image": _modal_image_ref(),
            "app": _modal_app_name(),
            "token_id_set": bool(_modal_token_id()),
            "token_secret_set": bool(_modal_token_secret()),
            "dependency_installed": installed,
            "workdir": _modal_workdir_env(),
            "sandbox_id": str(getattr(sandbox, "object_id", "") or "") if sandbox else "",
        },
        "configured": configured,
    }
