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
    MODAL_FALLBACK_IMAGE=debian_slim       # image used when a named image is
                                           # missing in a fallback account

Fallback accounts (tried in order when the primary account hits a limit, loses
auth, or the sandbox cannot be created/executed):
    MODAL_2_TOKEN_ID / MODAL_2_TOKEN_SECRET / MODAL_2_PROFILE
    MODAL_3_* / MODAL_4_*
All accounts share the global MODAL_ENVIRONMENT, MODAL_IMAGE, MODAL_APP_NAME,
and MODAL_WORKDIR; only the credentials change.

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
MODAL_DEFAULT_FALLBACK_IMAGE = "debian_slim"
MAX_MODAL_ACCOUNTS = 4

MODAL_FALLBACK_APT_PACKAGES = (
    "git",
    "curl",
    "wget",
    "ca-certificates",
    "build-essential",
    "python3",
    "python3-pip",
    "jq",
    "ripgrep",
)

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


def _normalize_image_ref(raw: str, profile: str = "") -> str:
    """Return the named-image reference passed to ``Image.from_name``.

    Accepts a plain name (``main``) or the dashboard URL shown by Modal
    (``https://modal.com/images/<workspace>/<name>``). The workspace segment
    is not part of a Modal image name, so it is dropped.
    """
    raw = (raw or "").strip() or MODAL_DEFAULT_IMAGE
    if raw.startswith(("http://", "https://")):
        parts = [p for p in raw.split("?", 1)[0].split("#", 1)[0].split("/") if p]
        if "images" in parts:
            index = parts.index("images")
            tail = parts[index + 1 :]
            if tail:
                return tail[-1]
    if "/" in raw and not raw.startswith("/"):
        workspace = profile
        head, _, tail = raw.partition("/")
        if tail and head and (not workspace or head == workspace):
            return tail
    return raw


def _modal_image_ref() -> str:
    return _normalize_image_ref(
        os.getenv("MODAL_IMAGE", MODAL_DEFAULT_IMAGE), _modal_profile()
    )


def _modal_config_path() -> Path:
    configured = os.getenv("MODAL_CONFIG_PATH", "").strip()
    return Path(configured) if configured else Path.home() / ".modal.toml"


def _profile_credentials(profile: str) -> tuple[str, str] | None:
    """Read a profile's token pair from the Modal TOML config, if present."""
    if not profile:
        return None
    path = _modal_config_path()
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001  (missing file, bad TOML, old Python)
        return None
    section = data.get(profile)
    if not isinstance(section, dict):
        return None
    token_id = str(section.get("token_id") or "").strip()
    token_secret = str(section.get("token_secret") or "").strip()
    if token_id and token_secret:
        return token_id, token_secret
    return None


def _modal_accounts() -> list[dict]:
    """Configured Modal accounts: primary first, then numbered fallbacks.

    Primary:   MODAL_TOKEN_ID / MODAL_TOKEN_SECRET / MODAL_PROFILE
    Fallbacks: MODAL_2_* ... MODAL_4_* (MODAL_1_* is accepted as an alias)

    An account needs either a token pair or a profile. Fallback accounts without
    credentials are skipped; the primary is always kept so the SDK can still use
    its ambient (~/.modal.toml) configuration.

    Environment, image, app name, and workdir are global: every account uses the
    same ``MODAL_ENVIRONMENT`` / ``MODAL_IMAGE`` / ``MODAL_APP_NAME`` /
    ``MODAL_WORKDIR`` and only the credentials change.
    """
    found: list[dict] = []
    environment = _modal_environment()
    image = _normalize_image_ref(os.getenv("MODAL_IMAGE", ""), _modal_profile())
    app_name = _modal_app_name()
    workdir = _modal_workdir_env()

    def add(
        name: str,
        token_id: str,
        token_secret: str,
        profile: str,
        *,
        primary: bool = False,
    ) -> None:
        token_id = token_id.strip()
        token_secret = token_secret.strip()
        profile = profile.strip()

        if token_id and token_secret:
            pass
        elif profile:
            credentials = _profile_credentials(profile)
            if credentials:
                token_id, token_secret = credentials
            elif not primary:
                return
        elif not primary:
            return

        signature = (token_id, token_secret, profile)
        if any(item["signature"] == signature for item in found):
            return
        found.append(
            {
                "name": name,
                "token_id": token_id,
                "token_secret": token_secret,
                "profile": profile,
                "environment": environment,
                "image": image,
                "app_name": app_name,
                "workdir": workdir,
                "signature": signature,
            }
        )

    add(
        "primary",
        _modal_token_id(),
        _modal_token_secret(),
        _modal_profile(),
        primary=True,
    )
    for index in range(1, MAX_MODAL_ACCOUNTS + 1):
        add(
            f"MODAL_{index}",
            os.getenv(f"MODAL_{index}_TOKEN_ID", ""),
            os.getenv(f"MODAL_{index}_TOKEN_SECRET", ""),
            os.getenv(f"MODAL_{index}_PROFILE", ""),
        )
    return found


