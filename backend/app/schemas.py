"""Request/response shapes for the Workelate_CLI API."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class TaskRequest(BaseModel):
    goal: str = Field(..., min_length=1, description="What the user wants done")
    max_steps: int = Field(8, ge=1, le=25)
    cwd: str | None = None
    conversation: list[ConversationMessage] = Field(default_factory=list, max_length=16)


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
