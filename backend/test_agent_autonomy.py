from app import agent, planner


def test_direct_question_is_answered_without_workspace_access_or_summary(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: True)
    monkeypatch.setattr(agent.planner, "workspace_files", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(agent.planner, "next_action", lambda *a, **k: {
        "tool": "done", "outcome": "A virtual environment isolates project dependencies.",
        "needs_clarification": False, "thought": "General knowledge answer"
    })
    monkeypatch.setattr(agent.llm, "chat", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("direct answer must not trigger a synthesis call")
    ))
    result = agent.run_task("What is a Python virtual environment?")
    assert result.ok is True
    assert result.steps[0].tool == "done"
    assert result.outcome.startswith("A virtual environment")
    assert result.stopped_because == "done"


def test_clarification_is_returned_without_running_tools(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: True)
    monkeypatch.setattr(agent.planner, "next_action", lambda *a, **k: {
        "tool": "done", "outcome": "Which project directory should I modify?",
        "needs_clarification": True, "thought": "Target is ambiguous"
    })
    monkeypatch.setattr(agent.executor, "execute", lambda a: (_ for _ in ()).throw(AssertionError()))
    result = agent.run_task("Update the config")
    assert result.ok is True
    assert result.stopped_because == "clarification_required"
    assert "Which project directory" in result.outcome


def test_action_is_executed_then_explained(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: True)
    actions = iter([
        {"tool": "run_command", "command": "pytest -q", "thought": "Run tests"},
        {"tool": "done", "outcome": "The test suite passed.", "thought": "Finished"},
    ])
    monkeypatch.setattr(agent.planner, "next_action", lambda *a, **k: next(actions))
    monkeypatch.setattr(agent.executor, "execute", lambda action: {
        "ok": True, "exit_code": 0, "stdout": "3 passed in 0.42s", "stderr": ""
    })
    monkeypatch.setattr(agent.llm, "chat", lambda *a, **k: (
        "You wanted the test suite run.\n\nRan `pytest -q`; 3 tests passed."
    ))
    result = agent.run_task("Run the tests")
    assert result.ok is True
    assert "You wanted the test suite run" in result.outcome
    assert "3 tests passed" in result.outcome


def test_parse_action_accepts_answer_and_clarification():
    answer = planner.parse_action('{"tool":"done","outcome":"Here is the answer.","needs_clarification":false}')
    clarification = planner.parse_action('{"tool":"done","outcome":"Which folder?","needs_clarification":true}')
    assert answer["tool"] == "done" and not answer["needs_clarification"]
    assert clarification["needs_clarification"]


def test_natural_language_without_llm_never_runs_shell(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: False)
    monkeypatch.setattr(agent.executor, "execute", lambda action: (_ for _ in ()).throw(AssertionError()))
    result = agent.run_task("Please inspect my project and fix the bug")
    assert result.ok is False
    assert result.steps == []
    assert "No terminal command was run" in result.outcome
