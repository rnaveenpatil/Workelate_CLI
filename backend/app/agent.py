"""Workelate_CLI agent: understand -> answer or act -> explain the result."""

import json
import os

from . import executor, inspector, llm, planner
from .schemas import Step, TaskResult

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
PLANNER_RETRIES = int(os.getenv("AGENT_PLANNER_RETRIES", "3"))
LOOP_LIMIT = 2


def run_task(goal: str, max_steps: int | None = None, cwd: str | None = None) -> TaskResult:
    limit = max_steps or MAX_STEPS
    mode = "llm" if llm.is_configured() else "fallback"

    # The model must decide whether this is answer-only or workspace work. Without
    # a model, do not guess from keywords or execute the user's natural language.
    if mode == "fallback":
        return TaskResult(
            ok=False, goal=goal, mode=mode, steps=[],
            outcome=(
                "I can't determine whether this request needs an answer or workspace "
                "actions because no language model is configured. Add an LLM API key "
                "in backend/.env. No terminal command was run."
            ),
            stopped_because="llm_not_configured",
        )

    # Do not eagerly list the workspace: direct questions should not incur terminal
    # latency or expose workspace metadata. The model can request list_files itself.
    files: list[str] = []
    steps: list[Step] = []
    history: list[dict] = []
    signatures: list[str] = []
    failures: dict[str, int] = {}
    stopped = "max_steps_reached"
    hint: str | None = None
    last_ok: bool | None = None

    for i in range(1, limit + 1):
        action = None
        for _ in range(1, PLANNER_RETRIES + 1):
            try:
                action = planner.next_action(goal, history, files, extra_hint=hint)
                break
            except planner.PlannerError as exc:
                hint = (
                    f"Your previous reply was unusable ({exc}). Return ONE valid JSON "
                    "object using a listed tool. Decide whether to answer, clarify, or act."
                )
            except Exception as exc:  # noqa: BLE001
                stopped = f"planner_error: {exc}"
                action = None
                break

        if action is None:
            if stopped == "max_steps_reached":
                stopped = "planner_unusable_reply"
            break

        tool = action.get("tool", "")
        if tool == "done":
            proposed_outcome = str(action.get("outcome") or "").strip()
            needs_clarification = bool(action.get("needs_clarification", False))
            steps.append(
                Step(index=i, tool="done", input={"needs_clarification": needs_clarification},
                     observation=action.get("thought", ""))
            )

            # For a direct answer or clarification, return the model's answer as-is.
            # No extra summarization call and no unnecessary terminal action.
            if not history:
                return TaskResult(
                    ok=True, goal=goal, mode=mode, steps=steps,
                    outcome=proposed_outcome or (
                        "I need one more detail before I can proceed."
                        if needs_clarification else "I couldn't produce an answer."
                    ),
                    stopped_because="clarification_required" if needs_clarification else "done",
                )

            summary = _summarize(
                goal, history, proposed_outcome,
                completed=last_ok is not False,
                stopped="done" if last_ok is not False else "last_action_failed",
            )
            return TaskResult(
                ok=last_ok is not False, goal=goal, mode=mode, steps=steps,
                outcome=summary,
                stopped_because="done" if last_ok is not False else "last_action_failed",
            )

        sig = json.dumps(
            {k: v for k, v in action.items() if k != "thought"}, sort_keys=True, default=str
        )
        signatures.append(sig)
        repeats = signatures.count(sig)
        hints: list[str] = []
        if repeats >= LOOP_LIMIT and sig == signatures[-1]:
            hints.append(
                f"You chose this exact action {repeats} times. Choose a different "
                "approach or finish with the evidence you have."
            )

        if cwd and tool == "run_command":
            action.setdefault("cwd", cwd)

        result = executor.execute(action)
        last_ok = bool(result.get("ok"))
        observation = inspector.observe(result)
        history.append(
            {"index": i, "tool": tool, "input": _brief(action), "observation": observation}
        )
        steps.append(Step(index=i, tool=tool, input=_brief(action), observation=observation))

        if not result.get("ok"):
            error_key = _error_key(result)
            failures[error_key] = failures.get(error_key, 0) + 1
            times = failures[error_key]
            seen = "" if times == 1 else f" (this exact failure has now happened {times} times)"
            hints.append(
                f"The last {tool} call FAILED{seen}: {error_key}. Diagnose from the "
                "observation and change the approach; do not repeat it unchanged."
            )
        hint = " ".join(hints) or None

    summary = _summarize(goal, history, "", completed=False, stopped=stopped)
    return TaskResult(
        ok=False, goal=goal, mode=mode, steps=steps, outcome=summary, stopped_because=stopped
    )


