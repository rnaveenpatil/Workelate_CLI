"""Workelate_CLI agent: goal -> actions on the workspace terminal -> outcome.

The loop is deliberately defensive:

* planner replies that are unusable are retried with a corrective hint;
* identical-but-failing actions are detected and the model is told to change approach;
* a failing-command history is summarized so the model does not burn steps re-running it;
* every tool call (including unknown tools) yields an observation the model can react to.
"""

import json
import os

from . import executor, inspector, llm, planner
from .schemas import Step, TaskResult

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
PLANNER_RETRIES = int(os.getenv("AGENT_PLANNER_RETRIES", "3"))
LOOP_LIMIT = 2  # identical action this many times in a row => nudge the model


def run_task(goal: str, max_steps: int | None = None, cwd: str | None = None) -> TaskResult:
    limit = max_steps or MAX_STEPS
    mode = "llm" if llm.is_configured() else "fallback"
    files = planner.workspace_files()

    steps: list[Step] = []
    history: list[dict] = []
    signatures: list[str] = []
    failures: dict[str, int] = {}
    stopped = "max_steps_reached"
    hint: str | None = None

    for i in range(1, limit + 1):
        action = None
        for _ in range(1, PLANNER_RETRIES + 1):
            try:
                action = planner.next_action(goal, history, files, extra_hint=hint)
                break
            except planner.PlannerError as exc:
                hint = (
                    f"your previous reply was unusable ({exc}). "
                    "Reply with ONE valid JSON object using a listed tool."
                )
            except Exception as exc:  # noqa: BLE001  (network / provider errors)
                stopped = f"planner_error: {exc}"
                action = None
                break

        if action is None:
            if stopped == "max_steps_reached":
                stopped = "planner_unusable_reply"
            break

        tool = action.get("tool", "")

        if tool == "done":
            outcome = str(action.get("outcome") or "").strip() or "done"
            steps.append(
                Step(index=i, tool="done", input={}, observation=action.get("thought", ""))
            )
            return TaskResult(
                ok=True, goal=goal, mode=mode, steps=steps, outcome=outcome, stopped_because="done"
            )

        # Detect the model spinning on the same action.
        sig = json.dumps(
            {k: v for k, v in action.items() if k != "thought"}, sort_keys=True, default=str
        )
        signatures.append(sig)
        repeats = signatures.count(sig)
        hints: list[str] = []
        if repeats >= LOOP_LIMIT and sig == signatures[-1]:
            hints.append(
                f"you have chosen this exact action {repeats} times. "
                "Do something different, or call `done` with what you know."
            )

        if cwd and tool == "run_command":
            action.setdefault("cwd", cwd)

        result = executor.execute(action)
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
                f"the last {tool} call FAILED{seen}: {error_key}. "
                "Diagnose it from the observation and change the approach - do not repeat it."
            )

        hint = " ".join(hints) or None

    outcome = steps[-1].observation if steps else "no steps were executed"
    return TaskResult(
        ok=False, goal=goal, mode=mode, steps=steps, outcome=outcome, stopped_because=stopped
    )


def _error_key(result: dict) -> str:
    """Short, stable fingerprint of a failure so repeats can be counted."""
    text = (result.get("stderr") or result.get("stdout") or "unknown error").strip()
    first = next((line for line in text.splitlines() if line.strip()), "unknown error")
    return first[:200]


def _brief(action: dict) -> dict:
    """Trim echo of large payloads into the step log."""
    keep = ("tool", "command", "path", "cwd", "timeout", "url", "method", "prefix")
    brief = {k: v for k, v in action.items() if k in keep}
    if "content" in action:
        brief["content"] = f"<{len(str(action['content']))} chars>"
    if "body" in action:
        brief["body"] = f"<{len(str(action['body']))} chars>"
    return brief
