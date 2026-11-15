"""HTTP surface for the agent."""

from fastapi import APIRouter

from . import agent, cli, executor, llm, planner
from .schemas import TaskRequest, TaskResult

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "agent": "Workelate_CLI",
        "tools": list(planner.TOOLS),
        "terminal": cli.status(),
        "llm": llm.status(),
    }


@router.get("/tools")
def tools() -> dict:
    """The tool surface the planner may choose from."""
    return {"tools": list(executor.TOOLS)}


@router.get("/files")
def files(prefix: str = "") -> dict:
    """Workspace listing from the terminal (remote sandbox or local)."""
    listing = executor.workspace_files()
    if prefix:
        listing = [p for p in listing if p.startswith(prefix)]
    return {"source": "remote" if cli.uses_remote() else "local", "files": listing}


@router.post("/task", response_model=TaskResult)
def task(req: TaskRequest) -> TaskResult:
    return agent.run_task(req.goal, max_steps=req.max_steps, cwd=req.cwd)
