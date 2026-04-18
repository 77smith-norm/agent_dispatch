from __future__ import annotations

import json
import os
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import uuid4

import typer
from pydantic import ValidationError

from agent_dispatch.db import DispatchDB
from agent_dispatch.models import (
    DispatchRecord,
    DispatchRequest,
    DispatchState,
    Message,
)
from agent_dispatch.network import (
    DispatchAuthenticationError,
    DispatchError,
    DispatchNetworkError,
    DispatchRateLimitError,
    DispatchTimeoutError,
    dispatch_request_sync,
)


app = typer.Typer(add_completion=False)


class ExitCode(IntEnum):
    SUCCESS = 0
    ERROR = 1
    RATE_LIMIT = 2
    AUTH = 3


class OutputFormat(StrEnum):
    JSON = "json"


OutputOption = Annotated[
    OutputFormat,
    typer.Option("--output", case_sensitive=False),
]
JsonInputOption = Annotated[str | None, typer.Option("--json")]
DbPathOption = Annotated[Path | None, typer.Option("--db-path")]
TimeoutOption = Annotated[float, typer.Option("--timeout", min=0.0)]
DispatchIdArgument = Annotated[int, typer.Argument()]
MessageOption = Annotated[str | None, typer.Option("--message")]
EndpointOption = Annotated[str | None, typer.Option("--endpoint")]
AgentOption = Annotated[str | None, typer.Option("--agent")]
ModelOption = Annotated[str | None, typer.Option("--model")]
ThreadIdOption = Annotated[str | None, typer.Option("--thread-id")]


def _default_db_path() -> Path:
    config_home = (
        Path(os.environ["XDG_CONFIG_HOME"])
        if "XDG_CONFIG_HOME" in os.environ
        else Path.home() / ".config"
    )
    return config_home / "agent_dispatch" / "state.db"


def _render_json(payload: Any, *, output: OutputFormat) -> None:
    if output is not OutputFormat.JSON:
        raise RuntimeError(f"unsupported output format: {output}")

    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _emit_error(
    *,
    output: OutputFormat,
    code: str,
    message: str,
    exit_code: int = ExitCode.ERROR,
    details: list[dict[str, Any]] | None = None,
) -> NoReturn:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details

    payload = {"error": error}

    _render_json(payload, output=output)
    raise typer.Exit(code=int(exit_code))


def _validation_error_details(error: ValidationError) -> list[dict[str, Any]]:
    return json.loads(error.json(include_url=False))


def _dispatch_error_details(error: DispatchError) -> list[dict[str, Any]] | None:
    detail: dict[str, Any] = {}
    if error.dispatch_id is not None:
        detail["dispatch_id"] = error.dispatch_id
    if error.status_code is not None:
        detail["status_code"] = error.status_code

    return [detail] if detail else None


def _validate_dispatch_id(dispatch_id: int, *, output: OutputFormat) -> int:
    if dispatch_id < 1:
        _emit_error(
            output=output,
            code="invalid_dispatch_id",
            message="dispatch_id must be a positive integer",
        )

    return dispatch_id


def _dispatch_payload(dispatch: DispatchRecord) -> dict[str, Any]:
    return {
        "dispatch_id": dispatch.id,
        "agent_id": dispatch.agent_id,
        "endpoint": str(dispatch.endpoint),
        "state": dispatch.state.value,
        "request": dispatch.request.model_dump(mode="json"),
        "response": dispatch.response,
        "error": dispatch.error_message,
        "timestamps": {
            "created_at": dispatch.created_at.isoformat(),
            "updated_at": dispatch.updated_at.isoformat(),
            "completed_at": (
                dispatch.completed_at.isoformat()
                if dispatch.completed_at is not None
                else None
            ),
        },
    }


def _validate_request_or_error(payload: str | dict[str, Any], *, output: OutputFormat) -> DispatchRequest:
    try:
        if isinstance(payload, str):
            return DispatchRequest.model_validate_json(payload)
        return DispatchRequest.model_validate(payload)
    except ValidationError as exc:
        _emit_error(
            output=output,
            code="validation_error",
            message="dispatch request validation failed",
            exit_code=ExitCode.ERROR,
            details=_validation_error_details(exc),
        )


def _normalize_endpoint(endpoint: str) -> str:
    normalized = endpoint.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized

    return f"{normalized}/chat/completions"


def _parse_message_override(
    message: str,
    *,
    output: OutputFormat,
) -> Message | str:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return message

    if not isinstance(payload, dict):
        return message

    try:
        return Message.model_validate(payload)
    except ValidationError as exc:
        _emit_error(
            output=output,
            code="validation_error",
            message="message validation failed",
            exit_code=ExitCode.ERROR,
            details=_validation_error_details(exc),
        )


def _build_send_request(
    *,
    json_input: str | None,
    endpoint: str | None,
    agent: str | None,
    message: str | None,
    model: str | None,
    thread_id: str | None,
    output: OutputFormat,
) -> DispatchRequest:
    has_json_input = json_input is not None
    has_agent_flags = any(
        value is not None for value in (endpoint, agent, message, model, thread_id)
    )

    if has_json_input and has_agent_flags:
        _emit_error(
            output=output,
            code="invalid_send_input",
            message=(
                "provide either --json or the --endpoint/--agent/--message flags, "
                "but not both"
            ),
        )

    if has_json_input:
        assert json_input is not None
        return _validate_request_or_error(json_input, output=output)

    if endpoint is None or agent is None or message is None:
        _emit_error(
            output=output,
            code="invalid_send_input",
            message="provide either --json or all of --endpoint, --agent, and --message",
        )

    message_override = _parse_message_override(message, output=output)
    request_payload: dict[str, Any] = {
        "agent_id": agent,
        "endpoint": _normalize_endpoint(endpoint),
        "thread": {
            "id": thread_id or uuid4().hex,
            "messages": [
                (
                    message_override.model_dump(mode="json", exclude_none=True)
                    if isinstance(message_override, Message)
                    else {"role": "user", "content": message_override}
                )
            ],
        },
    }
    if model is not None:
        request_payload["model"] = model

    return _validate_request_or_error(request_payload, output=output)


