"""Tests for the planner / executor / terminal layer.

Run:  cd backend && python -m pytest -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, cli, executor, inspector, llm, planner  # noqa: E402


@pytest.fixture(autouse=True)
def _local_terminal(monkeypatch):
    """Keep tests hermetic: never talk to the real remote terminal or LLM."""
    monkeypatch.setenv("CLI_MODE", "local")
    monkeypatch.delenv("SANDBOX_CONTAINER", raising=False)
    cli._reset_modal_cache()
    for prefix in ("", "_1", "_2", "_3", "_4"):
        for suffix in ("API_KEY", "BASE_URL", "MODEL"):
            monkeypatch.delenv(f"LLM{prefix}_{suffix}", raising=False)


# ------------------------------- llm config --------------------------------


def test_llm_reads_base_url_key_model_from_env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1/")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o-mini")
    assert llm.base_url() == "https://openrouter.ai/api/v1"
    assert llm.api_key() == "sk-test"
    assert llm.model() == "openai/gpt-4o-mini"
    assert llm.status() == {
        "configured": True,
        "api_key_set": True,
        "model": "openai/gpt-4o-mini",
        "base_url": "https://openrouter.ai/api/v1",
        "provider_count": 1,
        "providers": [
            {
                "name": "primary",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "openai/gpt-4o-mini",
                "api_key_set": True,
            }
        ],
    }


def test_llm_empty_base_url_and_model_fall_back(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "  ")
    assert llm.base_url() == llm.DEFAULT_BASE_URL
    assert llm.model() == llm.DEFAULT_MODEL


def test_llm_providers_inherit_primary_base_and_model(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "key-1")
    monkeypatch.setenv("LLM_MODEL", "model-1")
    monkeypatch.setenv("LLM_2_BASE_URL", "https://second.example/v1")
    monkeypatch.setenv("LLM_2_API_KEY", "key-2")
    monkeypatch.setenv("LLM_2_MODEL", "model-2")
    monkeypatch.setenv("LLM_3_API_KEY", "key-3")

    found = llm.providers()
    assert [p["name"] for p in found] == ["primary", "LLM_2", "LLM_3"]
    assert found[1]["base_url"] == "https://second.example/v1"
    assert found[1]["model"] == "model-2"
    assert found[2]["base_url"] == "https://primary.example/v1"
    assert found[2]["model"] == "model-1"

    status = llm.status()
    assert status["provider_count"] == 3
    assert [p["name"] for p in status["providers"]] == ["primary", "LLM_2", "LLM_3"]
    assert "api_key" not in status["providers"][0]


def test_llm_providers_include_fourth_fallback(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "key-1")
    monkeypatch.setenv("LLM_MODEL", "model-1")
    for index, host in ((2, "second"), (3, "third"), (4, "fourth")):
        monkeypatch.setenv(f"LLM_{index}_BASE_URL", f"https://{host}.example/v1")
        monkeypatch.setenv(f"LLM_{index}_API_KEY", f"key-{index}")
        monkeypatch.setenv(f"LLM_{index}_MODEL", f"model-{index}")

    found = llm.providers()
    assert [p["name"] for p in found] == ["primary", "LLM_2", "LLM_3", "LLM_4"]
    assert found[3]["base_url"] == "https://fourth.example/v1"
    assert found[3]["model"] == "model-4"
    assert llm.status()["provider_count"] == 4


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _mock_two_providers(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "key-1")
    monkeypatch.setenv("LLM_MODEL", "model-1")
    monkeypatch.setenv("LLM_2_BASE_URL", "https://second.example/v1")
    monkeypatch.setenv("LLM_2_API_KEY", "key-2")
    monkeypatch.setenv("LLM_2_MODEL", "model-2")
    monkeypatch.setenv("LLM_RETRIES", "0")


def test_llm_falls_back_after_5xx(monkeypatch):
    _mock_two_providers(monkeypatch)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json["model"]))
        if "primary" in url:
            return _FakeResponse(503, {"error": {"message": "down"}})
        return _FakeResponse(200, {"choices": [{"message": {"content": "fallback-ok"}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "fallback-ok"
    assert calls == [
        ("https://primary.example/v1/chat/completions", "model-1"),
        ("https://second.example/v1/chat/completions", "model-2"),
    ]


def test_llm_falls_back_after_4xx(monkeypatch):
    _mock_two_providers(monkeypatch)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        if "primary" in url:
            return _FakeResponse(401, {"error": {"message": "bad key"}})
        return _FakeResponse(200, {"choices": [{"message": {"content": "second"}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "second"
    assert len(calls) == 2


def test_llm_falls_back_to_fourth_provider(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "key-1")
    monkeypatch.setenv("LLM_MODEL", "model-1")
    for index, host in ((2, "second"), (3, "third"), (4, "fourth")):
        monkeypatch.setenv(f"LLM_{index}_BASE_URL", f"https://{host}.example/v1")
        monkeypatch.setenv(f"LLM_{index}_API_KEY", f"key-{index}")
        monkeypatch.setenv(f"LLM_{index}_MODEL", f"model-{index}")
    monkeypatch.setenv("LLM_RETRIES", "0")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        if "fourth" in url:
            return _FakeResponse(200, {"choices": [{"message": {"content": "fourth-ok"}}]})
        return _FakeResponse(503, {"error": {"message": "down"}})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "fourth-ok"
    assert calls == [
        "https://primary.example/v1/chat/completions",
        "https://second.example/v1/chat/completions",
        "https://third.example/v1/chat/completions",
        "https://fourth.example/v1/chat/completions",
    ]


def test_llm_all_providers_fail_reports_each(monkeypatch):
    _mock_two_providers(monkeypatch)

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(429, {"error": {"message": "rate limit"}})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    with pytest.raises(RuntimeError) as exc:
        llm.chat([{"role": "user", "content": "hi"}])
    message = str(exc.value)
    assert message.startswith("all LLM providers failed")
    assert "primary (model-1)" in message
    assert "LLM_2 (model-2)" in message


# --------------------------- planner.parse_action ---------------------------


def test_parse_plain_json():
    a = planner.parse_action('{"tool":"run_command","command":"ls -la"}')
    assert a["tool"] == "run_command"
    assert a["command"] == "ls -la"


def test_parse_fenced_json_with_prose():
    raw = 'Sure!\n```json\n{"tool":"read_file","path":"README.md"}\n```\nDone.'
    a = planner.parse_action(raw)
    assert a["tool"] == "read_file"
    assert a["path"] == "README.md"


def test_parse_nested_braces_and_braces_in_strings():
    raw = '{"tool":"write_file","path":"a.json","content":"{\\"k\\": {\\"n\\": 1}}"   , "thought": "x"}'
    a = planner.parse_action(raw)
    assert a["tool"] == "write_file"
    assert a["content"] == '{"k": {"n": 1}}'


def test_parse_alias_and_alternate_key_names():
    assert planner.parse_action('{"tool":"shell","command":"pwd"}')["tool"] == "run_command"
    assert planner.parse_action('{"name":"list_files"}')["tool"] == "list_files"
    assert planner.parse_action('{"action":"finish","outcome":"ok"}')["tool"] == "done"


def test_parse_rejects_unknown_tool():
    with pytest.raises(planner.PlannerError):
        planner.parse_action('{"tool":"rm_rf_everything"}')


def test_parse_rejects_missing_required_fields():
    with pytest.raises(planner.PlannerError):
        planner.parse_action('{"tool":"run_command"}')
    with pytest.raises(planner.PlannerError):
        planner.parse_action('{"tool":"write_file","path":"a.txt"}')


def test_agent_toolset_is_terminal_only():
    assert planner.WORKSPACE_TOOLS == (
        "run_command",
        "read_file",
        "write_file",
        "list_files",
    )
    assert planner.TOOLS == (*planner.WORKSPACE_TOOLS, "respond", "clarify", "done")
    assert executor.TOOLS == planner.WORKSPACE_TOOLS
    for name in ("http_request", "fetch_web"):
        assert name not in planner.TOOLS
        assert name not in executor.TOOLS


def test_parse_rejects_empty_and_garbage():
    for bad in ("", "   ", "no json here", "[1,2,3]"):
        with pytest.raises(planner.PlannerError):
            planner.parse_action(bad)


# ------------------------------- executor ----------------------------------


def test_safe_path_blocks_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "WORKSPACE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        executor._safe_path("../outside.txt")
    with pytest.raises(ValueError):
        executor._safe_path("/etc/passwd")
    assert executor._safe_path("sub/inside.txt").startswith(str(tmp_path))


def test_write_then_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "WORKSPACE_DIR", str(tmp_path))
    w = executor.execute({"tool": "write_file", "path": "nested/hello.txt", "content": "hi"})
    assert w["ok"] is True
    r = executor.execute({"tool": "read_file", "path": "nested/hello.txt"})
    assert r["ok"] is True and r["stdout"] == "hi"


def test_list_files_and_unknown_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    listing = executor.execute({"tool": "list_files"})
    assert listing["ok"] is True and "a.txt" in listing["stdout"]
    bad = executor.execute({"tool": "nope"})
    assert bad["ok"] is False and "unknown tool" in bad["stderr"]


def test_run_command_local(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("CLI_MODE", "local")
    ok = executor.execute({"tool": "run_command", "command": "echo hello"})
    assert ok["ok"] is True and "hello" in ok["stdout"]
    bad = executor.execute({"tool": "run_command", "command": "exit 3"})
    assert bad["ok"] is False and bad["exit_code"] == 3


# ------------------------------- inspector ---------------------------------


def test_observe_reports_status_and_output():
    text = inspector.observe({"ok": True, "exit_code": 0, "stdout": "body"})
    assert "status=OK" in text and "exit_code=0" in text and "body" in text


def test_observe_handles_failure_and_empty():
    assert "FAILED" in inspector.observe({"ok": False, "stderr": "boom", "exit_code": 2})
    assert "(no output)" in inspector.observe({"ok": True, "exit_code": 0})


# ------------------------------- agent loop --------------------------------


def test_agent_end_to_end_with_scripted_planner(monkeypatch, tmp_path):
    """Full loop: 3 actions then done, driven by a fake LLM."""
    monkeypatch.setattr(executor, "WORKSPACE_DIR", str(tmp_path))
    script = [
        {"tool": "list_files"},
        {"tool": "write_file", "path": "out.txt", "content": "hello"},
        {"tool": "read_file", "path": "out.txt"},
        {"tool": "done", "outcome": "wrote and verified out.txt"},
    ]
    calls = {"n": 0}

    def fake_next_action(goal, history, files=None, extra_hint=None):
        action = script[calls["n"]]
        calls["n"] += 1
        return dict(action)

    monkeypatch.setattr(planner, "next_action", fake_next_action)
    monkeypatch.setattr(planner, "workspace_files", lambda: [])
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    result = agent.run_task("write out.txt", max_steps=5)
    assert result.ok is True
    assert result.stopped_because == "done"
    assert result.mode == "llm"
    assert len(result.steps) == 4
    assert result.outcome == "wrote and verified out.txt"


def test_agent_retries_unusable_reply(monkeypatch):
    """A PlannerError must be retried with a corrective hint, not abort the run."""
    attempts = {"n": 0}

    def flaky(goal, history, files=None, extra_hint=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise planner.PlannerError("bad reply")
        assert extra_hint and "unusable" in extra_hint
        return {"tool": "done", "outcome": "recovered"}

    monkeypatch.setattr(planner, "next_action", flaky)
    monkeypatch.setattr(planner, "workspace_files", lambda: [])
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    result = agent.run_task("anything", max_steps=3)
    assert result.ok is True and result.outcome == "recovered"
    assert attempts["n"] == 2


def test_agent_stops_when_repeating_failing_action(monkeypatch):
    """Repeated identical actions must trigger a nudge hint."""
    hints: list[str | None] = []

    def always_same(goal, history, files=None, extra_hint=None):
        hints.append(extra_hint)
        return {"tool": "run_command", "command": "false"}

    monkeypatch.setattr(planner, "next_action", always_same)
    monkeypatch.setattr(planner, "workspace_files", lambda: [])
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setenv("CLI_MODE", "local")

    result = agent.run_task("fail forever", max_steps=3)
    assert result.ok is False
    assert result.stopped_because == "max_steps_reached"
    assert any(h and "exact action" in h for h in hints)
    assert any(h and "failed" in h.lower() for h in hints)


def test_agent_fallback_mode_without_llm(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    monkeypatch.setenv("CLI_MODE", "local")
    result = agent.run_task("echo fallback-ok", max_steps=3)
    assert result.mode == "fallback"
    assert "fallback-ok" in result.outcome or result.ok is False


def test_agent_fallback_does_not_execute_goal_without_llm(monkeypatch):
    """Without an LLM, natural-language goals must never be passed to the shell."""
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    monkeypatch.setenv("CLI_MODE", "local")
    calls = []
    monkeypatch.setattr(
        executor, "execute",
        lambda action: calls.append(action) or {
            "ok": False, "stdout": "", "stderr": "should not run", "exit_code": 3
        },
    )

    result = agent.run_task("exit 3", max_steps=3)

    assert result.ok is False
    assert result.stopped_because == "llm_not_configured"
    assert "No terminal command was run" in result.outcome
    assert calls == []


# ------------------------------- Modal sandbox ------------------------------


class _FakeStream:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


class _FakeProcess:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0):
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._exit_code = exit_code

    def wait(self) -> int:
        return self._exit_code


class _FakeSandbox:
    object_id = "sb-test"

    def __init__(self):
        self.calls: list[tuple] = []
        self.running = True

    def poll(self):
        return None if self.running else 0

    def exec(self, *args, timeout=None, workdir=None, **kwargs):
        self.calls.append((args, timeout, workdir))
        return _FakeProcess(stdout="modal-ok\n")


def test_modal_run_dispatches_to_sandbox(monkeypatch):
    sandbox = _FakeSandbox()
    monkeypatch.setenv("CLI_MODE", "modal")
    monkeypatch.setenv("MODAL_WORKDIR", "/workspace")
    monkeypatch.setattr(cli, "_modal_sandbox", lambda: sandbox)

    result = cli.run("echo hi")
    assert result["ok"] is True
    assert result["stdout"].strip() == "modal-ok"
    assert sandbox.calls == [(("bash", "-lc", "echo hi"), 120, "/workspace")]


def test_modal_recreates_terminated_sandbox(monkeypatch):
    old = _FakeSandbox()
    old.running = False
    new = _FakeSandbox()
    monkeypatch.setenv("CLI_MODE", "modal")
    monkeypatch.setenv("MODAL_IMAGE", "test-image")
    monkeypatch.setattr(cli, "_create_modal_sandbox", lambda: new)

    cli._reset_modal_cache()
    cli._MODAL_CACHE["sandbox"] = old
    cli._MODAL_CACHE["sandbox_signature"] = cli._modal_signature()

    assert cli._modal_sandbox() is new
    assert cli._MODAL_CACHE["sandbox"] is new


def test_modal_image_reference_accepts_dashboard_url(monkeypatch):
    monkeypatch.setenv("MODAL_PROFILE", "rnpvip685")
    monkeypatch.setenv("MODAL_IMAGE", "https://modal.com/images/rnpvip685/main")
    assert cli._modal_image_ref() == "main"
    monkeypatch.setenv("MODAL_IMAGE", "rnpvip685/main")
    assert cli._modal_image_ref() == "main"


def test_modal_status_reports_config(monkeypatch):
    monkeypatch.setenv("CLI_MODE", "modal")
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-test")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-test")
    monkeypatch.setenv("MODAL_PROFILE", "rnpvip685")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "main")
    monkeypatch.setenv("MODAL_IMAGE", "main")
    status = cli.status()
    assert status["mode"] == "modal"
    assert status["configured"] is True
    assert status["modal"]["token_id_set"] is True
    assert status["modal"]["token_secret_set"] is True
    assert status["modal"]["image"] == "main"


def test_executor_routes_file_tools_to_remote(monkeypatch):
    calls: list[tuple] = []
    ok = {"ok": True, "stdout": "x", "stderr": "", "exit_code": 0}

    monkeypatch.setenv("CLI_MODE", "modal")
    monkeypatch.setattr(cli, "read_file_remote", lambda p: calls.append(("read", p)) or ok)
    monkeypatch.setattr(
        cli, "write_file_remote", lambda p, c: calls.append(("write", p, c)) or ok
    )
    monkeypatch.setattr(cli, "list_files_remote", lambda prefix="": calls.append(("list", prefix)) or ok)

    executor.execute({"tool": "read_file", "path": "a.txt"})
    executor.execute({"tool": "write_file", "path": "a.txt", "content": "body"})
    executor.execute({"tool": "list_files"})
    assert calls == [("read", "a.txt"), ("write", "a.txt", "body"), ("list", "")]


def test_workspace_files_uses_remote_listing(monkeypatch):
    monkeypatch.setenv("CLI_MODE", "modal")
    monkeypatch.setattr(
        cli,
        "list_files_remote",
        lambda prefix="": {"ok": True, "stdout": "a.txt\nb.txt", "stderr": "", "exit_code": 0},
    )
    assert executor.workspace_files() == ["a.txt", "b.txt"]
