from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class DispatchState(StrEnum):
    PENDING = "PENDING"
    REPLIED = "REPLIED"
    FAILED = "FAILED"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: MessageRole
    content: str | list[dict[str, Any]]
    name: str | None = None


class Thread(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1)
    endpoint: AnyHttpUrl
    thread: Thread
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DispatchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    request_id: str
    agent_id: str
    endpoint: AnyHttpUrl
    thread_id: str
    request: DispatchRequest
    state: DispatchState
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    response: Any | None = None
    error_message: str | None = None
