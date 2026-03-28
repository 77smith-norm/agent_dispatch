# AGENTS.md — agent_dispatch

## What This Is
An agent-first Python CLI application for dispatching request/response messages to OpenAI-compatible endpoints (specifically Hermes Agent and OpenClaw). It uses SQLite for precise state management, enforcing rate limits, and implementing a "walkie-talkie" protocol (waiting for an agent's response before dispatching the next request to it).

## Build & Test
- Run tests: `uv run pytest`
- Property-based tests: `uv run pytest tests/test_properties.py`
- Lint & format: `uv run ruff check . --fix && uv run ruff format .`
- Type check: `uv run ty`
- Full quality gate (must pass before commit): `uv run ruff check . && uv run ty && uv run pytest`

## Project Structure
- `src/agent_dispatch/` — application source
- `tests/` — pytest and Hypothesis tests
- `docs/` — plans and design docs (plans named YYYY-MM-DD-x-plan.md)

## TCR Discipline
All implementation follows TCR: `test && commit || revert`.
Write one test → run suite → GREEN: commit / RED: revert and decompose.
Always do a refactor pass (`TCR+R`) while the state is green, then commit again.

## Compaction Recovery
If context compacts: (1) re-read the active plan in `docs/`, (2) run `git log --oneline -20`, (3) run the full test suite (`uv run pytest`), (4) resume from the next incomplete step. Do not restart committed work.

## Conventions
- Every CLI command must support `--output json`.
- Exit codes are semantic (0 = success, 1 = error, specific codes for rate limit / auth errors).
- Inputs should be accepted as raw JSON (`--json '{...}'`) to map directly to the API schema.
- Database: `state.db` in `~/.config/agent_dispatch/` (use `XDG_CONFIG_HOME` pattern).
- Use `pydantic` for strict payload validation.
- The `agent_dispatch schema` command should output the expected Pydantic JSON schemas.

## Off-Limits
- Do not add a web GUI. This is strictly a CLI.
- Do not use asynchronous I/O (`asyncio`) unless absolutely necessary for the SQLite walkie-talkie logic; keep the CLI fast and synchronous if possible, or use standard `httpx` async where network waits are strictly required.