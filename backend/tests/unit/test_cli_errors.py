"""How the CLI fails.

`_run` is the only place that owns the event loop and the rendering of an API failure, so it is
also the only place that decides whether a user sees one actionable line or a stack trace. These
tests need no database: they are about the boundary, not about what is behind it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from crucible.cli import main as cli
from crucible.cli.client import ApiError
from crucible.cli.config import ConfigError, load_config


def test_an_api_error_becomes_an_exit_code_not_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    async def rejected() -> None:
        raise ApiError(401, "UNAUTHORIZED", "invalid API key", "req-4f2a")

    with pytest.raises(typer.Exit) as excinfo:
        cli._run(rejected())

    assert excinfo.value.exit_code == cli.EXIT_USAGE
    # The request id is the whole point: it turns "it didn't work" into something supportable.
    assert "req-4f2a" in capsys.readouterr().err


def test_a_server_error_exits_failed_rather_than_usage() -> None:
    """A 500 is not the caller's fault, so it must not be reported as a usage problem."""

    async def broke() -> None:
        raise ApiError(503, "UNAVAILABLE", "database is not ready", None)

    with pytest.raises(typer.Exit) as excinfo:
        cli._run(broke())
    assert excinfo.value.exit_code == cli.EXIT_FAILED


def test_a_config_error_is_reported_with_its_message(capsys: pytest.CaptureFixture[str]) -> None:
    async def bad_config() -> None:
        raise ConfigError("evals/golden.jsonl:2 is not valid JSON")

    with pytest.raises(typer.Exit):
        cli._run(bad_config())
    assert "golden.jsonl:2" in capsys.readouterr().err


def test_a_missing_api_key_is_a_usage_error_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "crucible.yaml").write_text("project: demo\n")
    monkeypatch.delenv("CRUCIBLE_API_KEY", raising=False)
    config = load_config(tmp_path / "crucible.yaml")

    with pytest.raises(typer.Exit) as excinfo:
        cli._client(config)
    assert excinfo.value.exit_code == cli.EXIT_USAGE
    assert "CRUCIBLE_API_KEY" in capsys.readouterr().err
