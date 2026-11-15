"""Workelate_CLI agent: goal -> actions on the workspace terminal -> outcome."""

import os

from . import executor, inspector, llm, planner
from .schemas import Step, TaskResult

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "6"))


def run_task(goal: str, max_steps: int | None = None, cwd: str | None = None) -> TaskResult:
    limit = max_steps or MAX_STEPS
    mode = "llm" if llm.is_configured() else "fallback"
    files = planner.workspace_files()

    steps: list[Step] = []
    history: list[dict] = []
    stopped = "max_steps_reached"

    for i in range(1, limit + 1):
        try:
            action = planner.next_action(goal, history, files)
        except Exception as exc:  # noqa: BLE001
            stopped = f"planner_error: {exc}"
            break

        tool = action.get("tool", "")

        if tool == "done":
            outcome = action.get("outcome") or "done"
            steps.append(Step(index=i, tool="done", input={}, observation=action.get("thought", "")))
            return TaskResult(
                ok=True, goal=goal, mode=mode, steps=steps, outcome=outcome, stopped_because="done"
            )

        if cwd and "run_command" == tool:
            action.setdefault("cwd", cwd)

        result = executor.execute(action)
        observation = inspector.observe(result)

        history.append({"index": i, "tool": tool, "input": _brief(action), "observation": observation})
        steps.append(Step(index=i, tool=tool, input=_brief(action), observation=observation))

    outcome = steps[-1].observation if steps else "no steps were executed"
    return TaskResult(ok=False, goal=goal, mode=mode, steps=steps, outcome=outcome, stopped_because=stopped)


def _brief(action: dict) -> dict:
    """Trim echo of large payloads into the step log."""
    keep = ("tool", "command", "path", "cwd", "timeout")
    brief = {k: v for k, v in action.items() if k in keep}
    if "content" in action:
        brief["content"] = f"<{len(str(action['content']))} chars>"
    return brief
