"""The CLI against the real app.

The command that matters is `crucible run --fail-on-regression`, because its exit code is the gate
a pull request is blocked on. So these tests assert on exit codes as much as on output: a gate that
prints "broken" and exits 0 is worse than no gate at all.

The commands are driven through their coroutines rather than through a subprocess. The process
boundary would add a second event loop and a second database connection pool without testing
anything typer does not already test for us; everything below the `@app.command` decorator — config
resolution, project lookup, idempotency, polling, exit codes — is exercised as written.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import typer
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.cli import main as cli
from crucible.cli.client import CrucibleClient
from crucible.cli.config import CliConfig, load_config
from tests.integration.test_api import CASES, signup
from tests.integration.test_api_run_flow import make_worker

pytestmark = pytest.mark.asyncio

CONFIG = """\
project: acme
targets:
  - name: support-agent
    kind: prompt
    config:
      architecture: single_shot
      model: mock-small
      prompt_template: "{{question}}"
      max_tokens: 128
datasets:
  - name: golden
    path: evals/golden.jsonl
suites:
  - name: default
    dataset: golden
    pass_threshold: 1.0
    assertions:
      - type: equals
        value_from: expected.answer
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A repository as the CLI expects to find one: a config and a dataset beside it."""
    import json

    (tmp_path / "crucible.yaml").write_text(CONFIG)
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals/golden.jsonl").write_text("\n".join(json.dumps(c) for c in CASES) + "\n")
    return tmp_path


async def api_key(client: Any) -> str:
    """Sign up, create the project the config names, and mint a key scoped to it."""
    token = await signup(client)
    headers = {"Authorization": f"Bearer {token}"}
    project = (await client.post("/api/v1/projects", json={"name": "Acme", "slug": "acme"}, headers=headers)).json()
    created = (await client.post(f"/api/v1/projects/{project['id']}/keys", json={"name": "ci"}, headers=headers)).json()
    return str(created["key"])


@pytest.fixture
def cli_config(workspace: Path, asgi_transport: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load the workspace config, and point every command's client at the in-process app."""

    def factory(key: str) -> CliConfig:
        config = load_config(workspace / "crucible.yaml", api_url="http://api", api_key=key)
        # Rich sizes tables to the terminal, and pytest's is 80 columns wide, which truncates run
        # ids into ellipses. Widen it so the assertions can look at what a real terminal shows.
        monkeypatch.setattr(cli, "console", Console(width=200))
        monkeypatch.setattr(cli, "err", Console(stderr=True, width=200))
        monkeypatch.setattr(
            cli, "_client", lambda cfg: CrucibleClient(cfg.api_url, cfg.api_key, transport=asgi_transport)
        )
        return config

    return factory


async def latest_run_id(db: AsyncSession) -> str:
    """Poll for the run the CLI is creating in a concurrent task.

    Read committed means each statement takes a fresh snapshot, so this sees the API's commit
    without the test having to manage a transaction of its own.
    """
    for _ in range(200):
        row = (await db.execute(sa.text("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1"))).first()
        if row is not None:
            return str(row.id)
        await asyncio.sleep(0.02)
    raise AssertionError("the CLI never created a run")