def _modal_token_configured() -> bool:
    for account in _modal_accounts():
        if account["token_id"] and account["token_secret"]:
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


def _account_state(account: dict | None) -> dict:
    name = account["name"] if account else "primary"
    return _MODAL_CACHE.setdefault("accounts", {}).setdefault(name, {})


def _active_account(accounts: list[dict] | None = None) -> dict | None:
    accounts = _modal_accounts() if accounts is None else accounts
    if not accounts:
        return None
    active = _MODAL_CACHE.get("active_account")
    for account in accounts:
        if account["name"] == active:
            return account
    return accounts[0]


def _ordered_accounts(accounts: list[dict]) -> list[dict]:
    """Try the last working account first, then the remaining ones in order."""
    active = _active_account(accounts)
    if active is None:
        return accounts
    index = accounts.index(active)
    return accounts[index:] + accounts[:index]


def _modal_signature(account: dict | None = None) -> tuple:
    account = account or _active_account()
    if account is None:
        return (
            _modal_image_ref(),
            _modal_app_name(),
            _modal_environment(),
            _modal_timeout(),
            _modal_idle_timeout(),
            _modal_workdir_env(),
        )
    return (
        account["image"],
        account["app_name"],
        account["environment"],
        _modal_timeout(),
        _modal_idle_timeout(),
        account["workdir"],
        account["token_id"],
        account["token_secret"],
        account["profile"],
    )


def _modal_client(account: dict):
    """Explicit Modal client for an account, or ``None`` for ambient config."""
    import modal

    cache = _MODAL_CACHE.setdefault("clients", {})
    signature = (account["token_id"], account["token_secret"], account["profile"])
    cached = cache.get(account["name"])
    if cached and cached[0] == signature:
        return cached[1]

    client = None
    if account["token_id"] and account["token_secret"]:
        client = modal.Client.from_credentials(
            account["token_id"], account["token_secret"]
        )
    cache[account["name"]] = (signature, client)
    return client


def _modal_app(account: dict | None = None):
    import modal

    account = account or _active_account()
    if account is None:
        return None
    signature = (
        account["app_name"],
        account["environment"],
        account["token_id"],
        account["token_secret"],
        account["profile"],
    )
    state = _account_state(account)
    if state.get("app_signature") == signature and state.get("app"):
        return state["app"]

    kwargs = {
        "create_if_missing": True,
        "environment_name": account["environment"] or None,
    }
    client = _modal_client(account)
    if client is not None:
        kwargs["client"] = client
    app = modal.App.lookup(account["app_name"], **kwargs)
    state["app"] = app
    state["app_signature"] = signature
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


