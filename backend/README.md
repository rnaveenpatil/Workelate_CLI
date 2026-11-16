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
| `app/executor.py` | runs `run_command`, `read_file`, `write_file`, `list_files` |
| `app/cli.py` | the terminal: local subprocess **or** Modal sandbox |
| `app/inspector.py` | shrinks tool output before it goes back to the LLM |
| `app/llm.py` | OpenAI-compatible client, key empty by default |

## Tools

The planner can only use the terminal/file surface:
`run_command`, `read_file`, `write_file`, `list_files`, and `done`.

There is no separate web tool. Web pages and APIs are accessed through
`run_command` with `curl`/`wget`, e.g. `curl -sSL https://example.com` or
`curl -sS -o /dev/null -w '%{http_code}' https://api.github.com`.

## Run

```bash
cd backend
python3 -m venv .venv                      # once
.venv/bin/pip install -r requirements.txt  # once
cp .env.example .env
./run.sh                                   # 0.0.0.0:8000 with reload
```

Use `.venv/bin/uvicorn app.main:app` from `backend/`. Running
`.venv/bin/uvicorn main:app` from `backend/app` also works; the entrypoint adds
the backend root to `sys.path` for package-relative imports.

```bash
curl localhost:8000/health
curl -X POST localhost:8000/task -H 'content-type: application/json' \
     -d '{"goal":"git status && ls","max_steps":3}'
```

Chat UI: **http://localhost:8000/ui/** (files in `../frantend/`). It calls
`POST /task` and renders the agent's conclusion plus the execution log.

## Connect a Modal sandbox

Authenticate once per machine (writes the token to `~/.modal.toml`):

```bash
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

All `run_command`, `read_file`, `write_file`, and `list_files` calls execute in
that sandbox. It is cached per process and recreated automatically when it
terminates. `MODAL_IMAGE` also accepts the dashboard URL
`https://modal.com/images/rnpvip685/main`; only the image name is used. Modal
sandboxes have outbound internet access, so web access goes through
`curl`/`wget` in `run_command`.

### Modal account fallbacks

Add up to four accounts. The next account is tried automatically when the
active account hits a usage/rate limit, loses auth, its sandbox cannot be
created/executed, or its first command fails (a broken terminal). Once an
account has run a command successfully, ordinary command failures stay on that
account so the agent can inspect them:

```
MODAL_2_TOKEN_ID=
MODAL_2_TOKEN_SECRET=
# or a profile from ~/.modal.toml
MODAL_2_PROFILE=
MODAL_3_TOKEN_ID=
MODAL_3_TOKEN_SECRET=
MODAL_4_TOKEN_ID=
MODAL_4_TOKEN_SECRET=
```

All accounts share the global `MODAL_ENVIRONMENT`, `MODAL_IMAGE`,
`MODAL_APP_NAME`, and `MODAL_WORKDIR`; only the credentials change. `/health`
reports the configured accounts and which one is active.

If a fallback account has no named image (`main`), the backend creates a
`debian_slim` image with git/curl/wget/python and uses that account's default
workdir. Override it with `MODAL_FALLBACK_IMAGE=debian_slim` or a registry image
such as `python:3.12-slim`.

With `CLI_MODE=local`, commands run in `WORKSPACE_DIR` on the same machine — the
agent still works.

## LLM

Configured entirely through `backend/.env` (any OpenAI-compatible endpoint).
The primary is tried first, then `LLM_2_*` through `LLM_4_*` fallbacks in order:

```
# Primary
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=<your key>
LLM_MODEL=gpt-4o-mini

# Fallback 2 (empty BASE_URL/MODEL reuse the primary ones)
LLM_2_API_KEY=<key>
LLM_2_BASE_URL=
LLM_2_MODEL=

# Fallback 3
LLM_3_API_KEY=<key>
LLM_3_BASE_URL=
LLM_3_MODEL=

# Fallback 4
LLM_4_API_KEY=<key>
LLM_4_BASE_URL=
LLM_4_MODEL=

LLM_TEMPERATURE=0.2     # optional
LLM_TIMEOUT=120         # optional
LLM_RETRIES=2           # optional
```

If a provider exhausts its retries (5xx/network) or replies with a 4xx, the next
provider is tried. With no keys the planner falls back to running the goal itself
as a single shell command. `/health` lists each configured provider's base URL
and model without exposing keys.

## Env reference

| Var | Default | Meaning |
|---|---|---|
| `CLI_MODE` | `local` | `local` or `modal` |
| `MODAL_PROFILE` | – | profile written by `modal token set` |
| `MODAL_TOKEN_ID` | – | token id (optional when `~/.modal.toml` exists) |
| `MODAL_TOKEN_SECRET` | – | token secret (optional when `~/.modal.toml` exists) |
| `MODAL_ENVIRONMENT` | `main` | Modal environment for the app and image |
| `MODAL_IMAGE` | `main` | named image (dashboard URL also accepted) |
| `MODAL_APP_NAME` | `workelate-cli` | Modal app that owns the sandbox |
| `MODAL_TIMEOUT` | `86400` | sandbox lifetime (seconds) |
| `MODAL_IDLE_TIMEOUT` | – | shutdown after this many idle seconds |
| `MODAL_WORKDIR` | image default | cwd for commands and file tools |
| `MODAL_FALLBACK_IMAGE` | `debian_slim` | image for accounts missing the named image |
| `MODAL_2_TOKEN_ID` | – | fallback 2 Modal token id |
| `MODAL_2_TOKEN_SECRET` | – | fallback 2 Modal token secret |
| `MODAL_2_PROFILE` | – | fallback 2 profile name (alternative to tokens) |
| `MODAL_3_*`, `MODAL_4_*` | – | additional Modal accounts (same keys) |
| `WORKSPACE_DIR` | cwd | command cwd / file sandbox root |
| `AGENT_MAX_STEPS` | `6` | loop cap |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | primary API base |
| `LLM_API_KEY` | – | primary API key (empty = no LLM) |
| `LLM_MODEL` | `gpt-4o-mini` | primary model id |
| `LLM_2_API_KEY` | – | fallback 2 API key |
| `LLM_2_BASE_URL` | primary base | fallback 2 base URL |
| `LLM_2_MODEL` | primary model | fallback 2 model id |
| `LLM_3_API_KEY` | – | fallback 3 API key |
| `LLM_3_BASE_URL` | primary base | fallback 3 base URL |
| `LLM_3_MODEL` | primary model | fallback 3 model id |
| `LLM_4_API_KEY` | – | fallback 4 API key |
| `LLM_4_BASE_URL` | primary base | fallback 4 base URL |
| `LLM_4_MODEL` | primary model | fallback 4 model id |
| `LLM_TEMPERATURE` | `0.2` | planner sampling temperature |
| `LLM_TIMEOUT` | `120` | request timeout (seconds) |
| `LLM_RETRIES` | `2` | retries per provider on 5xx/network errors |