async def test_push_uploads_the_repository_and_is_idempotent(
    client: Any, db: AsyncSession, cli_config: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    key = await api_key(client)
    config = cli_config(key)

    await cli._push(config)
    first = capsys.readouterr().out
    assert "target  support-agent" in first and "new" in first
    assert "dataset golden" in first and "2 cases" in first
    assert "suite   default" in first

    counts = (
        await db.execute(
            sa.text(
                "SELECT (SELECT count(*) FROM target_versions) AS tv,"
                " (SELECT count(*) FROM dataset_versions) AS dv,"
                " (SELECT count(*) FROM suites) AS s"
            )
        )
    ).one()
    assert (counts.tv, counts.dv, counts.s) == (1, 1, 1)

    # CI pushes on every commit, and most commits do not touch the prompt. The second push must
    # not mint a second identical version, or the registry becomes noise within a week.
    await cli._push(config)
    second = capsys.readouterr().out
    assert second.count("unchanged") == 2 and "exists" in second

    after = (
        await db.execute(
            sa.text(
                "SELECT (SELECT count(*) FROM target_versions) AS tv, (SELECT count(*) FROM dataset_versions) AS dv"
            )
        )
    ).one()
    assert (after.tv, after.dv) == (1, 1)


async def test_push_reports_a_suite_that_names_an_unknown_dataset(
    client: Any, cli_config: Any, workspace: Path
) -> None:
    key = await api_key(client)
    (workspace / "crucible.yaml").write_text(CONFIG.replace("dataset: golden", "dataset: typo"))
    config = cli_config(key)

    with pytest.raises(typer.Exit) as excinfo:
        await cli._push(config)
    assert excinfo.value.exit_code == cli.EXIT_USAGE


async def test_an_unknown_project_slug_does_not_silently_use_another_one(
    client: Any, cli_config: Any, workspace: Path
) -> None:
    key = await api_key(client)
    (workspace / "crucible.yaml").write_text(CONFIG.replace("project: acme", "project: not-mine"))
    config = cli_config(key)

    with pytest.raises(typer.Exit) as excinfo:
        await cli._push(config)
    assert excinfo.value.exit_code == cli.EXIT_USAGE


async def test_run_waits_for_the_verdict_and_exits_zero_when_the_suite_passes(
    client: Any, db: AsyncSession, cli_config: Any, patched_session_scope: None, capsys: pytest.CaptureFixture[str]
) -> None:
    key = await api_key(client)
    config = cli_config(key)
    await cli._push(config)
    capsys.readouterr()

    started = asyncio.create_task(
        cli._start_run(
            config,
            suite="default",
            target=None,
            baseline=None,
            fail_on_regression=False,
            threshold=None,
            wait=True,
            timeout=30,
            json_output=False,
        )
    )
    run_id = await latest_run_id(db)
    report = await make_worker().drain(run_id)
    assert report.completed == 2

    with pytest.raises(typer.Exit) as excinfo:
        await started
    assert excinfo.value.exit_code == cli.EXIT_OK

    output = capsys.readouterr().out
    assert "succeeded" in output and "2/2 passed" in output and "100.0%" in output


async def test_run_fails_the_build_when_a_case_that_used_to_pass_now_fails(
    client: Any, db: AsyncSession, cli_config: Any, patched_session_scope: None, capsys: pytest.CaptureFixture[str]
) -> None:
    key = await api_key(client)
    config = cli_config(key)
    await cli._push(config)

    baseline = asyncio.create_task(
        cli._start_run(
            config,
            suite="default",
            target=None,
            baseline=None,
            fail_on_regression=False,
            threshold=None,
            wait=True,
            timeout=30,
            json_output=False,
        )
    )
    baseline_id = await latest_run_id(db)
    await make_worker().drain(baseline_id)
    with pytest.raises(typer.Exit):
        await baseline
    capsys.readouterr()

    # Same suite, same dataset, a model that now answers wrongly: the regression the gate exists
    # to catch. `quality=0.0` is the mock's knob for "answers, but incorrectly".
    current = asyncio.create_task(
        cli._start_run(
            config,
            suite="default",
            target=None,
            baseline=baseline_id,
            fail_on_regression=True,
            threshold=None,
            wait=True,
            timeout=30,
            json_output=False,
        )
    )
    current_id = await _next_run_id(db, baseline_id)
    await make_worker(quality=0.0).drain(current_id)

    with pytest.raises(typer.Exit) as excinfo:
        await current
    assert excinfo.value.exit_code == cli.EXIT_FAILED

    output = capsys.readouterr().out
    assert "2 broken" in output
    assert "case-01" in output and "case-02" in output


async def _next_run_id(db: AsyncSession, exclude: str) -> str:
    for _ in range(200):
        row = (
            await db.execute(
                sa.text("SELECT id FROM runs WHERE id <> :x ORDER BY created_at DESC LIMIT 1"), {"x": exclude}
            )
        ).first()
        if row is not None:
            return str(row.id)
        await asyncio.sleep(0.02)
    raise AssertionError("the CLI never created a second run")


async def test_a_pass_rate_below_the_threshold_fails_even_without_a_baseline(
    client: Any, db: AsyncSession, cli_config: Any, patched_session_scope: None, capsys: pytest.CaptureFixture[str]
) -> None:
    key = await api_key(client)
    config = cli_config(key)
    await cli._push(config)

    started = asyncio.create_task(
        cli._start_run(
            config,
            suite="default",
            target=None,
            baseline=None,
            fail_on_regression=False,
            threshold=0.9,
            wait=True,
            timeout=30,
            json_output=False,
        )
    )
    run_id = await latest_run_id(db)
    await make_worker(quality=0.0).drain(run_id)

    with pytest.raises(typer.Exit) as excinfo:
        await started
    assert excinfo.value.exit_code == cli.EXIT_FAILED
    assert "below the required" in capsys.readouterr().err


async def test_run_without_waiting_returns_immediately_and_names_the_run(
    client: Any, db: AsyncSession, cli_config: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    key = await api_key(client)
    config = cli_config(key)
    await cli._push(config)
    capsys.readouterr()

    await cli._start_run(
        config,
        suite="default",
        target=None,
        baseline=None,
        fail_on_regression=False,
        threshold=None,
        wait=False,
        timeout=30,
        json_output=False,
    )
    assert "queued with 2 cases" in capsys.readouterr().out

    status = (await db.execute(sa.text("SELECT status FROM runs"))).scalar_one()
    assert status == "queued"


async def test_run_against_a_suite_that_was_never_pushed_says_so(client: Any, cli_config: Any) -> None:
    key = await api_key(client)
    config = cli_config(key)

    with pytest.raises(typer.Exit) as excinfo:
        await cli._start_run(
            config,
            suite="default",
            target=None,
            baseline=None,
            fail_on_regression=False,
            threshold=None,
            wait=False,
            timeout=30,
            json_output=False,
        )
    assert excinfo.value.exit_code == cli.EXIT_USAGE


async def test_compare_exits_zero_when_nothing_regressed(
    client: Any, db: AsyncSession, cli_config: Any, patched_session_scope: None, capsys: pytest.CaptureFixture[str]
) -> None:
    key = await api_key(client)
    config = cli_config(key)
    await cli._push(config)

    await cli._start_run(
        config,
        suite="default",
        target=None,
        baseline=None,
        fail_on_regression=False,
        threshold=None,
        wait=False,
        timeout=30,
        json_output=False,
    )
    baseline_id = await latest_run_id(db)
    await make_worker().drain(baseline_id)

    await cli._start_run(
        config,
        suite="default",
        target=None,
        baseline=baseline_id,
        fail_on_regression=False,
        threshold=None,
        wait=False,
        timeout=30,
        json_output=False,
    )
    current_id = await _next_run_id(db, baseline_id)
    await make_worker().drain(current_id)
    capsys.readouterr()

    with pytest.raises(typer.Exit) as excinfo:
        await cli._compare(config, current_id, None, json_output=False)
    assert excinfo.value.exit_code == cli.EXIT_OK
    assert "0 broken" in capsys.readouterr().out


async def test_status_lists_recent_runs(
    client: Any, db: AsyncSession, cli_config: Any, patched_session_scope: None, capsys: pytest.CaptureFixture[str]
) -> None:
    key = await api_key(client)
    config = cli_config(key)
    await cli._push(config)
    await cli._start_run(
        config,
        suite="default",
        target=None,
        baseline=None,
        fail_on_regression=False,
        threshold=None,
        wait=False,
        timeout=30,
        json_output=False,
    )
    run_id = await latest_run_id(db)
    await make_worker().drain(run_id)
    capsys.readouterr()

    await cli._status(config, limit=10)
    output = capsys.readouterr().out
    assert run_id[:8] in output and "succeeded" in output and "100.0%" in output
