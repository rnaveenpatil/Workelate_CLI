from app import agent


def test_natural_language_request_without_llm_never_runs_shell(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: False)
    monkeypatch.setattr(agent.planner, "workspace_files", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(agent.executor, "execute", lambda action: (_ for _ in ()).throw(AssertionError()))

    result = agent.run_task("Please inspect my project and fix the bug")

    assert result.ok is False
    assert result.mode == "fallback"
    assert result.steps == []
    assert result.stopped_because == "llm_not_configured"
    assert "No terminal command was run" in result.outcome


def test_final_summary_uses_execution_trace_and_keeps_steps(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: True)
    monkeypatch.setattr(agent.planner, "workspace_files", lambda: ["README.md"])
    actions = iter([
        {"tool": "run_command", "command": "pytest -q", "thought": "Run tests"},
        {"tool": "done", "outcome": "Tests passed and task is complete", "thought": "Finished"},
    ])
    monkeypatch.setattr(agent.planner, "next_action", lambda *args, **kwargs: next(actions))
    monkeypatch.setattr(agent.executor, "execute", lambda action: {
        "ok": True, "exit_code": 0, "stdout": "3 passed in 0.42s", "stderr": ""
    })
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return "Completed the requested check.\n\n- Ran `pytest -q`\n- 3 tests passed."

    monkeypatch.setattr(agent.llm, "chat", fake_chat)
    result = agent.run_task("Run the tests")

    assert result.ok is True
    assert result.outcome.startswith("Completed the requested check")
    assert len(result.steps) == 2
    assert result.steps[0].observation.startswith("status=OK")
    assert "pytest -q" in captured["messages"][1]["content"]
    assert "3 passed in 0.42s" in captured["messages"][1]["content"]


def test_summary_falls_back_if_synthesis_provider_fails(monkeypatch):
    monkeypatch.setattr(agent.llm, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")))
    history = [{
        "index": 1,
        "tool": "run_command",
        "input": {"tool": "run_command", "command": "git status"},
        "observation": "status=OK exit_code=0\nstdout:\nworking tree clean",
    }]

    summary = agent._summarize(
        "Check git status", history, "", completed=False, stopped="max_steps_reached"
    )
    assert "not confirmed complete" in summary
    assert "git status" in summary
    assert "Actions executed" in summary