def _fallback_image():
    """Build a capable default image for accounts without a named image."""
    import modal

    raw = (
        os.getenv("MODAL_FALLBACK_IMAGE", "").strip()
        or MODAL_DEFAULT_FALLBACK_IMAGE
    )
    if raw in ("debian_slim", "default"):
        image = modal.Image.debian_slim()
    else:
        image = modal.Image.from_registry(raw)
    return image.apt_install(*MODAL_FALLBACK_APT_PACKAGES)


def _missing_named_image(exc: Exception) -> bool:
    text = str(exc).lower()
    if "image" not in text:
        return False
    return any(
        marker in text
        for marker in ("not found", "does not exist", "no such", "unknown")
    )


def _launch_modal_sandbox(account: dict, image, client, image_kind: str = "named"):
    import modal

    app = _modal_app(account)
    timeout = _modal_timeout()
    kwargs: dict = {
        "image": image,
        "app": app,
        "timeout": timeout,
    }
    if client is not None:
        kwargs["client"] = client
    idle_timeout = _modal_idle_timeout()
    if idle_timeout:
        kwargs["idle_timeout"] = idle_timeout

    sandbox = modal.Sandbox.create("sleep", str(timeout), **kwargs)
    state = _account_state(account)
    state["image_kind"] = image_kind
    if image_kind == "fallback" or not account["workdir"]:
        try:
            state["default_workdir"] = _probe_workdir(sandbox)
        except Exception:  # noqa: BLE001  (pwd probe is best-effort)
            state["default_workdir"] = ""
    return sandbox


def _create_modal_sandbox(account: dict | None = None):
    import modal

    account = account or _active_account()
    if account is None:
        account = _modal_accounts()[0]
    client = _modal_client(account)
    image_kwargs = {"client": client} if client is not None else {}
    try:
        image = modal.Image.from_name(account["image"], **image_kwargs)
        return _launch_modal_sandbox(account, image, client)
    except Exception as exc:  # noqa: BLE001
        if not _missing_named_image(exc):
            raise
    return _launch_modal_sandbox(
        account, _fallback_image(), client, image_kind="fallback"
    )


def _modal_sandbox(account: dict | None = None):
    account = account or _active_account()
    if account is None:
        account = _modal_accounts()[0]
    signature = _modal_signature(account)
    state = _account_state(account)
    sandbox = state.get("sandbox")
    if sandbox is not None and state.get("signature") == signature:
        try:
            if sandbox.poll() is None:
                return sandbox
        except Exception:  # noqa: BLE001  (stale handle -> recreate)
            pass

    state.pop("exec_ok", None)
    sandbox = _create_modal_sandbox(account)
    state["sandbox"] = sandbox
    state["signature"] = signature
    return sandbox


def _modal_workdir(account: dict | None = None, sandbox=None) -> str:
    account = account or _active_account()
    state = _account_state(account)
    configured = (account or {}).get("workdir") or _modal_workdir_env()
    if configured and state.get("image_kind", "named") != "fallback":
        return configured
    if sandbox is not None and not state.get("default_workdir"):
        try:
            state["default_workdir"] = _probe_workdir(sandbox)
        except Exception:  # noqa: BLE001
            state["default_workdir"] = ""
    return state.get("default_workdir", "")


def _modal_cwd(account: dict | None, sandbox, cwd: str | None) -> str | None:
    raw = str(cwd or "").strip()
    if raw:
        if raw.startswith("/"):
            return raw
        base = _modal_workdir(account, sandbox)
        if base:
            return posixpath.normpath(posixpath.join(base, raw))
        return None
    return _modal_workdir(account, sandbox) or None


def _invalidate_account(account: dict) -> None:
    """Forget a failed account's client/sandbox so the next call reconnects."""
    state = _MODAL_CACHE.get("accounts", {}).get(account["name"])
    if state:
        state.pop("sandbox", None)
        state.pop("app", None)
        state.pop("exec_ok", None)
        state.pop("image_kind", None)
        state.pop("default_workdir", None)
    _MODAL_CACHE.get("clients", {}).pop(account["name"], None)


