from app import agent


def test_file_action_inputs_are_kept_in_execution_trace():
    brief = agent._brief({
        "tool": "write_file",
        "path": "src/example.py",
        "content": "print('hello')",
        "thought": "Create the requested file",
    })
    assert brief == {"tool": "write_file", "path": "src/example.py"}


def test_planner_failure_without_actions_skips_final_synthesis(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        agent.planner, "next_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    monkeypatch.setattr(
        agent, "_summarize",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("synthesis should not run")),
    )

    result = agent.run_task("Explain this follow-up")

    assert result.ok is False
    assert result.steps == []
    assert "provider unavailable" in result.outcome
    assert "No workspace action was executed" in result.outcome
