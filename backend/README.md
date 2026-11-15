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
| `app/cli.py` | the terminal: local subprocess **or** Daytona sandbox |
| `app/crawler.py` | read-only GitHub file listing (optional) |
| `app/inspector.py` | shrinks tool output before it goes back to the LLM |
| `app/llm.py` | OpenAI-compatible client, key empty by default |

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

## Connect a Daytona sandbox

Set in `backend/.env`:

```
CLI_MODE=daytona
DAYTONA_API_KEY=<project api key>
DAYTONA_API_URL=https://app.daytona.io/api
DAYTONA_SANDBOX_ID=raw-terml-test   # optional; empty = reuse a running one
DAYTONA_WORKDIR=/home/daytona
```

All `run_command`, `read_file`, `write_file`, and `list_files` calls execute in
that sandbox. Without `DAYTONA_SANDBOX_ID`, a running sandbox is reused or a new
one is created.

With `CLI_MODE=local`, commands run in `WORKSPACE_DIR` on the same machine — the
agent still works.

## LLM

`LLM_API_KEY` is empty by default. With no key the planner falls back to running
the goal itself as a single shell command, so the stack is testable offline.

```bash
LLM_API_KEY=... LLM_MODEL=gpt-4o-mini LLM_BASE_URL=https://api.openai.com/v1
```

## Env reference

| Var | Default | Meaning |
|---|---|---|
| `CLI_MODE` | `local` | `local` or `daytona` |
| `DAYTONA_API_KEY` | – | Daytona project API key |
| `DAYTONA_API_URL` | `https://app.daytona.io/api` | Daytona API base URL |
| `DAYTONA_SANDBOX_ID` | – | sandbox id/name to reuse (empty = running/new) |
| `DAYTONA_WORKDIR` | `/home/daytona` | cwd inside the sandbox |
| `WORKSPACE_DIR` | cwd | command cwd / file sandbox root |
| `AGENT_MAX_STEPS` | `6` | loop cap |
| `GITHUB_TOKEN` | – | optional repo listing |
| `GITHUB_REPO` | – | `owner/name` |
| `GITHUB_BRANCH` | `main` | branch to list |
