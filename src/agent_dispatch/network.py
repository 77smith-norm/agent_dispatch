from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent_dispatch.db import DispatchDB, WalkieTalkieViolation
from agent_dispatch.models import DispatchRecord, DispatchRequest


class DispatchError(RuntimeError):
    error_code = "dispatch_error"

    def __init__(
        self,
        message: str,
        *,
        dispatch_id: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.dispatch_id = dispatch_id
        self.status_code = status_code


class DispatchTimeoutError(DispatchError):
    error_code = "dispatch_timeout"


class DispatchRateLimitError(DispatchError):
    error_code = "rate_limit"


class DispatchAuthenticationError(DispatchError):
    error_code = "auth_error"


class DispatchNetworkError(DispatchError):
    error_code = "network_error"


async def record_pending_when_ready(
    database: DispatchDB,
    request: DispatchRequest,
    *,
    poll_interval: float = 0.01,
    timeout: float | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DispatchRecord:
    if poll_interval < 0:
        raise ValueError("poll_interval must be non-negative")
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")

    deadline = None if timeout is None else monotonic() + timeout

    while True:
        try:
            return await asyncio.to_thread(database.record_pending, request)
        except WalkieTalkieViolation as exc:
            _raise_wait_timeout_if_needed(
                agent_id=request.agent_id,
                deadline=deadline,
                monotonic=monotonic,
                cause=exc,
            )

        await sleep(poll_interval)


async def dispatch_request(
    database: DispatchDB,
    request: DispatchRequest,
    *,
    client: httpx.AsyncClient | None = None,
    poll_interval: float = 0.01,
    timeout: float = 120.0,
    wait_timeout: float | None = None,
) -> DispatchRecord:
    dispatch = await record_pending_when_ready(
        database,
        request,
        poll_interval=poll_interval,
        timeout=wait_timeout,
    )
    payload = build_request_payload(request)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout)

    try:
        response = await _post_response_or_error(
            http_client,
            database=database,
            request=request,
            payload=payload,
            dispatch_id=dispatch.id,
            timeout=timeout,
        )
        await _raise_for_error_response(
            database,
            dispatch_id=dispatch.id,
            response=response,
        )
        response_payload = await _response_payload_or_error(
            database,
            dispatch_id=dispatch.id,
            response=response,
        )
        return await asyncio.to_thread(
            database.mark_replied, dispatch.id, response_payload
        )
    finally:
        if owns_client:
            await http_client.aclose()


def dispatch_request_sync(
    database: DispatchDB,
    request: DispatchRequest,
    *,
    poll_interval: float = 0.01,
    timeout: float = 120.0,
    wait_timeout: float | None = None,
) -> DispatchRecord:
    return asyncio.run(
        dispatch_request(
            database,
            request,
            poll_interval=poll_interval,
            timeout=timeout,
            wait_timeout=wait_timeout,
        )
    )


def build_request_payload(request: DispatchRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": [
            message.model_dump(mode="json", exclude_none=True)
            for message in request.thread.messages
        ]
    }
    if request.model is not None:
        payload["model"] = request.model
    if request.metadata:
        payload["metadata"] = request.metadata

    return payload


def _raise_wait_timeout_if_needed(
    *,
    agent_id: str,
    deadline: float | None,
    monotonic: Callable[[], float],
    cause: WalkieTalkieViolation,
) -> None:
    if deadline is None:
        return
    if monotonic() < deadline:
        return

    raise DispatchTimeoutError(
        f"timed out waiting for agent {agent_id!r} to clear its pending dispatch"
    ) from cause


async def _post_response_or_error(
    client: httpx.AsyncClient,
    *,
    database: DispatchDB,
    request: DispatchRequest,
    payload: dict[str, Any],
    dispatch_id: int,
    timeout: float,
) -> httpx.Response:
    try:
        return await client.post(str(request.endpoint), json=payload)
    except httpx.TimeoutException as exc:
        message = f"request timed out after {timeout}s"
        await _mark_failed(database, dispatch_id, message)
        raise DispatchTimeoutError(message, dispatch_id=dispatch_id) from exc
    except httpx.TransportError as exc:
        message = f"connection error: {exc}"
        await _mark_failed(database, dispatch_id, message)
        raise DispatchNetworkError(message, dispatch_id=dispatch_id) from exc


async def _raise_for_error_response(
    database: DispatchDB,
    *,
    dispatch_id: int,
    response: httpx.Response,
) -> None:
    error = _response_dispatch_error(response, dispatch_id=dispatch_id)
    if error is None:
        return

    await _mark_failed(database, dispatch_id, str(error))
    raise error


def _response_dispatch_error(
    response: httpx.Response,
    *,
    dispatch_id: int,
) -> DispatchError | None:
    message = _response_error_message(response)
    if response.status_code == 429:
        return DispatchRateLimitError(
            message,
            dispatch_id=dispatch_id,
            status_code=response.status_code,
        )
    if response.status_code in {401, 403}:
        return DispatchAuthenticationError(
            message,
            dispatch_id=dispatch_id,
            status_code=response.status_code,
        )
    if not response.is_error:
        return None

    return DispatchNetworkError(
        message,
        dispatch_id=dispatch_id,
        status_code=response.status_code,
    )


async def _response_payload_or_error(
    database: DispatchDB,
    *,
    dispatch_id: int,
    response: httpx.Response,
) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        message = f"endpoint returned invalid JSON with status {response.status_code}"
        await _mark_failed(database, dispatch_id, message)
        raise DispatchNetworkError(
            message,
            dispatch_id=dispatch_id,
            status_code=response.status_code,
        ) from exc


async def _mark_failed(
    database: DispatchDB,
    dispatch_id: int,
    message: str,
) -> DispatchRecord:
    return await asyncio.to_thread(database.mark_failed, dispatch_id, message)


def _response_error_message(response: httpx.Response) -> str:
    body = response.text.strip()
    if body:
        return f"endpoint returned {response.status_code}: {body}"

    return f"endpoint returned {response.status_code}"


__all__ = [
    "DispatchAuthenticationError",
    "DispatchError",
    "DispatchNetworkError",
    "DispatchRateLimitError",
    "DispatchTimeoutError",
    "build_request_payload",
    "dispatch_request",
    "dispatch_request_sync",
    "record_pending_when_ready",
]
