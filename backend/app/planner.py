"""Decides the next action. One LLM call per step; tiny rule fallback if no key.

LLM must reply with JSON:
    {"tool": "run_command" | "read_file" | "write_file" | "done",
     "command" | "path" | "content": "...",
     "thought": "one short line"}
"""

from . import crawler, llm

SYSTEM = """You are Workelate_CLI, an agent with a shell tool on the user's workspace.
Tools:
  run_command -> {"tool":"run_command","command":"<shell>","thought":"..."}
  read_file   -> {"tool":"read_file","path":"<rel path>","thought":"..."}
  write_file  -> {"tool":"write_file","path":"<rel path>","content":"<text>","thought":"..."}
  done        -> {"tool":"done","outcome":"<final answer for the user>"}

Rules: one action per reply. Prefer few steps. Inspect before you change.
Reply with JSON only."""


def next_action(goal: str, history: list[dict], files: list[str] | None = None) -> dict:
    if not llm.is_configured():
        return _fallback(goal, history)

    context = "\n".join(
        f"[{h['index']}] {h['tool']} {h['input']}\n  -> {h['observation']}" for h in history
    ) or "(nothing yet)"

    listing = ""
    if files:
        listing = "\nWorkspace files: " + ", ".join(files[:60])

    user = (
        f"GOAL: {goal}{listing}\n\n"
        f"STEPS SO FAR:\n{context}\n\n"
        "Reply with the single next JSON action."
    )
    return llm.chat_json(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    )


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
    """Optional repo listing to ground the LLM. Empty when GitHub is unset."""
    return crawler.list_files()
