"""Decides the next action: one LLM call per step, with a small rule fallback.

The planner always returns a *normalized* action dict:

    {"tool": "<name>", ...arguments..., "thought": "<short>"}

so the executor can trust the shape even when the model is sloppy (fenced JSON,
aliases like "shell"/"read", extra prose around the object).
"""

import json
import os
import re

from . import llm

TOOLS = (
    "run_command",
    "read_file",
    "write_file",
    "list_files",
    "done",
)

# Tolerate common model shorthand.
ALIASES = {
    "shell": "run_command",
    "bash": "run_command",
    "command": "run_command",
    "exec": "run_command",
    "execute": "run_command",
    "terminal": "run_command",
    "read": "read_file",
    "cat": "read_file",
    "readfile": "read_file",
    "read_file_lines": "read_file",
    "write": "write_file",
    "create_file": "write_file",
    "save_file": "write_file",
    "edit_file": "write_file",
    "ls": "list_files",
    "list": "list_files",
    "listfiles": "list_files",
    "list_files_in_workspace": "list_files",
    "finish": "done",
    "stop": "done",
    "answer": "done",
    "final": "done",
}

# Fields the planner must echo back for a tool to be executable.
SPEC = {
    "run_command": ("command",),
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "list_files": (),
    "done": (),
}

SYSTEM = """You are Workelate_CLI, an autonomous agent that operates a shell \
inside the user's workspace. You work in a loop: you choose ONE action, you receive its \
observation, then you choose the next action. You never ask the user to run things \
yourself and you never describe what you would do - you act.

Reply with exactly one JSON object and nothing else. No prose, no markdown fences.

TOOLS
  run_command  {"tool":"run_command","command":"<shell>","cwd":"<optional>","timeout":<sec>,"thought":"..."}
  read_file    {"tool":"read_file","path":"<workspace-relative path>","thought":"..."}
  write_file   {"tool":"write_file","path":"<workspace-relative path>","content":"<FULL new file text>","thought":"..."}
  list_files   {"tool":"list_files","prefix":"<optional prefix>","thought":"..."}
  done         {"tool":"done","outcome":"<final answer>","thought":"..."}

HOW TO DECIDE THE NEXT ACTION
1. Restate the GOAL to yourself and ask: what single fact or effect is still missing?
2. If you do not know the workspace layout or a file's contents, inspect first
   (list_files / read_file) instead of guessing paths.
3. Prefer actions that produce evidence (command output, file contents, HTTP status)
   over actions that only change state.
4. After a change, verify it in a later step (re-read the file, re-run the check).
5. Use the fewest steps that still reach and verify the goal.

HARD RULES
- Exactly one action per reply, valid JSON, listed tool name.
- `write_file` must include the COMPLETE file content, not a diff or a placeholder.
- Paths are workspace-relative. Never try to escape the workspace with `..` or absolute paths.
- Use `run_command` for anything CLI-ish (git, npm, pip, python, tests, grep, curl, wget).
- There is no separate web tool. To read a page or call an API, use `run_command`
  with `curl`/`wget` (for example: `curl -sSL <url>` or
  `curl -sS -o /dev/null -w '%{http_code}' <url>`).
- Never repeat an action that already failed the same way - change the approach or the input.
- Never re-issue an action that already succeeded; build on its observation.
- Only call `done` when the goal is achieved, or when you are genuinely blocked.
- `outcome` must contain concrete results (paths, command output, findings), never a promise
  like "I will now ...". If blocked, state precisely what blocked you.
"""

PLANNER_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))


class PlannerError(RuntimeError):
    """Raised when the model reply cannot be turned into a usable action."""


def next_action(
    goal: str,
    history: list[dict],
    files: list[str] | None = None,
    extra_hint: str | None = None,
) -> dict:
    """Return the next normalized action, or drive the fallback when no key is set."""
    if not llm.is_configured():
        return _fallback(goal, history)

    context = (
        "\n".join(
            f"[{h['index']}] {h['tool']} {h['input']}\n  -> {h['observation']}" for h in history
        )
        or "(nothing yet - this is step 1)"
    )

    listing = ""
    if files:
        shown = ", ".join(files[:80])
        more = f" (+{len(files) - 80} more)" if len(files) > 80 else ""
        listing = f"\nWorkspace files: {shown}{more}"

    hint = f"\n\nIMPORTANT: {extra_hint}" if extra_hint else ""

    user = (
        f"GOAL: {goal}{listing}\n\n"
        f"STEPS SO FAR:\n{context}{hint}\n\n"
        "Choose the single next action. Reply with one JSON object."
    )
    raw = llm.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        temperature=PLANNER_TEMPERATURE,
    )
    return parse_action(raw)


def parse_action(raw: str) -> dict:
    """Turn a model reply into a validated action dict.

    Tolerates ```json fences, surrounding prose, and unknown-tool guesses. Raises
    PlannerError when the reply contains no usable JSON object or the tool is not
    recognized.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PlannerError("empty planner reply")

    data = _extract_json_object(raw.strip())
    if not isinstance(data, dict):
        raise PlannerError(f"reply was not a JSON object: {raw[:200]!r}")

    tool = str(data.get("tool") or data.get("name") or data.get("action") or "").strip().lower()
    tool = ALIASES.get(tool, tool)
    if tool not in TOOLS:
        raise PlannerError(f"unknown tool {tool!r}; allowed: {', '.join(TOOLS)}")
    data["tool"] = tool

    if not isinstance(data.get("thought"), str):
        data["thought"] = ""

    _require(data, tool)
    return data


def _extract_json_object(text: str) -> dict | None:
    """Pull the first decodable JSON object out of a noisy reply."""
    if "```" in text:
        text = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Scan for a balanced {...} block (regex alone breaks on nested braces/strings).
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _require(action: dict, tool: str) -> None:
    """Validate the fields each tool needs so the executor never gets a blank call."""
    for field in SPEC[tool]:
        if field == "content":
            if "content" not in action:
                raise PlannerError("write_file needs 'content'")
            continue
        if not str(action.get(field, "")).strip():
            raise PlannerError(f"{tool} needs a non-empty {field!r}")


def _fallback(goal: str, history: list[dict]) -> dict:
    """No LLM key -> treat the goal itself as one shell command."""
    if history:
        return {
            "tool": "done",
            "outcome": history[-1]["observation"],
            "thought": "no LLM configured, single command already run",
        }
    return {
        "tool": "run_command",
        "command": goal,
        "thought": "no LLM configured, running the goal as a command",
    }


def workspace_files() -> list[str]:
    """Grounding list from the terminal (remote sandbox or local workspace)."""
    try:
        from .executor import workspace_files as terminal_files

        return terminal_files()
    except Exception:  # noqa: BLE001
        return []
