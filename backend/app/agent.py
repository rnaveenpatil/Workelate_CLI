"""Conversational agent with model-driven answer, clarify, and execute decisions."""

import json
import os

from . import executor, inspector, llm, planner
from .schemas import Step, TaskResult

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
PLANNER_RETRIES = int(os.getenv("AGENT_PLANNER_RETRIES", "3"))
LOOP_LIMIT = 2


def run_task(
    goal: str,
    max_steps: int | None = None,
    cwd: str | None = None,
    conversation: list[dict] | None = None,
) -> TaskResult:
    limit = max_steps or MAX_STEPS
    mode = "llm" if llm.is_configured() else "fallback"

    if mode == "fallback":
        return TaskResult(
            ok=False, goal=goal, mode=mode, steps=[],
            outcome=(
                "I can't answer or execute this request because no language model "
                "is configured. Add an LLM API key in backend/.env. No terminal "
                "command was run."
            ),
            stopped_because="llm_not_configured",
        )

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
                action = planner.next_action(
                    goal, history, extra_hint=hint, conversation=conversation
                )
                break
            except planner.PlannerError as exc:
                hint = (
                    f"Your previous response was invalid ({exc}). Return one valid "
                    "JSON object matching one of the documented action shapes."
                )
            except Exception as exc:  # noqa: BLE001
                stopped = f"planner_error: {exc}"
                break

        if action is None:
            if stopped == "max_steps_reached":
                stopped = "planner_unusable_reply"
            break

        tool = action.get("tool", "")
        if tool == "respond":
            response = str(action.get("response", "")).strip()
            return TaskResult(
                ok=True, goal=goal, mode=mode, steps=[],
                outcome=response, stopped_because="answered_directly",
            )

        if tool == "clarify":
            question = str(action.get("question", "")).strip()
            return TaskResult(
                ok=True, goal=goal, mode=mode, steps=[],
                outcome=question, stopped_because="clarification_required",
            )

        if tool == "done":
            proposed_outcome = str(action.get("outcome") or "").strip()
            steps.append(
                Step(index=i, tool="done", input={}, observation=action.get("thought", ""))
            )
            if not history:
                summary = proposed_outcome or "The agent finished without providing a response."
            else:
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
                f"You selected this exact action {repeats} times. Change approach "
                "or finish with the evidence already available."
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
            seen = "" if times == 1 else f" (repeated {times} times)"
            hints.append(
                f"The last command failed{seen}: {error_key}. Diagnose the output "
                "and change the approach; do not repeat it unchanged."
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
    """Turn execution evidence into a natural answer, without a rigid report template."""
    trace = [
        {
            "step": item["index"],
            "tool": item["tool"],
            "input": item["input"],
            "result": item["observation"][:2500],
        }
        for item in history[-12:]
    ]
    status = (
        "The agent believes the task is complete."
        if completed else f"The task is not confirmed complete ({stopped})."
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Answer the user's original request naturally and directly, using only "
                "the supplied execution evidence. Explain what the user asked for and "
                "what actually happened in terms they can understand. State important "
                "results, failures, and remaining blockers. Never invent changes, "
                "commands, or test results. Do not force headings, bullet lists, or a "
                "fixed report format; use them only when they genuinely improve clarity. "
                "Keep it concise."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_request": goal,
                    "execution_status": status,
                    "agent_final_note": proposed_outcome,
                    "execution_evidence": trace,
                },
                ensure_ascii=False, default=str,
            ),
        },
    ]
    try:
        report = llm.chat(messages, temperature=0.2, timeout=45).strip()
        if report:
            return report
    except Exception:  # noqa: BLE001
        pass
    return _deterministic_summary(goal, history, completed, stopped)


def _deterministic_summary(goal: str, history: list[dict], completed: bool, stopped: str) -> str:
    actions = []
    failures = []
    for item in history:
        action = item.get("input") or {}
        label = action.get("command") or item.get("tool", "action")
        actions.append(f"- `{label}`")
        observation = str(item.get("observation") or "")
        if "status=FAILED" in observation:
            failures.append(f"- `{label}` failed; inspect its execution output.")
    heading = (
        "The agent completed the requested workspace actions."
        if completed else f"The request was not confirmed complete (stopped: {stopped})."
    )
    lines = [heading, f"Request: {goal}"]
    if actions:
        lines.extend(["Actions:", *actions])
    if failures:
        lines.extend(["Failures:", *failures])
    return "\n".join(lines)


def _error_key(result: dict) -> str:
    text = (result.get("stderr") or result.get("stdout") or "unknown error").strip()
    first = next((line for line in text.splitlines() if line.strip()), "unknown error")
    return first[:200]


def _brief(action: dict) -> dict:
    keep = ("tool", "command", "cwd", "timeout")
    return {k: v for k, v in action.items() if k in keep}
