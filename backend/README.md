# Workelate_CLI — backend

Minimal agent: **goal in → shell/file actions on the workspace → outcome out.**

## Layout

| File | Role |
|---|---|
| `app/main.py` | FastAPI entrypoint (`uvicorn app.main:app`) |
| `app/routes.py` | `/health`, `/files`, `/task` |
| `app/schemas.py` | request/response models |
| `app/agent.py` | the loop: plan → execute → observe, ±`AGENT_MAX_STEPS` |
| `app/planner.py` | picks the next action (LLM, or a 1-step rule fallback) |
| `app/executor.py` | runs `run_command`, `read_file`, `write_file`, `list_files`, `http_request`, `fetch_web` |
| `app/cli.py` | the terminal: local subprocess **or** remote HTTP |
| `app/crawler.py` | read-only GitHub file listing (optional) |
| `app/inspector.py` | shrinks tool output before it goes back to the LLM |
| `app/llm.py` | OpenAI-compatible client, key empty by default |

`cli_server.py` (repo root) is the **remote terminal** that runs inside the
GitHub workspace.

## Run

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env        # keys are empty on purpose
uvicorn app.main:app --reload --port 8000
```

```bash
curl localhost:8000/health
curl -X POST localhost:8000/task -H 'content-type: application/json' \
     -d '{"goal":"git status && ls","max_steps":3}'
```

## Connect the GitHub workspace terminal

1. In the Codespace, expose the terminal server:
   ```bash
   pip install fastapi uvicorn
   CLI_TOKEN=change-me uvicorn cli_server:app --host 0.0.0.0 --port 8000
   # port 8000 is private by default
   ```
2. In `backend/.env`:
   ```
   CLI_MODE=remote
   CLI_REMOTE_URL=https://<codespace>-8000.app.github.dev
   CLI_TOKEN=change-me
   ```
3. For a **private** port, authenticate the tunnel with the codespace's own token
   (`echo $GITHUB_TOKEN` inside the codespace, starts with `ghu_`):
   ```
   CLI_GITHUB_TOKEN=<ghu_...>
   ```
   Sent as `X-Github-Token`. Alternatively make port 8000 public in the Ports tab.

Without these, `CLI_MODE=local` runs commands in `WORKSPACE_DIR` on the same
machine — the agent still works.

## LLM

`LLM_API_KEY` is empty by default. With no key the planner falls back to running
the goal itself as a single shell command, so the stack is testable offline.

```bash
LLM_API_KEY=... LLM_MODEL=gpt-4o-mini LLM_BASE_URL=https://api.openai.com/v1
```

## Env reference

| Var | Default | Meaning |
|---|---|---|
| `CLI_MODE` | `local` | `local` or `remote` |
| `CLI_REMOTE_URL` | – | remote terminal base URL |
| `CLI_TOKEN` | – | shared secret (`X-CLI-Token`) |
| `CLI_GITHUB_TOKEN` | – | codespace `GITHUB_TOKEN` for private ports (`X-Github-Token`) |
| `CLI_REMOTE_TIMEOUT` | `45` | extra seconds over the command timeout |
| `SANDBOX_CONTAINER` | – | Docker container for isolated remote tasks (empty = off) |
| `SANDBOX_WORKDIR` | `/workspace` | cwd inside the sandbox container |
| `SANDBOX_PYTHON` | `python3` | interpreter used by the sandbox file tools |
| `SANDBOX_USER` | – | `docker exec --user` value (e.g. `1000:1000`) |
| `WORKSPACE_DIR` | cwd | command cwd / file sandbox root |
| `AGENT_MAX_STEPS` | `6` | loop cap |
| `GITHUB_TOKEN` | – | optional repo listing |
| `GITHUB_REPO` | – | `owner/name` |
| `GITHUB_BRANCH` | `main` | branch to list |
