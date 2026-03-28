from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict, cast

from typer.testing import CliRunner

from agent_dispatch.cli import app
from agent_dispatch.db import DispatchDB
from agent_dispatch.models import DispatchState


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


def test_send_records_pending_dispatch_after_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    request = _request_payload()

    result = runner.invoke(
        app,
        _send_command(db_path, request) + ["--output", "json"],
    )

    assert result.exit_code == 0
    payload = cast(dict[str, Any], json.loads(result.stdout))

    assert payload["state"] == "PENDING"
    assert payload["agent_id"] == request["agent_id"]
    assert payload["request"]["thread"]["id"] == request["thread"]["id"]

    db = DispatchDB(db_path)
    records = db.list_dispatches(agent_id=request["agent_id"])

    assert len(records) == 1
    assert records[0].state is DispatchState.PENDING


def test_send_rejects_second_pending_dispatch_for_agent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    request = _request_payload()

    first = runner.invoke(app, _send_command(db_path, request))
    second = runner.invoke(app, _send_command(db_path, request))

    assert first.exit_code == 0
    assert second.exit_code == 1

    payload = json.loads(second.stdout)
    assert payload["error"]["code"] == "walkie_talkie_violation"

    db = DispatchDB(db_path)
    records = db.list_dispatches(agent_id=request["agent_id"])

    assert len(records) == 1
    assert records[0].state is DispatchState.PENDING
