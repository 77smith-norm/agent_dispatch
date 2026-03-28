from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from agent_dispatch.db import DispatchDB, WalkieTalkieViolation
from agent_dispatch.models import DispatchRequest


app = typer.Typer(add_completion=False)


class OutputFormat(StrEnum):
    JSON = "json"


OutputOption = Annotated[
    OutputFormat,
    typer.Option("--output", case_sensitive=False),
]
JsonInputOption = Annotated[str, typer.Option("--json")]
DbPathOption = Annotated[Path | None, typer.Option("--db-path")]


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
    details: list[dict[str, Any]] | None = None,
) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details

    payload = {"error": error}

    _render_json(payload, output=output)
    raise typer.Exit(code=1)


@app.command()
def schema(output: OutputOption = OutputFormat.JSON) -> None:
    _render_json(DispatchRequest.model_json_schema(), output=output)


@app.command()
def send(
    json_input: JsonInputOption,
    db_path: DbPathOption = None,
    output: OutputOption = OutputFormat.JSON,
) -> None:
    try:
        request = DispatchRequest.model_validate_json(json_input)
    except ValidationError as exc:
        _emit_error(
            output=output,
            code="validation_error",
            message="dispatch request validation failed",
            details=json.loads(exc.json(include_url=False)),
        )

    database = DispatchDB(db_path or _default_db_path())

    try:
        dispatch = database.record_pending(request)
    except WalkieTalkieViolation as exc:
        _emit_error(
            output=output,
            code="walkie_talkie_violation",
            message=str(exc),
        )

    _render_json(dispatch.model_dump(mode="json"), output=output)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
