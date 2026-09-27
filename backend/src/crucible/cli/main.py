"""`crucible` — the command CI actually runs.

The design goal is one line in a pipeline:

    crucible run --suite default --fail-on-regression

Everything else exists to make that line possible: `push` uploads what the repo says the target is,
`run` waits for the verdict, and the exit code is the gate. Exit codes are part of the contract —
0 passed, 1 the eval says no, 2 the invocation or config is wrong — because a CI step cannot read
prose.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from crucible.cli.client import ApiError, CrucibleClient
from crucible.cli.config import CliConfig, ConfigError, find_config, load_cases, load_config
from crucible.domain.cost import format_usd

app = typer.Typer(
    name="crucible",
    help="CI for AI agents: versioned prompts, golden-dataset evals, regression gates.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err = Console(stderr=True)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _fail(message: str, code: int = EXIT_USAGE) -> None:
    err.print(f"[bold red]error[/bold red] {message}")
    raise typer.Exit(code)


def _run(coro: Any) -> Any:
    """One place that owns the event loop, and one place that renders API errors."""
    try:
        return asyncio.run(coro)
    except ConfigError as exc:
        _fail(str(exc))
    except ApiError as exc:
        details = f"\n  {json.dumps(exc.details)}" if exc.details else ""
        _fail(f"{exc}{details}", EXIT_USAGE if exc.status < 500 else EXIT_FAILED)


def _config(config_path: Path | None = None, api_url: str | None = None, api_key: str | None = None) -> CliConfig:
    return load_config(config_path, api_url=api_url, api_key=api_key)


def _client(config: CliConfig) -> CrucibleClient:
    if not config.api_key:
        _fail("no API key: set CRUCIBLE_API_KEY, or run `crucible init` and follow the instructions")
    return CrucibleClient(config.api_url, config.api_key)


async def _project_id(client: CrucibleClient, config: CliConfig) -> str:
    projects = await client.get("/api/v1/projects")
    if config.project:
        for project in projects:
            if config.project in (project["slug"], project["id"]):
                return str(project["id"])
        _fail(f"project {config.project!r} not found, or this API key does not have access to it")
    if len(projects) != 1:
        _fail("crucible.yaml does not name a `project`, and this key is not scoped to exactly one")
    return str(projects[0]["id"])


async def _find_named(client: CrucibleClient, path: str, name: str) -> dict[str, Any] | None:
    for item in await client.get(path):
        if item["name"] == name:
            return dict(item)
    return None


# ─── init ────────────────────────────────────────────────────────────────────

STARTER_CONFIG = """\
# Crucible — evaluation config. Committed next to the prompt it describes, so a pull request that
# changes behaviour also changes the thing that gets evaluated.
project: {slug}

targets:
  - name: {slug}-agent
    kind: prompt
    config:
      architecture: single_shot      # single_shot | react | planner_executor
      model: claude-sonnet-5
      system: You are a precise assistant. Answer with the exact value requested and nothing else.
      prompt_template: "{{{{question}}}}"
      max_tokens: 512

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

STARTER_CASES = [
    {"external_id": "capital-fr", "input": {"question": "Capital of France?"}, "expected": {"answer": "Paris"}},
    {"external_id": "capital-jp", "input": {"question": "Capital of Japan?"}, "expected": {"answer": "Tokyo"}},
    {"external_id": "sum-basic", "input": {"question": "What is 17 + 25?"}, "expected": {"answer": "42"}},
]


