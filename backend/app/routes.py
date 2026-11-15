"""HTTP surface for the agent."""

from fastapi import APIRouter

from . import agent, cli, crawler, executor, llm, planner
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
        "github": crawler.status(),
    }


@router.get("/tools")
def tools() -> dict:
    """The tool surface the planner may choose from."""
    return {"tools": list(planner.TOOLS), "executor_tools": list(executor.TOOLS)}


@router.get("/files")
def files(prefix: str = "") -> dict:
    """GitHub listing when configured, otherwise a local workspace walk."""
    if crawler.is_configured():
        return {"source": "github", "files": crawler.list_files(prefix=prefix)}
    listing = executor.workspace_files()
    if prefix:
        listing = [p for p in listing if p.startswith(prefix)]
    return {"source": "remote" if cli.mode() == "remote" else "local", "files": listing}


@router.post("/task", response_model=TaskResult)
def task(req: TaskRequest) -> TaskResult:
    return agent.run_task(req.goal, max_steps=req.max_steps, cwd=req.cwd)
