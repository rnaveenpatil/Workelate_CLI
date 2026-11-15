"""cli_server.py - remote terminal server for Workelate_CLI.

Run this INSIDE the GitHub workspace / Codespace whose terminal you want the
agent to reach:

    pip install fastapi uvicorn
    CLI_TOKEN=change-me uvicorn cli_server:app --host 0.0.0.0 --port 8001

Then point the agent at it (backend/.env):

    CLI_MODE=remote
    CLI_REMOTE_URL=https://<codespace>-8001.app.github.dev
    CLI_TOKEN=change-me

Endpoints:
    GET  /health          -> liveness
    POST /exec            -> {"command","cwd","timeout"} -> stdout/stderr/exit_code
"""

import os
import subprocess

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

TOKEN = os.getenv("CLI_TOKEN", "")
WORKDIR = os.getenv("WORKSPACE_DIR", os.getcwd())
MAX_OUTPUT = 20000

app = FastAPI(title="Workelate_CLI remote terminal", version="1.0")


class ExecRequest(BaseModel):
    command: str = Field(..., min_length=1)
    cwd: str | None = None
    timeout: int = Field(120, ge=1, le=1800)


def _auth(token: str | None) -> None:
    if not TOKEN:
        return  # no token configured -> open (use only on a private workspace)
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing X-CLI-Token")


def _clip(text: str) -> str:
    if text and len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + f"\n...[truncated {len(text) - MAX_OUTPUT} chars]"
    return text or ""


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "workelate-cli-remote", "workdir": WORKDIR}


@app.post("/exec")
def exec_command(req: ExecRequest, x_cli_token: str | None = Header(default=None)) -> dict:
    _auth(x_cli_token)
    cwd = req.cwd or WORKDIR
    if not os.path.isdir(cwd):
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"cwd not found: {cwd}"}
    try:
        proc = subprocess.run(
            req.command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=req.timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": _clip(proc.stdout),
            "stderr": _clip(proc.stderr),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"timeout after {req.timeout}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8001")))
