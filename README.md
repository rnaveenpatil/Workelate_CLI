# Workelate_CLI

Minimal agent: **goal in → shell/file actions on the workspace → outcome out.**

```
frantend/            static UI (optional)
cli_server.py        remote terminal server (run INSIDE the GitHub workspace/codespace)
backend/
  app/main.py        FastAPI entrypoint
  app/routes.py      /health  /files  /task
  app/schemas.py     request/response models
  app/agent.py       the loop: plan -> execute -> observe
  app/planner.py     picks next action (LLM if key set, else 1-step command)
  app/executor.py    3 tools: run_command, read_file, write_file
  app/cli.py         the terminal: local subprocess OR remote HTTP
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

## Connect the GitHub workspace terminal

Inside the codespace whose terminal the agent should use:

```bash
pip install fastapi uvicorn
CLI_TOKEN=change-me uvicorn cli_server:app --host 0.0.0.0 --port 8001
```

Then in `backend/.env`:

```
CLI_MODE=remote
CLI_REMOTE_URL=https://<codespace>-8001.app.github.dev
CLI_TOKEN=change-me
```

With no remote set, `CLI_MODE=local` runs commands in `WORKSPACE_DIR` on the same
machine — the agent still works.

## LLM

`LLM_API_KEY` is empty by default. With no key the planner runs the goal itself as
a single shell command, so the stack is testable offline. To enable it later:

```
LLM_API_KEY=<your key>
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```
