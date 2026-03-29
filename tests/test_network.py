from __future__ import annotations

import asyncio
import json
import string
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypedDict

import httpx
import pytest
from hypothesis import given, settings, strategies as st

from agent_dispatch.db import DispatchDB
from agent_dispatch.models import (
    DispatchRequest,
    DispatchState,
    Message,
    MessageRole,
    Thread,
)
from agent_dispatch.network import (
    DispatchAuthenticationError,
    DispatchNetworkError,
    DispatchRateLimitError,
    DispatchTimeoutError,
    dispatch_request,
    record_pending_when_ready,
)


TOKEN = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-_",
    min_size=1,
    max_size=16,
)
MESSAGE = st.builds(
    Message,
    role=st.sampled_from(["system", "user", "assistant"]),
    content=TOKEN,
)
THREAD = st.builds(
    Thread,
    id=TOKEN,
    messages=st.lists(MESSAGE, min_size=1, max_size=4),
)
REQUEST = st.builds(
    DispatchRequest,
    agent_id=TOKEN,
    endpoint=st.just("http://example.com/v1/chat/completions"),
    thread=THREAD,
    model=st.one_of(st.none(), TOKEN),
    metadata=st.dictionaries(TOKEN, TOKEN, max_size=3),
)
TERMINAL_STATE = st.sampled_from([DispatchState.REPLIED, DispatchState.FAILED])
MILLISECONDS = st.integers(min_value=0, max_value=5)


class PollingSchedule(TypedDict):
    start_delays_ms: list[int]
    finish_delays_ms: list[int]
    outcomes: list[DispatchState]
    poll_interval_ms: int
    observer_interval_ms: int


@st.composite
def polling_schedules(draw: st.DrawFn) -> PollingSchedule:
    workers = draw(st.integers(min_value=2, max_value=5))
    return {
        "start_delays_ms": draw(
            st.lists(MILLISECONDS, min_size=workers, max_size=workers)
        ),
        "finish_delays_ms": draw(
            st.lists(MILLISECONDS, min_size=workers, max_size=workers)
        ),
        "outcomes": draw(st.lists(TERMINAL_STATE, min_size=workers, max_size=workers)),
        "poll_interval_ms": draw(MILLISECONDS),
        "observer_interval_ms": draw(MILLISECONDS),
    }


def _request(agent_id: str = "agent-1") -> DispatchRequest:
    return DispatchRequest.model_validate(
        {
            "agent_id": agent_id,
            "endpoint": "http://example.com/v1/chat/completions",
            "thread": {
                "id": f"thread-{agent_id}",
                "messages": [
                    {"role": MessageRole.USER.value, "content": "hello"},
                ],
            },
            "model": "test-model",
            "metadata": {"source": "test"},
        }
    )


def test_dispatch_request_marks_replied_on_success() -> None:
    sent_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": "response-1", "status": "ok"})

    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            dispatch = await dispatch_request(
                db, request, client=client, poll_interval=0
            )
            assert dispatch.state is DispatchState.REPLIED
            assert dispatch.response == {"id": "response-1", "status": "ok"}

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        asyncio.run(scenario(db, request))

        assert sent_payloads == [
            {
                "messages": [{"content": "hello", "role": "user"}],
                "metadata": {"source": "test"},
                "model": "test-model",
            }
        ]
        stored = db.list_dispatches(agent_id=request.agent_id)
        assert len(stored) == 1
        assert stored[0].state is DispatchState.REPLIED


def test_dispatch_request_uses_configured_http_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float | bool] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def post(self, url: str, json: dict[str, Any]) -> httpx.Response:
            request = httpx.Request("POST", url, json=json)
            return httpx.Response(
                200,
                json={"id": "response-1", "status": "ok"},
                request=request,
            )

        async def aclose(self) -> None:
            captured["closed"] = True

    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        dispatch = await dispatch_request(db, request, poll_interval=0, timeout=42.5)
        assert dispatch.state is DispatchState.REPLIED

    with TemporaryDirectory() as tempdir:
        monkeypatch.setattr("agent_dispatch.network.httpx.AsyncClient", FakeAsyncClient)
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        asyncio.run(scenario(db, request))

        assert captured == {"timeout": 42.5, "closed": True}


def test_dispatch_request_marks_failed_on_rate_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DispatchRateLimitError) as exc_info:
                await dispatch_request(db, request, client=client, poll_interval=0)

        assert exc_info.value.status_code == 429

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        asyncio.run(scenario(db, request))

        stored = db.list_dispatches(agent_id=request.agent_id)
        assert len(stored) == 1
        assert stored[0].state is DispatchState.FAILED
        assert (
            stored[0].error_message
            == 'endpoint returned 429: {"error":{"message":"slow down"}}'
        )


def test_dispatch_request_marks_failed_on_auth_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DispatchAuthenticationError) as exc_info:
                await dispatch_request(db, request, client=client, poll_interval=0)

        assert exc_info.value.status_code == 401

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        asyncio.run(scenario(db, request))

        stored = db.list_dispatches(agent_id=request.agent_id)
        assert len(stored) == 1
        assert stored[0].state is DispatchState.FAILED
        assert (
            stored[0].error_message
            == 'endpoint returned 401: {"error":{"message":"bad key"}}'
        )


