from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from typer.testing import CliRunner

from agent_dispatch.cli import ExitCode, app
from agent_dispatch.db import DispatchDB
from agent_dispatch.models import DispatchRecord, DispatchRequest, DispatchState
from agent_dispatch.network import DispatchNetworkError, DispatchRateLimitError


runner = CliRunner()


class MessagePayload(TypedDict):
    role: str
    content: str


class ThreadPayload(TypedDict):
    id: str
    messages: list[MessagePayload]


class RequestPayload(TypedDict):
    agent_id: str
    endpoint: str
    thread: ThreadPayload
    model: str
    metadata: dict[str, str]


def _request_payload(agent_id: str = "agent-1") -> RequestPayload:
    return {
        "agent_id": agent_id,
        "endpoint": "http://example.com/v1/chat/completions",
        "thread": {
            "id": f"thread-{agent_id}",
            "messages": [{"role": "user", "content": "hello"}],
        },
        "model": "test-model",
        "metadata": {"source": "test"},
    }


def _send_command(db_path: Path, payload: RequestPayload) -> list[str]:
    return [
        "send",
        "--json",
        json.dumps(payload),
        "--db-path",
        str(db_path),
    ]


def test_schema_outputs_dispatch_request_schema_by_default() -> None:
    result = runner.invoke(app, ["schema"])

    assert result.exit_code == 0
    payload = cast(dict[str, Any], json.loads(result.stdout))

    assert payload["title"] == "DispatchRequest"
    assert {"agent_id", "endpoint", "thread"}.issubset(payload["properties"])
    assert {"agent_id", "endpoint", "thread"}.issubset(payload["required"])


def test_schema_accepts_output_json_flag() -> None:
    result = runner.invoke(app, ["schema", "--output", "json"])

    assert result.exit_code == 0
    payload = cast(dict[str, Any], json.loads(result.stdout))

    assert payload["title"] == "DispatchRequest"


def test_send_rejects_invalid_dispatch_request_before_touching_db(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    result = runner.invoke(
        app,
        [
            "send",
            "--json",
            json.dumps({"agent_id": "agent-1"}),
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 1
    payload = cast(dict[str, Any], json.loads(result.stdout))

    assert payload["error"]["code"] == "validation_error"
    assert not db_path.exists()


def test_send_dispatches_request_and_outputs_terminal_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "state.db"
    request = _request_payload()

    def fake_dispatch(
        database: DispatchDB, dispatch_request: DispatchRequest
    ) -> DispatchRecord:
        dispatch = database.record_pending(dispatch_request)
        return database.mark_replied(dispatch.id, {"id": "response-1"})

    monkeypatch.setattr("agent_dispatch.cli.dispatch_request_sync", fake_dispatch)

    result = runner.invoke(
        app,
        _send_command(db_path, request) + ["--output", "json"],
    )

    assert result.exit_code == 0
    payload = cast(dict[str, Any], json.loads(result.stdout))

    assert payload["state"] == "REPLIED"
    assert payload["agent_id"] == request["agent_id"]
    assert payload["request"]["thread"]["id"] == request["thread"]["id"]
    assert payload["response"]["id"] == "response-1"

    db = DispatchDB(db_path)
    records = db.list_dispatches(agent_id=request["agent_id"])

    assert len(records) == 1
    assert records[0].state is DispatchState.REPLIED


def test_send_returns_rate_limit_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "state.db"
    request = _request_payload()

    def fake_dispatch(
        database: DispatchDB, dispatch_request: DispatchRequest
    ) -> DispatchRecord:
        dispatch = database.record_pending(dispatch_request)
        database.mark_failed(dispatch.id, "endpoint returned 429: slow down")
        raise DispatchRateLimitError(
            "endpoint returned 429: slow down",
            dispatch_id=dispatch.id,
            status_code=429,
        )

    monkeypatch.setattr("agent_dispatch.cli.dispatch_request_sync", fake_dispatch)

    result = runner.invoke(app, _send_command(db_path, request))

    assert result.exit_code == int(ExitCode.RATE_LIMIT)
    payload = cast(dict[str, Any], json.loads(result.stdout))

    assert payload["error"]["code"] == "rate_limit"
    assert payload["error"]["details"] == [{"dispatch_id": 1, "status_code": 429}]

    db = DispatchDB(db_path)
    records = db.list_dispatches(agent_id=request["agent_id"])
    assert len(records) == 1
    assert records[0].state is DispatchState.FAILED


def test_send_returns_network_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "state.db"
    request = _request_payload()

    def fake_dispatch(
        database: DispatchDB, dispatch_request: DispatchRequest
    ) -> DispatchRecord:
        dispatch = database.record_pending(dispatch_request)
        database.mark_failed(dispatch.id, "connection error: refused")
        raise DispatchNetworkError(
            "connection error: refused",
            dispatch_id=dispatch.id,
        )

    monkeypatch.setattr("agent_dispatch.cli.dispatch_request_sync", fake_dispatch)

    result = runner.invoke(app, _send_command(db_path, request))

    assert result.exit_code == int(ExitCode.NETWORK)
    payload = cast(dict[str, Any], json.loads(result.stdout))

    assert payload["error"]["code"] == "network_error"
    assert payload["error"]["details"] == [{"dispatch_id": 1}]

    db = DispatchDB(db_path)
    records = db.list_dispatches(agent_id=request["agent_id"])

    assert len(records) == 1
    assert records[0].state is DispatchState.FAILED
