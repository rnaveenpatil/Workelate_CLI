"""HTTP surface for the agent."""

from fastapi import APIRouter

from . import agent, cli, crawler, llm
from .schemas import TaskRequest, TaskResult

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "agent": "Workelate_CLI",
        "cli_mode": cli.mode(),
        "llm_configured": llm.is_configured(),
        "github_configured": crawler.is_configured(),
    }


@router.get("/files")
def files(prefix: str = "") -> dict:
    return {"files": crawler.list_files(prefix=prefix)}


@router.post("/task", response_model=TaskResult)
def task(req: TaskRequest) -> TaskResult:
    return agent.run_task(req.goal, max_steps=req.max_steps, cwd=req.cwd)
