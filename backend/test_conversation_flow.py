from app import agent, planner


def test_planner_accepts_direct_conversational_answer():
    action = planner.parse_action(
        '{"tool":"respond","response":"A Python list is mutable."}'
    )
    assert action["tool"] == "respond"
    assert action["response"] == "A Python list is mutable."


def test_planner_accepts_clarification_and_workspace_actions():
    assert planner.parse_action(
        '{"tool":"clarify","question":"Which environment should I use?"}'
    )["tool"] == "clarify"
    assert planner.parse_action(
        '{"tool":"read_file","path":"README.md"}'
    )["tool"] == "read_file"
    assert planner.parse_action(
        '{"tool":"run_command","command":"pytest -q"}'
    )["tool"] == "run_command"


def test_planner_rejects_plain_text_and_missing_action_fields():
    try:
        planner.parse_action("Hello! How can I help?")
    except planner.PlannerError:
        pass
    else:
        raise AssertionError("plain text must not be accepted as a structured action")

    try:
        planner.parse_action('{"tool":"run_command"}')
    except planner.PlannerError:
        pass
    else:
        raise AssertionError("run_command without a command must be rejected")


def test_direct_answer_skips_workspace_and_summary(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        agent.planner, "workspace_files",
        lambda: (_ for _ in ()).throw(AssertionError("workspace listed")),
    )
    monkeypatch.setattr(
        agent.executor, "execute",
        lambda action: (_ for _ in ()).throw(AssertionError("tool executed")),
    )
    monkeypatch.setattr(
        agent, "_summarize",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("summary called")),
    )
    monkeypatch.setattr(
        agent.planner, "next_action",
        lambda *args, **kwargs: {
            "tool": "respond", "response": "A concise direct answer."
        },
    )

    result = agent.run_task("What is a Python tuple?")

    assert result.ok is True
    assert result.steps == []
    assert result.outcome == "A concise direct answer."
    assert result.stopped_because == "answered_directly"


def test_clarification_returns_without_running_workspace_tools(monkeypatch):
    monkeypatch.setattr(agent.llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        agent.executor, "execute",
        lambda action: (_ for _ in ()).throw(AssertionError("tool executed")),
    )
    monkeypatch.setattr(
        agent.planner, "next_action",
        lambda *args, **kwargs: {
            "tool": "clarify", "question": "Which project directory?"
        },
    )

    result = agent.run_task("Fix the issue")

    assert result.ok is True
    assert result.steps == []
    assert result.outcome == "Which project directory?"
    assert result.stopped_because == "clarification_required"


def test_conversation_history_is_passed_to_model(monkeypatch):
    monkeypatch.setattr(planner.llm, "is_configured", lambda: True)
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return '{"tool":"respond","response":"It refers to the previous command."}'

    monkeypatch.setattr(planner.llm, "chat", fake_chat)
    conversation = [
        {"role": "user", "content": "Run git status"},
        {"role": "assistant", "content": "The working tree is clean."},
    ]

    action = planner.next_action(
        "Why did you say that?", history=[], conversation=conversation
    )

    assert action["tool"] == "respond"
    serialized = "\\n".join(message["content"] for message in captured["messages"])
    assert "Run git status" in serialized
    assert "The working tree is clean." in serialized
    assert "Why did you say that?" in serialized