def _summarize(
    goal: str,
    history: list[dict],
    proposed_outcome: str,
    *,
    completed: bool,
    stopped: str,
) -> str:
    """Explain workspace work using observed evidence; keep raw output in the log."""
    trace = [
        {
            "step": item["index"], "tool": item["tool"], "input": item["input"],
            "result": item["observation"][:2500],
        }
        for item in history[-12:]
    ]
    if proposed_outcome:
        trace.append({"agent_final_note": proposed_outcome[:3000]})
    status = (
        "The agent reached its done action."
        if completed else f"The agent stopped before confirming completion ({stopped})."
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You write the final user-facing report for a workspace automation agent. "
                "Use ONLY the supplied goal and execution trace. Begin by briefly explaining "
                "what you understood the user wanted. Then state what you actually did and "
                "the evidence/results. Clearly separate completed work, attempted work, and "
                "remaining blockers. Never invent commands, file changes, tests, or success. "
                "If a command failed, say whether it was later resolved. Do not dump raw JSON, "
                "stdout, or stderr. Write concise plain Markdown. Mention paths and command "
                "results only when evidenced. If evidence is insufficient, say so."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"user_goal": goal, "execution_status": status, "execution_trace": trace},
                ensure_ascii=False, default=str,
            ),
        },
    ]
    try:
        report = llm.chat(messages, temperature=0.1, timeout=45).strip()
        if report:
            return report
    except Exception:  # noqa: BLE001
        pass
    return _deterministic_summary(goal, history, completed, stopped)


def _deterministic_summary(goal: str, history: list[dict], completed: bool, stopped: str) -> str:
    """Readable fallback if final LLM synthesis is unavailable."""
    actions = []
    failures = []
    for item in history:
        action = item.get("input") or {}
        label = action.get("command") or action.get("path") or item.get("tool", "action")
        actions.append(f"- `{label}`")
        observation = str(item.get("observation") or "")
        if "status=FAILED" in observation:
            failures.append(f"- `{label}` returned a failure; inspect its execution-log output.")
    heading = (
        "The agent reached its completion step."
        if completed else f"The request was not confirmed complete (stopped: {stopped})."
    )
    lines = [heading, "", f"**What I understood:** {goal}"]
    if actions:
        lines.extend(["", "**Actions executed:**", *actions])
    else:
        lines.extend(["", "No terminal actions were executed."])
    if failures:
        lines.extend(["", "**Failures:**", *failures])
    elif completed:
        lines.extend(["", "The planner reported completion. Review the execution log for command-level evidence."])
    return "\n".join(lines)


def _error_key(result: dict) -> str:
    text = (result.get("stderr") or result.get("stdout") or "unknown error").strip()
    first = next((line for line in text.splitlines() if line.strip()), "unknown error")
    return first[:200]


def _brief(action: dict) -> dict:
    keep = ("tool", "command", "path", "cwd", "timeout", "url", "method", "prefix")
    brief = {k: v for k, v in action.items() if k in keep}
    if "content" in action:
        brief["content"] = f"<{len(str(action['content']))} chars>"
    if "body" in action:
        brief["body"] = f"<{len(str(action['body']))} chars>"
    return brief
