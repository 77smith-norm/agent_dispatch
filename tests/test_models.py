from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_dispatch.models import DispatchRequest


def _payload(
    *,
    agent_id: str = "agent-1",
    endpoint: str = "http://example.com/v1/chat/completions",
    content: str = "hello",
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "endpoint": endpoint,
        "thread": {
            "id": "thread-1",
            "messages": [{"role": "user", "content": content}],
        },
        "model": "test-model",
        "metadata": metadata or {},
    }


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (
            "http://example.com/v1/chat/completions",
            "http://example.com/v1/chat/completions",
        ),
        (
            "http://example.com/v1/chat/completions/",
            "http://example.com/v1/chat/completions/",
        ),
    ],
)
def test_dispatch_request_accepts_endpoint_with_or_without_trailing_slash(
    endpoint: str,
    expected: str,
) -> None:
    request = DispatchRequest.model_validate(_payload(endpoint=endpoint))

    assert str(request.endpoint) == expected


def test_dispatch_request_allows_empty_message_content() -> None:
    request = DispatchRequest.model_validate(_payload(content=""))

    assert request.thread.messages[0].content == ""


def test_dispatch_request_allows_nested_metadata() -> None:
    request = DispatchRequest.model_validate(
        _payload(metadata={"nested": {"enabled": True, "count": 2}})
    )

    assert request.metadata == {"nested": {"enabled": True, "count": 2}}


@pytest.mark.parametrize("agent_id", [" ", "   ", "\t", "\n"])
def test_dispatch_request_rejects_whitespace_only_agent_id(agent_id: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        DispatchRequest.model_validate(_payload(agent_id=agent_id))

    errors = exc_info.value.errors(include_url=False)
    assert errors[0]["loc"] == ("agent_id",)
