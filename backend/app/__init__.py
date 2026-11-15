"""Workelate_CLI backend package.

Loads ``backend/.env`` before any submodule reads ``os.environ``, so modules that
snapshot configuration at import time (cli, executor, llm, crawler) actually see
the user's values.
"""

import os
from pathlib import Path

_ENV_LOADED = False


def _load_manual(path: Path) -> None:
    """Minimal KEY=VALUE parser used when python-dotenv is unavailable."""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_env() -> None:
    """Load the first .env found next to the backend package or in the CWD."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
        Path.cwd() / "backend" / ".env",
    ]

    try:  # python-dotenv is optional
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001
        load_dotenv = None

    for path in candidates:
        if not path.is_file():
            continue
        if load_dotenv is not None:
            load_dotenv(path, override=False)
        else:
            _load_manual(path)
        break


load_env()
