# Workelate_CLI

Minimal agent: **goal in → shell/file actions on the workspace → outcome out.**
The only tool is the terminal — web access is done with `curl`/`wget` through it.

```
frantend/            static UI (optional)
backend/
  app/main.py        FastAPI entrypoint
  app/routes.py      /health  /files  /task
  app/schemas.py     request/response models
  app/agent.py       the loop plus final user-facing summarisation
  app/planner.py     picks the next action using the configured LLM
  app/executor.py    tools: run_command, read_file, write_file, list_files
  app/cli.py         the terminal: local subprocess OR Modal sandbox
  app/inspector.py   shrinks tool output for the next step
  app/llm.py         OpenAI-compatible client — API KEY LEFT EMPTY ON PURPOSE
```

## Run

```bash
cd backend
python3 -m venv .venv                      # once
.venv/bin/pip install -r requirements.txt  # once
cp .env.example .env
./run.sh                                   # 0.0.0.0:8000 with reload
```

`run.sh` uses `.venv/bin/uvicorn app.main:app`. You can also run
`.venv/bin/uvicorn main:app` from `backend/app`; the entrypoint inserts the
backend root on `sys.path` so package-relative imports still work.

```bash
curl localhost:8000/health
curl -X POST localhost:8000/task -H 'content-type: application/json' \
     -d '{"goal":"ls && git status","max_steps":3}'
```

## Frontend

The chat UI is served by the same backend at **http://localhost:8000/ui/**.

It sends each message to `POST /task`, shows a concise user-facing summary, and keeps
the actual commands, inputs, and raw terminal observations in the expandable execution log.
The final summary is generated from the execution trace; if synthesis fails, a deterministic
report is used instead. It can also be opened directly from
`frantend/index.html`; in that case it calls `http://localhost:8000` by default.
Override the API URL with `?api=http://127.0.0.1:8000` or the **API** button.

## Connect a Modal sandbox

The SDK is already in `requirements.txt`. Authenticate once per machine:

```bash
cd backend
.venv/bin/modal token set --profile=rnpvip685
.venv/bin/modal profile activate rnpvip685
```

Publish the named image (for example `main`) in the `rnpvip685` workspace. If
you have a Dockerfile:

```bash
.venv/bin/modal image build -t main . -e main
```

Then set in `backend/.env`:

```
CLI_MODE=modal
MODAL_PROFILE=rnpvip685
MODAL_ENVIRONMENT=main
MODAL_IMAGE=main
MODAL_APP_NAME=workelate-cli
MODAL_TIMEOUT=86400
MODAL_IDLE_TIMEOUT=
MODAL_WORKDIR=
```

Every `run_command`, `read_file`, `write_file`, and `list_files` then runs inside
that Modal sandbox. The sandbox is cached per process and recreated automatically
when it terminates. `MODAL_IMAGE` also accepts the dashboard URL
`https://modal.com/images/rnpvip685/main`; the workspace segment is dropped.
Modal sandboxes have outbound internet access, so use `curl`/`wget` through
`run_command` for web access.

With `CLI_MODE=local`, commands run in `WORKSPACE_DIR` on the same machine
instead — the agent still works offline.

## LLM

Configure 1–3 providers in `backend/.env` (any OpenAI-compatible endpoints).
The primary is tried first, then the numbered fallbacks:

```
# Primary
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=<your key>
LLM_MODEL=gpt-4o-mini

# Fallbacks (empty BASE_URL/MODEL reuse the primary ones)
LLM_2_API_KEY=<key>
LLM_2_BASE_URL=
LLM_2_MODEL=
LLM_3_API_KEY=<key>
LLM_3_BASE_URL=
LLM_3_MODEL=
```

If a provider fails after its retries (5xx/network) or returns a 4xx, the agent
tries the next one. An LLM key is required for natural-language requests. Without one, the agent refuses
to execute the request rather than treating an English sentence as a shell command.
This prevents accidental or confusing terminal execution. Optional: `LLM_TEMPERATURE=0.2`,
`LLM_TIMEOUT=120`, `LLM_RETRIES=2`.