def test_dispatch_request_marks_failed_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DispatchNetworkError) as exc_info:
                await dispatch_request(db, request, client=client, poll_interval=0)

        assert "connection refused" in str(exc_info.value)

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        asyncio.run(scenario(db, request))

        stored = db.list_dispatches(agent_id=request.agent_id)
        assert len(stored) == 1
        assert stored[0].state is DispatchState.FAILED
        assert stored[0].error_message == "connection error: connection refused"


def test_dispatch_request_marks_failed_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DispatchNetworkError) as exc_info:
                await dispatch_request(db, request, client=client, poll_interval=0)

        assert "timed out" in str(exc_info.value)

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        asyncio.run(scenario(db, request))

        stored = db.list_dispatches(agent_id=request.agent_id)
        assert len(stored) == 1
        assert stored[0].state is DispatchState.FAILED
        assert stored[0].error_message == "connection error: timed out"


def test_dispatch_request_marks_failed_on_server_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DispatchNetworkError) as exc_info:
                await dispatch_request(db, request, client=client, poll_interval=0)

        assert exc_info.value.status_code == 500

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        asyncio.run(scenario(db, request))

        stored = db.list_dispatches(agent_id=request.agent_id)
        assert len(stored) == 1
        assert stored[0].state is DispatchState.FAILED
        assert stored[0].error_message == "endpoint returned 500: server exploded"


def test_record_pending_when_ready_times_out_without_terminal_transition() -> None:
    async def scenario(db: DispatchDB, request: DispatchRequest) -> None:
        with pytest.raises(DispatchTimeoutError):
            await record_pending_when_ready(
                db,
                request,
                poll_interval=0,
                timeout=0.01,
            )

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        request = _request()

        db.record_pending(request)
        asyncio.run(scenario(db, request))

        stored = db.list_dispatches(agent_id=request.agent_id)
        assert len(stored) == 1
        assert stored[0].state is DispatchState.PENDING


@given(request=REQUEST, schedule=polling_schedules())
@settings(deadline=None, max_examples=30)
def test_record_pending_when_ready_serializes_concurrent_waiters(
    request: DispatchRequest,
    schedule: PollingSchedule,
) -> None:
    start_delays = schedule["start_delays_ms"]
    finish_delays = schedule["finish_delays_ms"]
    outcomes = schedule["outcomes"]
    poll_interval_ms = schedule["poll_interval_ms"]
    observer_interval_ms = schedule["observer_interval_ms"]

    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        acquired_ids: list[int | None] = [None] * len(outcomes)

        async def observer(stop: asyncio.Event) -> list[int]:
            pending_counts: list[int] = []

            while not stop.is_set():
                records = await asyncio.to_thread(
                    db.list_dispatches,
                    agent_id=request.agent_id,
                )
                pending_count = sum(
                    record.state is DispatchState.PENDING for record in records
                )
                pending_counts.append(pending_count)
                assert pending_count <= 1
                await asyncio.sleep(observer_interval_ms / 1000)

            records = await asyncio.to_thread(
                db.list_dispatches,
                agent_id=request.agent_id,
            )
            pending_count = sum(
                record.state is DispatchState.PENDING for record in records
            )
            pending_counts.append(pending_count)
            assert pending_count <= 1
            return pending_counts

        async def worker(index: int) -> None:
            await asyncio.sleep(start_delays[index] / 1000)
            dispatch = await record_pending_when_ready(
                db,
                request,
                poll_interval=poll_interval_ms / 1000,
                timeout=1.0,
            )
            acquired_ids[index] = dispatch.id

            records = await asyncio.to_thread(
                db.list_dispatches,
                agent_id=request.agent_id,
            )
            pending_ids = [
                record.id for record in records if record.state is DispatchState.PENDING
            ]
            assert pending_ids == [dispatch.id]

            await asyncio.sleep(finish_delays[index] / 1000)
            if outcomes[index] is DispatchState.REPLIED:
                await asyncio.to_thread(
                    db.mark_replied,
                    dispatch.id,
                    {"worker": index},
                )
                return

            await asyncio.to_thread(db.mark_failed, dispatch.id, f"failed-{index}")

        async def scenario() -> list[int]:
            stop = asyncio.Event()
            observer_task = asyncio.create_task(observer(stop))

            try:
                async with asyncio.TaskGroup() as task_group:
                    for index in range(len(outcomes)):
                        task_group.create_task(worker(index))
            finally:
                stop.set()

            return await observer_task

        pending_counts = asyncio.run(scenario())
        records = db.list_dispatches(agent_id=request.agent_id)

        assert pending_counts
        assert all(count <= 1 for count in pending_counts)
        assert all(dispatch_id is not None for dispatch_id in acquired_ids)
        assert len(
            {dispatch_id for dispatch_id in acquired_ids if dispatch_id is not None}
        ) == len(outcomes)
        assert len(records) == len(outcomes)
        assert all(record.state is not DispatchState.PENDING for record in records)
