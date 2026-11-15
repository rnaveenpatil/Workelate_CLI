"""Tests for the planner / executor / web layer.

Run:  cd backend && python -m pytest -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, cli, executor, inspector, llm, planner, web  # noqa: E402


@pytest.fixture(autouse=True)
def _local_terminal(monkeypatch):
    """Keep tests hermetic: never talk to the real remote terminal."""
    monkeypatch.setenv("CLI_MODE", "local")
    monkeypatch.delenv("SANDBOX_CONTAINER", raising=False)


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
    assert planner.parse_action('{"name":"ls"}')["tool"] == "list_files"
    assert planner.parse_action('{"action":"finish","outcome":"ok"}')["tool"] == "done"


def test_parse_rejects_unknown_tool():
    with pytest.raises(planner.PlannerError):
        planner.parse_action('{"tool":"rm_rf_everything"}')


def test_parse_rejects_missing_required_fields():
    with pytest.raises(planner.PlannerError):
        planner.parse_action('{"tool":"run_command"}')
    with pytest.raises(planner.PlannerError):
        planner.parse_action('{"tool":"http_request"}')


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


# --------------------------------- web -------------------------------------


def test_html_to_text_strips_markup():
    raw = "<html><head><style>.x{}</style></head><body><h1>Hi</h1><p>A &amp; B</p></body></html>"
    text = web.html_to_text(raw)
    assert "Hi" in text and "A & B" in text
    assert "<" not in text and ".x{}" not in text


def test_http_request_rejects_bad_scheme():
    r = web.http_request("ftp://example.com")
    assert r["ok"] is False and "http" in r["stderr"].lower()


def test_fetch_page_rejects_empty_url():
    r = web.fetch_page("")
    assert r["ok"] is False


def test_http_request_live_smoke():
    """Network-dependent; skipped automatically when offline."""
    r = web.http_request("https://example.com", timeout=10)
    if not r["ok"] and "request failed" in r["stderr"]:
        pytest.skip("no network")
    assert r["status"] in (200, 301, 302) and "example" in r["stdout"].lower()


# ------------------------------- inspector ---------------------------------


def test_observe_reports_status_and_http():
    text = inspector.observe({"ok": True, "status": 200, "stdout": "body", "content_type": "text/html"})
    assert "status=OK" in text and "http_status=200" in text and "body" in text


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
    assert any(h and "FAILED" in h for h in hints)


def test_agent_fallback_mode_without_llm(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    monkeypatch.setenv("CLI_MODE", "local")
    result = agent.run_task("echo fallback-ok", max_steps=3)
    assert result.mode == "fallback"
    assert "fallback-ok" in result.outcome or result.ok is False


def test_agent_fallback_reports_failed_command(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    monkeypatch.setenv("CLI_MODE", "local")
    result = agent.run_task("exit 3", max_steps=3)
    assert result.ok is False
    assert result.stopped_because == "fallback_command_failed"


# ------------------------------ remote terminal -----------------------------


def test_remote_headers_carry_both_tokens(monkeypatch):
    monkeypatch.setenv("CLI_TOKEN", "shared")
    monkeypatch.setenv("CLI_GITHUB_TOKEN", "ghu_test")
    assert cli._headers() == {"X-CLI-Token": "shared", "X-Github-Token": "ghu_test"}


def test_sandbox_wraps_remote_commands(monkeypatch):
    captured: dict = {}

    def fake_remote(command, cwd=None, timeout=120):
        captured.update(command=command, cwd=cwd, timeout=timeout)
        return {"ok": True, "exit_code": 0, "stdout": "hi", "stderr": ""}

    monkeypatch.setenv("CLI_MODE", "remote")
    monkeypatch.setenv("SANDBOX_CONTAINER", "agent-sandbox")
    monkeypatch.setenv("SANDBOX_WORKDIR", "/workspace")
    monkeypatch.setenv("SANDBOX_USER", "1000:1000")
    monkeypatch.setattr(cli, "run_remote", fake_remote)

    result = cli.run("echo hi && ls", cwd="/workspaces/Workelate_CLI")
    assert result["ok"] is True
    assert "docker exec" in captured["command"]
    assert "agent-sandbox" in captured["command"]
    assert "--user 1000:1000" in captured["command"]
    assert "echo hi" in captured["command"]
    assert captured["cwd"] is None


def test_executor_routes_file_tools_to_remote(monkeypatch):
    calls: list[tuple] = []
    ok = {"ok": True, "stdout": "x", "stderr": "", "exit_code": 0}

    monkeypatch.setenv("CLI_MODE", "remote")
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
    monkeypatch.setenv("CLI_MODE", "remote")
    monkeypatch.setattr(
        cli,
        "list_files_remote",
        lambda prefix="": {"ok": True, "stdout": "a.txt\nb.txt", "stderr": "", "exit_code": 0},
    )
    assert executor.workspace_files() == ["a.txt", "b.txt"]


def test_remote_private_port_returns_actionable_error(monkeypatch):
    class FakeResponse:
        status_code = 401
        is_redirect = False
        headers = {"www-authenticate": "tunnel"}
        text = ""

        def json(self):
            return {}

    monkeypatch.setenv("CLI_REMOTE_URL", "https://example-8000.app.github.dev")
    monkeypatch.setenv("CLI_GITHUB_TOKEN", "")
    monkeypatch.setattr(cli.httpx, "post", lambda *a, **k: FakeResponse())

    result = cli.run_remote("echo hi")
    assert result["ok"] is False
    assert "CLI_GITHUB_TOKEN" in result["stderr"]
    assert "private" in result["stderr"].lower()
