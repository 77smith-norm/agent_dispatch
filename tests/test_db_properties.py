from __future__ import annotations

import string
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings, strategies as st

from agent_dispatch.db import DispatchDB, InvalidStateTransition, WalkieTalkieViolation
from agent_dispatch.models import DispatchRequest, DispatchState, Message, Thread


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
    endpoint=TOKEN.map(lambda token: f"http://example.com/{token}"),
    thread=THREAD,
    model=st.one_of(st.none(), TOKEN),
)
TERMINAL_STATE = st.sampled_from([DispatchState.REPLIED, DispatchState.FAILED])


def _apply_terminal_state(
    db: DispatchDB,
    dispatch_id: int,
    state: DispatchState,
    sequence: int,
):
    if state is DispatchState.REPLIED:
        return db.mark_replied(dispatch_id, {"sequence": sequence})

    return db.mark_failed(dispatch_id, f"failed-{sequence}")


@given(request=REQUEST, terminal_states=st.lists(TERMINAL_STATE, min_size=1, max_size=4))
def test_terminal_state_transitions_only_once(request: DispatchRequest, terminal_states: list[DispatchState]) -> None:
    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        dispatch = db.record_pending(request)
        first_terminal = terminal_states[0]

        for sequence, terminal_state in enumerate(terminal_states):
            if sequence == 0:
                updated = _apply_terminal_state(db, dispatch.id, terminal_state, sequence)
                assert updated.state is terminal_state
                continue

            with pytest.raises(InvalidStateTransition):
                _apply_terminal_state(db, dispatch.id, terminal_state, sequence)

        stored = db.get_dispatch(dispatch.id)
        assert stored.state is first_terminal
        assert db.can_dispatch(request.agent_id)


@given(request=REQUEST, outcomes=st.lists(TERMINAL_STATE, min_size=1, max_size=6))
def test_walkie_talkie_reopens_after_terminal_transition(
    request: DispatchRequest,
    outcomes: list[DispatchState],
) -> None:
    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")

        for sequence, outcome in enumerate(outcomes):
            current = db.record_pending(request)

            with pytest.raises(WalkieTalkieViolation):
                db.record_pending(request)

            finished = _apply_terminal_state(db, current.id, outcome, sequence)
            assert finished.state is outcome
            assert db.can_dispatch(request.agent_id)

        records = db.list_dispatches(agent_id=request.agent_id)
        assert len(records) == len(outcomes)
        assert all(record.state is not DispatchState.PENDING for record in records)


@given(request=REQUEST, attempts=st.integers(min_value=2, max_value=8))
@settings(deadline=None, max_examples=25)
def test_walkie_talkie_blocks_concurrent_pending_requests(
    request: DispatchRequest,
    attempts: int,
) -> None:
    with TemporaryDirectory() as tempdir:
        db = DispatchDB(Path(tempdir) / "state.db")
        barrier = threading.Barrier(attempts)

        def try_record_pending(_: int) -> tuple[str, int | None]:
            barrier.wait()

            try:
                dispatch = db.record_pending(request)
            except WalkieTalkieViolation:
                return ("blocked", None)

            return ("accepted", dispatch.id)

        with ThreadPoolExecutor(max_workers=attempts) as executor:
            results = list(executor.map(try_record_pending, range(attempts)))

        accepted = [dispatch_id for status, dispatch_id in results if status == "accepted"]
        blocked = [status for status, _ in results if status == "blocked"]
        records = db.list_dispatches(agent_id=request.agent_id)
        pending = [record for record in records if record.state is DispatchState.PENDING]

        assert len(accepted) == 1
        assert len(blocked) == attempts - 1
        assert len(records) == 1
        assert len(pending) == 1