# Account/terminal-level errors that can show up as a normal process result
# (non-zero exit + message) instead of raising from the Modal SDK.
_MODAL_TERMINAL_FAILURE_MARKERS = (
    "modal error",
    "modal exception",
    "resource exhausted",
    "resource_exhausted",
    "resourceexhausted",
    "rate limit",
    "rate-limit",
    "too many requests",
    "usage limit",
    "quota exceeded",
    "quota reached",
    "limit reached",
    "workspace limit",
    "insufficient credit",
    "out of credit",
    "billing",
    "authentication failed",
    "unauthorized",
    "invalid token",
    "token expired",
    "not authorized",
    "sandbox is not running",
    "sandbox terminated",
    "sandbox closed",
    "sandbox died",
    "sandbox unavailable",
    "sandbox failed",
)


def _terminal_failure(exit_code: int, stdout: str, stderr: str) -> bool:
    """True when a failed result looks like the terminal itself is unusable."""
    if exit_code == 0:
        return False
    text = f"{stdout}\n{stderr}".lower()
    return any(marker in text for marker in _MODAL_TERMINAL_FAILURE_MARKERS)


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
    """Run one command inside a Modal sandbox, failing over between accounts.

    Each configured account (primary, then ``MODAL_2`` ... ``MODAL_4``) is tried
    in turn. Account-level failures - auth errors, rate/usage limits, sandbox
    quota, or an unreachable sandbox - move on to the next account instead of
    failing the whole command.
    """
    if not str(command).strip():
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "empty command"}

    timeout = max(1, int(timeout or 120))
    errors: list[str] = []
    last_result: dict | None = None

    for account in _ordered_accounts(_modal_accounts()):
        state = _account_state(account)
        try:
            sandbox = _modal_sandbox(account)
            process = sandbox.exec(
                "bash",
                "-lc",
                str(command),
                timeout=timeout,
                workdir=_modal_cwd(account, sandbox, cwd),
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
        except Exception as exc:  # noqa: BLE001  (auth, limit, network, sandbox, timeout)
            _invalidate_account(account)
            errors.append(f"{account['name']}: {exc}")
            continue

        result = {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "stdout": _clip(stdout),
            "stderr": _clip(stderr),
        }
        if exit_code == 0:
            state["exec_ok"] = True
            _MODAL_CACHE["active_account"] = account["name"]
            return result

        # A sandbox that has never run a command successfully is treated as a
        # broken terminal, so any failure moves on to the next account. Once an
        # account has worked, only terminal/account-level errors trigger failover
        # (normal command failures are results the agent must reason about).
        broken_terminal = _terminal_failure(exit_code, stdout, stderr)
        if broken_terminal or not state.get("exec_ok"):
            last_result = result
            detail = (stderr or stdout).strip()[:300] or f"exit {exit_code}"
            errors.append(f"{account['name']}: {detail}")
            _invalidate_account(account)
            continue

        _MODAL_CACHE["active_account"] = account["name"]
        return result

    if last_result is not None:
        return last_result

    detail = " | ".join(errors) or "no Modal account is configured"
    return {
        "ok": False,
        "exit_code": -1,
        "stdout": "",
        "stderr": f"modal error: all accounts failed: {detail}",
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
    accounts = _modal_accounts()
    active = _active_account(accounts)
    sandbox = _account_state(active).get("sandbox") if active else None
    usable_accounts = [
        account
        for account in accounts
        if account["token_id"] and account["token_secret"]
    ]
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
            "account_count": len(usable_accounts),
            "active_account": active["name"] if active else "",
            "accounts": [
                {
                    "name": account["name"],
                    "profile": account["profile"],
                    "environment": account["environment"],
                    "image": account["image"],
                    "token_id_set": bool(account["token_id"]),
                    "token_secret_set": bool(account["token_secret"]),
                }
                for account in accounts
            ],
        },
        "configured": configured,
    }