def _get_dispatch_or_error(
    database: DispatchDB,
    dispatch_id: int,
    *,
    output: OutputFormat,
) -> DispatchRecord:
    try:
        return database.get_dispatch(dispatch_id)
    except KeyError:
        _emit_error(
            output=output,
            code="dispatch_not_found",
            message=f"dispatch {dispatch_id} was not found",
        )


def _dispatch_request_or_error(
    database: DispatchDB,
    request: DispatchRequest,
    *,
    output: OutputFormat,
    timeout: float = 120.0,
) -> DispatchRecord:
    try:
        return dispatch_request_sync(database, request, timeout=timeout)
    except DispatchRateLimitError as exc:
        _emit_error(
            output=output,
            code=exc.error_code,
            message=str(exc),
            exit_code=ExitCode.RATE_LIMIT,
            details=_dispatch_error_details(exc),
        )
    except DispatchAuthenticationError as exc:
        _emit_error(
            output=output,
            code=exc.error_code,
            message=str(exc),
            exit_code=ExitCode.AUTH,
            details=_dispatch_error_details(exc),
        )
    except DispatchNetworkError as exc:
        _emit_error(
            output=output,
            code=exc.error_code,
            message=str(exc),
            exit_code=ExitCode.ERROR,
            details=_dispatch_error_details(exc),
        )
    except DispatchTimeoutError as exc:
        _emit_error(
            output=output,
            code=exc.error_code,
            message=str(exc),
            exit_code=ExitCode.ERROR,
            details=_dispatch_error_details(exc),
        )
    except DispatchError as exc:
        _emit_error(
            output=output,
            code=exc.error_code,
            message=str(exc),
            exit_code=ExitCode.ERROR,
            details=_dispatch_error_details(exc),
        )


def _build_retry_request(
    original_request: DispatchRequest,
    *,
    message: str | None,
    output: OutputFormat,
) -> DispatchRequest:
    if message is None:
        return original_request

    payload = original_request.model_dump(mode="json")
    message_override = _parse_message_override(message, output=output)
    if isinstance(message_override, Message):
        payload["thread"]["messages"][-1] = message_override.model_dump(
            mode="json",
            exclude_none=True,
        )
    else:
        payload["thread"]["messages"][-1]["content"] = message_override
    return _validate_request_or_error(payload, output=output)


@app.command()
def schema(output: OutputOption = OutputFormat.JSON) -> None:
    _render_json(DispatchRequest.model_json_schema(), output=output)


@app.command()
def send(
    json_input: JsonInputOption = None,
    endpoint: EndpointOption = None,
    agent: AgentOption = None,
    message: MessageOption = None,
    model: ModelOption = None,
    thread_id: ThreadIdOption = None,
    db_path: DbPathOption = None,
    timeout: TimeoutOption = 120.0,
    output: OutputOption = OutputFormat.JSON,
) -> None:
    request = _build_send_request(
        json_input=json_input,
        endpoint=endpoint,
        agent=agent,
        message=message,
        model=model,
        thread_id=thread_id,
        output=output,
    )

    database = DispatchDB(db_path or _default_db_path())
    dispatch = _dispatch_request_or_error(
        database,
        request,
        output=output,
        timeout=timeout,
    )

    _render_json(_dispatch_payload(dispatch), output=output)


@app.command()
def follow(
    dispatch_id: DispatchIdArgument,
    db_path: DbPathOption = None,
    output: OutputOption = OutputFormat.JSON,
) -> None:
    validated_dispatch_id = _validate_dispatch_id(dispatch_id, output=output)
    database = DispatchDB(db_path or _default_db_path())
    dispatch = _get_dispatch_or_error(
        database,
        validated_dispatch_id,
        output=output,
    )

    _render_json(_dispatch_payload(dispatch), output=output)


@app.command()
def retry(
    dispatch_id: DispatchIdArgument,
    message: MessageOption = None,
    db_path: DbPathOption = None,
    output: OutputOption = OutputFormat.JSON,
) -> None:
    validated_dispatch_id = _validate_dispatch_id(dispatch_id, output=output)
    database = DispatchDB(db_path or _default_db_path())
    original_dispatch = _get_dispatch_or_error(
        database,
        validated_dispatch_id,
        output=output,
    )

    if original_dispatch.state is not DispatchState.FAILED:
        _emit_error(
            output=output,
            code="dispatch_not_retryable",
            message=(
                f"dispatch {validated_dispatch_id} cannot be retried from state "
                f"{original_dispatch.state.value}"
            ),
        )

    request = _build_retry_request(
        original_dispatch.request,
        message=message,
        output=output,
    )
    dispatch = _dispatch_request_or_error(
        database,
        request,
        output=output,
    )

    _render_json(_dispatch_payload(dispatch), output=output)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
