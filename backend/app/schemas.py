"""Request/response shapes. Small on purpose."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskRequest(BaseModel):
    goal: str = Field(..., min_length=1, description="What the user wants done")
    max_steps: int = Field(8, ge=1, le=25)
    cwd: str | None = None


class Step(BaseModel):
    index: int
    tool: str
    input: dict[str, Any]
    observation: str


class TaskResult(BaseModel):
    ok: bool
    goal: str
    mode: Literal["llm", "fallback"]
    steps: list[Step]
    outcome: str
    stopped_because: str