@app.command()
def init(
    slug: Annotated[str, typer.Option(help="Project slug to write into the config")] = "my-project",
    force: Annotated[bool, typer.Option(help="Overwrite an existing crucible.yaml")] = False,
) -> None:
    """Write a starter `crucible.yaml` and a golden dataset to run against."""
    config_path = Path("crucible.yaml")
    if config_path.exists() and not force:
        _fail(f"{config_path} already exists; pass --force to overwrite")

    config_path.write_text(STARTER_CONFIG.format(slug=slug))
    cases_path = Path("evals/golden.jsonl")
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    if not cases_path.exists() or force:
        cases_path.write_text("\n".join(json.dumps(c) for c in STARTER_CASES) + "\n")

    console.print(f"[green]created[/green] {config_path} and {cases_path}")
    console.print("\nNext:")
    console.print("  1. export CRUCIBLE_API_KEY=crk_...    # create one in the app, or with `crucible seed`")
    console.print("  2. crucible push")
    console.print("  3. crucible run --suite default")


# ─── push ────────────────────────────────────────────────────────────────────


@app.command()
def push(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Path to crucible.yaml")] = None,
    api_url: Annotated[str | None, typer.Option("--api-url")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key")] = None,
) -> None:
    """Upload targets, datasets and suites from `crucible.yaml`.

    Idempotent: pushing an unchanged config reuses the existing version rather than minting a new
    one, so running this on every commit does not fill the registry with identical rows.
    """
    config = _config(config_path, api_url, api_key)
    if config.path is None:
        _fail("no crucible.yaml found; run `crucible init` first")
    _run(_push(config))


async def _push(config: CliConfig) -> None:
    async with _client(config) as client:
        project_id = await _project_id(client, config)
        base = f"/api/v1/projects/{project_id}"

        for spec in config.targets:
            target = await _find_named(client, f"{base}/targets", spec.name)
            if target is None:
                target = await client.post(
                    f"{base}/targets",
                    json={"name": spec.name, "kind": spec.kind, "description": spec.description},
                )
            version = await client.post(f"{base}/targets/{target['id']}/versions", json={"config": spec.config})
            mark = "[dim]unchanged[/dim]" if version["reused"] else "[green]new[/green]"
            console.print(f"target  {spec.name:<24} {version['semver']:<8} {mark}")

        for dataset_spec in config.datasets:
            cases = load_cases(config.root, dataset_spec.path)
            dataset = await _find_named(client, f"{base}/datasets", dataset_spec.name)
            if dataset is None:
                dataset = await client.post(
                    f"{base}/datasets", json={"name": dataset_spec.name, "description": dataset_spec.description}
                )
            version = await client.post(f"{base}/datasets/{dataset['id']}/versions", json={"cases": cases})
            mark = "[dim]unchanged[/dim]" if version["reused"] else "[green]new[/green]"
            console.print(f"dataset {dataset_spec.name:<24} v{version['version']:<7} {len(cases)} cases {mark}")

        for suite_spec in config.suites:
            existing = await _find_named(client, f"{base}/suites", suite_spec.name)
            if existing is not None:
                console.print(f"suite   {suite_spec.name:<24} [dim]exists[/dim]")
                continue
            dataset = await _find_named(client, f"{base}/datasets", suite_spec.dataset)
            if dataset is None:
                _fail(f"suite {suite_spec.name!r} names dataset {suite_spec.dataset!r}, missing from this config")
                return
            await client.post(
                f"{base}/suites",
                json={
                    "name": suite_spec.name,
                    "dataset_id": dataset["id"],
                    "assertions": suite_spec.assertions,
                    "judge_model": suite_spec.judge_model,
                    "pass_threshold": suite_spec.pass_threshold,
                    "description": suite_spec.description,
                },
            )
            console.print(f"suite   {suite_spec.name:<24} [green]new[/green]")


# ─── run ─────────────────────────────────────────────────────────────────────


@app.command()
def run(
    suite: Annotated[str, typer.Option("--suite", "-s", help="Suite name from crucible.yaml")],
    target: Annotated[str | None, typer.Option("--target", "-t", help="Target name; defaults to the only one")] = None,
    baseline: Annotated[str | None, typer.Option(help="Baseline run id; defaults to the suite's last run")] = None,
    fail_on_regression: Annotated[bool, typer.Option(help="Exit 1 if a case that passed now fails")] = False,
    threshold: Annotated[float | None, typer.Option(help="Exit 1 if the pass rate falls below this")] = None,
    wait: Annotated[bool, typer.Option(help="Wait for the run to finish")] = True,
    timeout: Annotated[int, typer.Option(help="Seconds to wait before giving up on the run")] = 1800,
    json_output: Annotated[bool, typer.Option("--json", help="Print the run as JSON instead of a table")] = False,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    api_url: Annotated[str | None, typer.Option("--api-url")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key")] = None,
) -> None:
    """Start an eval run and, by default, wait for the verdict."""
    config = _config(config_path, api_url, api_key)
    _run(
        _start_run(
            config,
            suite=suite,
            target=target,
            baseline=baseline,
            fail_on_regression=fail_on_regression,
            threshold=threshold,
            wait=wait,
            timeout=timeout,
            json_output=json_output,
        )
    )


async def _start_run(config: CliConfig, **options: Any) -> None:
    async with _client(config) as client:
        project_id = await _project_id(client, config)
        base = f"/api/v1/projects/{project_id}"

        suite = await _find_named(client, f"{base}/suites", options["suite"])
        if suite is None:
            _fail(f"suite {options['suite']!r} not found; did you run `crucible push`?")
            return

        targets = await client.get(f"{base}/targets")
        if options["target"]:
            targets = [t for t in targets if t["name"] == options["target"]]
        if len(targets) != 1:
            _fail("pass --target: this project has more than one, or none")
        versions = await client.get(f"{base}/targets/{targets[0]['id']}/versions")
        if not versions:
            _fail(f"target {targets[0]['name']!r} has no versions; run `crucible push`")

        payload = {
            "target_version_id": versions[0]["id"],
            "suite_id": suite["id"],
            "baseline_run_id": options["baseline"],
            "trigger": "ci" if os.getenv("CI") else "cli",
            "git_sha": os.getenv("GITHUB_SHA"),
            "git_branch": os.getenv("GITHUB_REF_NAME"),
            # Two CI attempts at the same commit are the same run, not two bills.
            "idempotency_key": _idempotency_key(suite["id"], versions[0]["id"]),
        }
        created = await client.post(f"{base}/runs", json={k: v for k, v in payload.items() if v is not None})
        run_id = created["id"]
        console.print(
            f"run [bold]{run_id}[/bold] queued with {created['total_cases']} cases"
            + (" [dim](existing run, same idempotency key)[/dim]" if created["reused"] else "")
        )

        if not options["wait"]:
            return

        run = await _wait_for_run(client, base, run_id, options["timeout"])
        if options["json_output"]:
            console.print_json(data=run)
        else:
            _print_run(run)

        exit_code = EXIT_OK
        if run["status"] in ("failed", "cancelled"):
            exit_code = EXIT_FAILED

        if options["threshold"] is not None and (run["pass_rate"] or 0) < options["threshold"]:
            err.print(f"[red]pass rate {run['pass_rate']:.1%} is below the required {options['threshold']:.1%}[/red]")
            exit_code = EXIT_FAILED

        if options["fail_on_regression"] and run.get("baseline_run_id"):
            comparison = await client.get(f"{base}/runs/{run_id}/compare")
            _print_comparison(comparison)
            if comparison["has_regressions"]:
                exit_code = EXIT_FAILED

        raise typer.Exit(exit_code)


def _idempotency_key(suite_id: str, target_version_id: str) -> str | None:
    """Stable for a given (commit, suite, target version), and absent outside CI.

    Locally, re-running the same command is usually a deliberate re-run and should produce a new
    run; in CI, a retried job is the same job and should not pay twice.
    """
    sha = os.getenv("GITHUB_SHA") or os.getenv("CI_COMMIT_SHA")
    attempt = os.getenv("GITHUB_RUN_ID", "")
    return f"{sha[:12]}-{suite_id[:8]}-{target_version_id[:8]}-{attempt}" if sha else None


async def _wait_for_run(client: CrucibleClient, base: str, run_id: str, deadline_seconds: int) -> dict[str, Any]:
    """Poll rather than stream. A run can outlive any single connection, and CI logs are happier
    with a line every few seconds than with a held-open socket."""
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    delay, last_completed = 1.0, -1
    while True:
        run = await client.get(f"{base}/runs/{run_id}")
        if run["completed_cases"] != last_completed:
            last_completed = run["completed_cases"]
            console.print(f"  {run['completed_cases']}/{run['total_cases']} cases", highlight=False)
        if run["status"] in ("succeeded", "failed", "cancelled"):
            return dict(run)
        if asyncio.get_running_loop().time() > deadline:
            _fail(f"run {run_id} is still running after {deadline_seconds}s; it continues server-side", EXIT_FAILED)
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 10.0)


def _print_run(run: dict[str, Any]) -> None:
    colour = {"succeeded": "green", "failed": "red", "cancelled": "yellow"}.get(run["status"], "white")
    # No title: rich centres and wraps a title to the table's width, which turns a uuid into three
    # ragged lines on an 80-column terminal. As a row it stays copy-pasteable.
    table = Table(show_header=False, box=None)
    table.add_row("run", str(run["id"]))
    table.add_row("status", f"[{colour}]{run['status']}[/{colour}]")
    table.add_row("cases", f"{run['passed_cases']}/{run['total_cases']} passed, {run['errored_cases']} errored")
    if run["pass_rate"] is not None:
        table.add_row("pass rate", f"{run['pass_rate']:.1%}")
    if run["mean_score"] is not None:
        table.add_row("mean score", f"{run['mean_score']:.3f}")
    table.add_row("cost", format_usd(run["total_cost_micros"]))
    table.add_row("tokens", f"{run['total_input_tokens']:,} in / {run['total_output_tokens']:,} out")
    if run["p50_latency_ms"] is not None:
        table.add_row("latency", f"p50 {run['p50_latency_ms']}ms · p95 {run['p95_latency_ms']}ms")
    console.print(table)


def _print_comparison(comparison: dict[str, Any]) -> None:
    summary = comparison["summary"]
    console.print(
        f"\nvs baseline: [red]{summary['broken']} broken[/red] · [green]{summary['fixed']} fixed[/green] · "
        f"{summary['unchanged']} unchanged · {summary['added']} added · {summary['removed']} removed"
    )
    broken = [d for d in comparison["deltas"] if d["verdict"] == "broken"]
    if not broken:
        return
    table = Table(show_header=True, header_style="bold red")
    table.add_column("case")
    table.add_column("was")
    table.add_column("now")
    table.add_column("why")
    for delta in broken[:20]:
        table.add_row(delta["external_id"], delta["baseline_status"], delta["current_status"], delta["detail"] or "")
    console.print(table)
    if len(broken) > 20:
        console.print(f"[dim]… and {len(broken) - 20} more[/dim]")


# ─── compare ─────────────────────────────────────────────────────────────────


@app.command()
def compare(
    current: Annotated[str, typer.Argument(help="Run id to inspect")],
    baseline: Annotated[str | None, typer.Argument(help="Baseline run id; defaults to the run's own")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    api_url: Annotated[str | None, typer.Option("--api-url")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key")] = None,
) -> None:
    """Show what changed between two runs. Exits 1 if anything regressed."""
    config = _config(config_path, api_url, api_key)
    _run(_compare(config, current, baseline, json_output))


async def _compare(config: CliConfig, current: str, baseline: str | None, json_output: bool) -> None:
    async with _client(config) as client:
        project_id = await _project_id(client, config)
        params = {"baseline": baseline} if baseline else None
        comparison = await client.get(f"/api/v1/projects/{project_id}/runs/{current}/compare", params=params)
        if json_output:
            console.print_json(data=comparison)
        else:
            _print_comparison(comparison)
        raise typer.Exit(EXIT_FAILED if comparison["has_regressions"] else EXIT_OK)


# ─── worker / serve ──────────────────────────────────────────────────────────


@app.command()
def worker(
    run_id: Annotated[str | None, typer.Option(help="Drain one run and exit, instead of serving forever")] = None,
    concurrency: Annotated[int | None, typer.Option(help="Override the run's configured concurrency")] = None,
) -> None:
    """Execute queued cases. Run as many of these as you like — the queue is safe to share."""
    from uuid import UUID

    from crucible.logging import configure_logging
    from crucible.runner.worker import build_worker

    configure_logging()
    instance = build_worker(batch_size=concurrency) if concurrency else build_worker()

    async def _serve() -> None:
        try:
            if run_id:
                report = await instance.drain(UUID(run_id))
                console.print(f"drained {report.completed} cases; run is {report.status}")
            else:
                await instance.serve()
        finally:
            await instance.aclose()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        # In-flight cases keep their leases and are re-claimed by another worker; nothing is lost.
        console.print("\n[dim]worker stopped[/dim]")


@app.command()
def serve(
    host: Annotated[str | None, typer.Option()] = None,
    port: Annotated[int | None, typer.Option()] = None,
    reload: Annotated[bool, typer.Option(help="Reload on source changes (development only)")] = False,
) -> None:
    """Run the API."""
    import uvicorn

    from crucible.config import settings

    uvicorn.run(
        "crucible.api.app:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
        log_config=None,
    )


# ─── seed ────────────────────────────────────────────────────────────────────


@app.command()
def seed(
    email: Annotated[str, typer.Option(help="Owner account to create or reuse")] = "dev@crucible.dev",
    password: Annotated[str, typer.Option(help="Password for a newly created account")] = "crucible-dev-1234",
    slug: Annotated[str, typer.Option(help="Project slug to create or reuse")] = "my-project",
) -> None:
    """Create a local owner, project and API key, and print the key.

    This talks to the database directly rather than to the API, because it exists to solve the
    bootstrap problem: on a fresh database there is no account yet to authenticate the call that
    would create one. It is a development convenience — in a deployed environment accounts are
    created through `POST /auth/signup` like everyone else's.
    """
    _run(_seed(email, password, slug))


async def _seed(email: str, password: str, slug: str) -> None:
    from pydantic import EmailStr, TypeAdapter, ValidationError
    from sqlalchemy import text as sql

    from crucible.api.security import generate_api_key, hash_password
    from crucible.db.session import dispose_engine, session_scope

    # This command writes the user with raw SQL, which means it skips the `EmailStr` validation that
    # `POST /auth/signup` would have applied — so it will happily create an account that the login
    # endpoint then refuses. It did exactly that: the old default of `dev@crucible.local` is
    # rejected because `.local` is a reserved special-use name (RFC 6762), and the result was a
    # quickstart that ended in a 422 nobody could explain. Validating here means the bootstrap path
    # and the real signup path agree on what an address is.
    try:
        TypeAdapter(EmailStr).validate_python(email)
    except ValidationError as exc:
        reason = exc.errors()[0].get("msg", "invalid address")
        console.print(f"[red]refusing[/red] to seed {email}: {reason}")
        console.print("[dim]the API would reject this address at login, so the account would be unusable[/dim]")
        raise typer.Exit(2) from None

    try:
        async with session_scope() as db:
            user = (await db.execute(sql("SELECT id FROM users WHERE email = :e"), {"e": email})).first()
            if user is None:
                user = (
                    await db.execute(
                        sql(
                            "INSERT INTO users (id, email, name, password_hash) "
                            "VALUES (gen_random_uuid(), :e, :n, :h) RETURNING id"
                        ),
                        {"e": email, "n": email.split("@")[0], "h": hash_password(password)},
                    )
                ).one()
                console.print(f"[green]created[/green] user {email} (password: {password})")
            else:
                console.print(f"[dim]reusing[/dim] user {email}")

            project = (await db.execute(sql("SELECT id FROM projects WHERE slug = :s"), {"s": slug})).first()
            if project is None:
                project = (
                    await db.execute(
                        sql("INSERT INTO projects (id, name, slug) VALUES (gen_random_uuid(), :n, :s) RETURNING id"),
                        {"n": slug.replace("-", " ").title(), "s": slug},
                    )
                ).one()
                console.print(f"[green]created[/green] project {slug}")
            else:
                console.print(f"[dim]reusing[/dim] project {slug}")

            await db.execute(
                sql(
                    "INSERT INTO project_members (id, project_id, user_id, role) "
                    "VALUES (gen_random_uuid(), :p, :u, 'owner') ON CONFLICT DO NOTHING"
                ),
                {"p": project.id, "u": user.id},
            )

            # A key's plaintext is unrecoverable by design, so seeding always mints a fresh one
            # rather than trying to tell you what the existing key was.
            plaintext, prefix, key_hash = generate_api_key()
            await db.execute(
                sql(
                    "INSERT INTO api_keys (id, project_id, name, prefix, key_hash, created_by) "
                    "VALUES (gen_random_uuid(), :p, 'local-dev', :pre, :h, :u)"
                ),
                {"p": project.id, "pre": prefix, "h": key_hash, "u": user.id},
            )
    finally:
        await dispose_engine()

    console.print("\n[bold]export CRUCIBLE_API_KEY=[/bold]" + plaintext)


# ─── status ──────────────────────────────────────────────────────────────────


@app.command()
def status(
    limit: Annotated[int, typer.Option(help="How many recent runs to show")] = 10,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    api_url: Annotated[str | None, typer.Option("--api-url")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key")] = None,
) -> None:
    """Recent runs for this project."""
    config = _config(config_path, api_url, api_key)
    _run(_status(config, limit))


async def _status(config: CliConfig, limit: int) -> None:
    async with _client(config) as client:
        project_id = await _project_id(client, config)
        page = await client.get(f"/api/v1/projects/{project_id}/runs", params={"limit": limit})
        runs = page["items"]
        if not runs:
            console.print("[dim]no runs yet[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        for column in ("run", "status", "pass rate", "cases", "cost", "label", "created"):
            table.add_column(column)
        for run in runs:
            colour = {"succeeded": "green", "failed": "red", "cancelled": "yellow"}.get(run["status"], "cyan")
            table.add_row(
                str(run["id"])[:8],
                f"[{colour}]{run['status']}[/{colour}]",
                f"{run['pass_rate']:.1%}" if run["pass_rate"] is not None else "—",
                f"{run['passed_cases']}/{run['total_cases']}",
                format_usd(run["total_cost_micros"]),
                run["label"] or "—",
                str(run["created_at"])[:19],
            )
        console.print(table)


@app.command()
def config(
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Show the resolved configuration — which file, which API, whether a key is present."""
    found = config_path or find_config()
    resolved = _config(config_path)
    console.print(f"config file : {found or '[dim]none found[/dim]'}")
    console.print(f"api url     : {resolved.api_url}")
    key = f"set ({resolved.api_key[:12]}…)" if resolved.api_key else "[red]not set[/red]"
    console.print(f"api key     : {key}")
    console.print(f"project     : {resolved.project or '[dim]not set[/dim]'}")
    console.print(f"targets     : {', '.join(t.name for t in resolved.targets) or '—'}")
    console.print(f"datasets    : {', '.join(d.name for d in resolved.datasets) or '—'}")
    console.print(f"suites      : {', '.join(s.name for s in resolved.suites) or '—'}")


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        err.print(f"[bold red]error[/bold red] {exc}")
        sys.exit(EXIT_USAGE)


if __name__ == "__main__":
    main()
