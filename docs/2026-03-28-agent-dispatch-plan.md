# 2026-03-28 Agent Dispatch Plan

## Goal
Build an agent-first Python CLI application (`agent_dispatch`) to systematically and deterministically dispatch request/response messages to OpenAI-compatible endpoints (specifically Hermes Agent and OpenClaw).

## Constraints
1. **SQLite State Management:** Record every outbound dispatch and inbound response with datetimes. Enforce a "walkie-talkie" constraint (block or queue new requests to a specific agent if a `PENDING` request is already in-flight).
2. **OpenAI Endpoint Compatibility:** Target OpenClaw (`v2026.3.24`) and Hermes Agent (`v2026.3.23`) endpoints.
3. **Agent-Optimized Input/Output:** Accept raw JSON (`--json`) that maps directly to the API payload. Output structured JSON (`--output json`).
4. **Schema Introspection:** Provide an `agent_dispatch schema` command.
5. **Quality:** `uv`, `ruff`, `ty`, `pytest`, `Hypothesis` (property-based testing).

## Phases

### Phase 1: Harness & Scaffolding (COMPLETED)
- Create repository with `uv init`.
- Configure `AGENTS.md` and this plan document.
- Install dependencies (`httpx`, `pydantic`, `typer`, `rich`, `sqlite3`).
- Install dev dependencies (`pytest`, `hypothesis`, `ruff`, `ty`).
- Initialize Git repository.

### Phase 2: SQLite State & Domain Models
- Define Pydantic models for `Message`, `Thread`, and `DispatchRequest`.
- Create the SQLite wrapper (`src/agent_dispatch/db.py`) storing timestamps, agent target IDs, and state (`PENDING`, `REPLIED`, `FAILED`).
- Implement the "walkie-talkie" validation function in the DB wrapper (returns `False`/Error if an agent currently has a `PENDING` state).
- **Validation Contract:** Write `Hypothesis` property tests to ensure state transitions (`PENDING` -> `REPLIED` / `FAILED`) are valid and the walkie-talkie block works under concurrency/rapid requests.
  - `uv run pytest tests/test_db_properties.py`

### Phase 3: Core CLI & Pydantic Validation
- Set up Typer CLI (`src/agent_dispatch/cli.py`).
- Create `agent_dispatch schema` command to output the Pydantic JSON schema of the expected `DispatchRequest`.
- Create `agent_dispatch send --json '{...}'` command. Parse and validate the input JSON with Pydantic before it touches the DB or network.
- Ensure all outputs are formatted as `--output json`.
- **Validation Contract:** `uv run pytest tests/test_cli.py`

### Phase 4: Network Dispatch (OpenAI SDK Compatibility)
- Integrate `httpx` to POST the payload to the provided endpoint URL (e.g., `http://127.0.0.1:8001/v1/chat/completions`).
- If walkie-talkie validation passes, save to DB as `PENDING`, make the request, await the response, and update DB to `REPLIED` or `FAILED` based on network outcome.
- Handle `429 Too Many Requests` or connection errors properly, updating the DB and exiting with semantic exit codes.
- **Validation Contract:** `uv run pytest tests/test_network.py` (using `respx` or similar httpx mocker, if needed, or pure unit tests on the network layer).

## Notes
- Start Phase 2 by scaffolding the DB schema and tests first.
- Run `uv run ruff check .` and `uv run ty` before every commit.