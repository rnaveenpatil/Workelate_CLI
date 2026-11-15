# Workelate_CLI

Minimal agent: **goal in → shell/file actions on the workspace → outcome out.**

```
frantend/            static UI (optional)
backend/
  app/main.py        FastAPI entrypoint
  app/routes.py      /health  /files  /task
  app/schemas.py     request/response models
  app/agent.py       the loop: plan -> execute -> observe
  app/planner.py     picks next action (LLM if key set, else 1-step command)
  app/executor.py    tools: run_command, read_file, write_file, list_files, http_request, fetch_web
  app/cli.py         the terminal: local subprocess OR Daytona sandbox
  app/crawler.py     read-only GitHub file listing (optional)
  app/inspector.py   shrinks tool output for the next step
  app/llm.py         OpenAI-compatible client — API KEY LEFT EMPTY ON PURPOSE
```

## Run

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env          # keys stay empty
uvicorn app.main:app --reload --port 8000
```

```bash
curl localhost:8000/health
curl -X POST localhost:8000/task -H 'content-type: application/json' \
     -d '{"goal":"ls && git status","max_steps":3}'
```

## Connect a Daytona sandbox

Install the SDK (already in `requirements.txt`) and set in `backend/.env`:

```
CLI_MODE=daytona
DAYTONA_API_KEY=<project api key>
DAYTONA_API_URL=https://app.daytona.io/api
DAYTONA_SANDBOX_ID=raw-terml-test   # optional; empty = reuse a running one
DAYTONA_WORKDIR=/home/daytona
```

Every `run_command`, `read_file`, `write_file`, and `list_files` then runs inside
that Daytona sandbox. When `DAYTONA_SANDBOX_ID` is empty the backend reuses a
running sandbox or creates a new one.

With `CLI_MODE=local`, commands run in `WORKSPACE_DIR` on the same machine
instead — the agent still works offline.

## LLM

`LLM_API_KEY` is empty by default. With no key the planner runs the goal itself as
a single shell command, so the stack is testable offline. To enable it later:

```
LLM_API_KEY=<your key>
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```
