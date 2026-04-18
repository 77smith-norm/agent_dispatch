from __future__ import annotations

from pathlib import Path

import pytest

from agent_dispatch.db import DispatchDB, InvalidStateTransition, WalkieTalkieViolation
from agent_dispatch.models import DispatchRequest, DispatchState


def _request(agent_id: str = "agent-1") -> DispatchRequest:
    return DispatchRequest.model_validate(
        {
            "agent_id": agent_id,
            "endpoint": "http://example.com/v1/chat/completions",
            "thread": {
                "id": f"thread-{agent_id}",
                "messages": [{"role": "user", "content": "hello"}],
            },
            "model": "test-model",
        }
    )


def test_get_dispatch_raises_key_error_with_dispatch_id_in_message(
    tmp_path: Path,
) -> None:
    db = DispatchDB(tmp_path / "state.db")

    with pytest.raises(KeyError) as exc_info:
        db.get_dispatch(999)

    assert exc_info.value.args == ("unknown dispatch id 999",)


def test_record_pending_blocks_second_dispatch_for_same_agent(tmp_path: Path) -> None:
    db = DispatchDB(tmp_path / "state.db")
    request = _request()

    db.record_pending(request)

    with pytest.raises(WalkieTalkieViolation) as exc_info:
        db.record_pending(request)

    assert str(exc_info.value) == "agent 'agent-1' already has a pending dispatch"


def test_mark_replied_is_not_idempotent(tmp_path: Path) -> None:
    db = DispatchDB(tmp_path / "state.db")
    dispatch = db.record_pending(_request())

    replied = db.mark_replied(dispatch.id, {"id": "response-1"})

    assert replied.state is DispatchState.REPLIED
    with pytest.raises(InvalidStateTransition) as exc_info:
        db.mark_replied(dispatch.id, {"id": "response-2"})

    assert (
        str(exc_info.value) == f"dispatch {dispatch.id} cannot transition from REPLIED"
    )


def test_get_dispatch_round_trips_replied_payload_and_completed_at(
    tmp_path: Path,
) -> None:
    db = DispatchDB(tmp_path / "state.db")
    dispatch = db.record_pending(_request())
    db.mark_replied(dispatch.id, {"id": "response-1", "status": "ok"})

    stored = db.get_dispatch(dispatch.id)

    assert stored.state is DispatchState.REPLIED
    assert stored.response == {"id": "response-1", "status": "ok"}
    assert stored.error_message is None
    assert stored.completed_at is not None


def test_mark_failed_is_not_idempotent(tmp_path: Path) -> None:
    db = DispatchDB(tmp_path / "state.db")
    dispatch = db.record_pending(_request())

    failed = db.mark_failed(dispatch.id, "boom")

    assert failed.state is DispatchState.FAILED
    with pytest.raises(InvalidStateTransition) as exc_info:
        db.mark_failed(dispatch.id, "still boom")

    assert (
        str(exc_info.value) == f"dispatch {dispatch.id} cannot transition from FAILED"
    )
