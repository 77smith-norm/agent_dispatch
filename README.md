[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

# agent_dispatch

A walkie-talkie dispatch protocol CLI for OpenAI-compatible agent endpoints.

`agent_dispatch` solves a coordination problem that shows up when multiple callers
need to send request/response traffic to the same agent endpoint without
overlapping in-flight work. It records every dispatch in SQLite and enforces a
walkie-talkie rule per `agent_id`: do not dispatch the next request until the
previous one has reached a terminal state.

## Installation

```bash
pip install agent-dispatch
```

```bash
uv add agent-dispatch
```

## What It Does

- Validates raw JSON payloads with Pydantic before they touch SQLite or the network.
- Persists outbound requests and inbound responses in `state.db`.
- Serializes same-agent traffic so only one request is `PENDING` for an agent at a time.
- Talks to any OpenAI-compatible HTTP endpoint with JSON output for both success and error paths.

## Usage

### 1. Inspect the request schema

```bash
agent_dispatch schema --output json
```

Example output:

```json
{
  "$defs": {
    "Message": {
      "additionalProperties": false,
      "properties": {
        "content": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "items": {
                "additionalProperties": true,
                "type": "object"
              },
              "type": "array"
            }
          ],
          "title": "Content"
        },
        "role": {
          "$ref": "#/$defs/MessageRole"
        }
      },
      "required": [
        "role",
        "content"
      ],
      "title": "Message",
      "type": "object"
    }
  },
  "properties": {
    "agent_id": {
      "minLength": 1,
      "title": "Agent Id",
      "type": "string"
    },
    "endpoint": {
      "format": "uri",
      "minLength": 1,
      "title": "Endpoint",
      "type": "string"
    },
    "thread": {
      "$ref": "#/$defs/Thread"
    }
  },
  "required": [
    "agent_id",
    "endpoint",
    "thread"
  ],
  "title": "DispatchRequest",
  "type": "object"
}
```

### 2. Send a request and get JSON output back

```bash
agent_dispatch send \
  --json '{
    "agent_id": "agent-1",
    "endpoint": "http://127.0.0.1:8001/v1/chat/completions",
    "thread": {
      "id": "thread-agent-1",
      "messages": [
        {"role": "user", "content": "hello"}
      ]
    },
    "model": "test-model",
    "metadata": {"source": "cli"}
  }' \
  --output json
```

Representative success output:

```json
{
  "agent_id": "agent-1",
  "completed_at": "2026-03-29T01:55:23.048962Z",
  "created_at": "2026-03-29T01:55:23.046608Z",
  "endpoint": "http://127.0.0.1:8001/v1/chat/completions",
  "error_message": null,
  "id": 1,
  "request": {
    "agent_id": "agent-1",
    "endpoint": "http://127.0.0.1:8001/v1/chat/completions",
    "metadata": {
      "source": "cli"
    },
    "model": "test-model",
    "thread": {
      "id": "thread-agent-1",
      "messages": [
        {
          "content": "hello",
          "name": null,
          "role": "user"
        }
      ]
    }
  },
  "request_id": "6edaff6b156c4bf4a80afcc4db334a96",
  "response": {
    "id": "response-1",
    "status": "ok"
  },
  "state": "REPLIED",
  "thread_id": "thread-agent-1",
  "updated_at": "2026-03-29T01:55:23.048962Z"
}
```

### 3. Handle validation failures as JSON

```bash
agent_dispatch send --json '{"agent_id":"agent-1"}' --output json
```

Example error output:

```json
{
  "error": {
    "code": "validation_error",
    "details": [
      {
        "input": {
          "agent_id": "agent-1"
        },
        "loc": [
          "endpoint"
        ],
        "msg": "Field required",
        "type": "missing"
      }
    ],
    "message": "dispatch request validation failed"
  }
}
```

## Configuration

- `agent_dispatch` stores SQLite state at `${XDG_CONFIG_HOME:-~/.config}/agent_dispatch/state.db`.
- Set `XDG_CONFIG_HOME` if you want the database under a different config root.
- Use `--db-path` in tests or local experiments when you want an isolated database file.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Validation error, dispatch timeout, transport timeout, DNS/connect failure, or other general error |
| `2` | Upstream rate limited the request (`429`) |
| `3` | Upstream authentication/authorization failure (`401` or `403`) |
| `4` | Other network or HTTP dispatch failure |

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ty check
uv run pytest -v
```

Full quality gate used in this repository:

```bash
uv run ruff check .
uv run ty check
uv run pytest -v
```

## License

MIT. See [LICENSE](LICENSE).
