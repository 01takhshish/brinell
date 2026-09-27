"""One run, all the way through the HTTP surface.

The other API tests stop at "queued", because that is where the request ends. This one drains the
queue with a real worker and then asks the API the questions a user actually asks afterwards: how
did it go, what did case X do, what regressed, and what do the failures have in common.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.providers.mock import MockBehaviour, MockProvider
from crucible.providers.registry import ProviderRegistry
from crucible.runner.worker import Worker
from tests.integration.test_api import auth, bootstrap, signup

pytestmark = pytest.mark.asyncio


def make_worker(**behaviour: Any) -> Worker:
    registry = ProviderRegistry()
    registry.register(MockProvider(MockBehaviour(**behaviour)))
    return Worker(registry=registry, redis=None, lease_seconds=30, batch_size=4)


async def start_run(client: Any, ctx: dict[str, Any], **overrides: Any) -> str:
    response = await client.post(
        f"{ctx['base']}/runs",
        json={"target_version_id": ctx["target_version_id"], "suite_id": ctx["suite_id"], **overrides},
        headers=ctx["headers"],
    )
    assert response.status_code == 202, response.text
    return str(response.json()["id"])


async def test_a_run_reports_itself_truthfully_after_execution(
    client: Any, db: AsyncSession, patched_session_scope: None
) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    run_id = await start_run(client, ctx, label="first")

    report = await make_worker(latency_ms=3).drain(run_id)
    assert report.completed == 2

    run = (await client.get(f"{ctx['base']}/runs/{run_id}", headers=ctx["headers"])).json()
    assert run["status"] == "succeeded"
    assert (run["completed_cases"], run["passed_cases"], run["errored_cases"]) == (2, 2, 0)
    assert run["pass_rate"] == 1.0
    # The mock provider has synthetic prices, so cost is a real number here rather than a guess —
    # and it has to be the sum of the cases, not a separately-maintained counter.
    case_cost = (
        await db.execute(sa.text("SELECT sum(cost_micros) FROM run_cases WHERE run_id = :r"), {"r": run_id})
    ).scalar_one()
    assert run["total_cost_micros"] == case_cost > 0
    assert run["p50_latency_ms"] is not None

    cases = (await client.get(f"{ctx['base']}/runs/{run_id}/cases", headers=ctx["headers"])).json()
    assert [c["status"] for c in cases["items"]] == ["passed", "passed"]
    assert cases["total"] == 2

    detail = (
        await client.get(f"{ctx['base']}/runs/{run_id}/cases/{cases['items'][0]['id']}", headers=ctx["headers"])
    ).json()
    assert detail["input"]["question"].startswith("code for 1?")
    assert detail["expected"] == {"answer": "CODE-1"}
    assert detail["output"]["text"] == "CODE-1"
    assert [a["passed"] for a in detail["assertion_results"]] == [True]
    # The trace is the product: at least the model call has to be there to look at.
    assert [s["kind"] for s in detail["spans"]] == ["llm"]


async def test_compare_names_the_cases_that_broke(client: Any, db: AsyncSession, patched_session_scope: None) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)

    baseline_id = await start_run(client, ctx, label="baseline")
    await make_worker().drain(baseline_id)

    # A second run of the same suite against a model that gets everything wrong. `quality=0.0` is
    # the mock's knob for "answers, but incorrectly" — a regression, not an outage.
    current_id = await start_run(client, ctx, label="regressed", baseline_run_id=baseline_id)
    await make_worker(quality=0.0).drain(current_id)

    comparison = (await client.get(f"{ctx['base']}/runs/{current_id}/compare", headers=ctx["headers"])).json()
    assert comparison["has_regressions"] is True
    assert comparison["summary"]["broken"] == 2
    assert comparison["pass_rate_delta"] == -1.0
    assert {d["external_id"] for d in comparison["deltas"]} == {"case-01", "case-02"}
    assert all(d["verdict"] == "broken" for d in comparison["deltas"])

    # The baseline defaults to the run's own recorded baseline, so CI does not have to pass it.
    explicit = await client.get(
        f"{ctx['base']}/runs/{current_id}/compare", params={"baseline": baseline_id}, headers=ctx["headers"]
    )
    assert explicit.json()["summary"] == comparison["summary"]


async def test_compare_without_a_baseline_is_a_clear_400(
    client: Any, db: AsyncSession, patched_session_scope: None
) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    run_id = await start_run(client, ctx)
    response = await client.get(f"{ctx['base']}/runs/{run_id}/compare", headers=ctx["headers"])
    assert response.status_code == 400
    assert "baseline" in response.json()["error"]["message"]


async def test_clusters_are_computed_on_demand_and_then_readable(
    client: Any, db: AsyncSession, patched_session_scope: None
) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    run_id = await start_run(client, ctx)
    await make_worker(quality=0.0).drain(run_id)

    # GET before any computation reports nothing rather than silently doing the expensive work.
    assert (await client.get(f"{ctx['base']}/runs/{run_id}/clusters", headers=ctx["headers"])).json()["clusters"] == []

    computed = await client.post(f"{ctx['base']}/runs/{run_id}/clusters", headers=ctx["headers"])
    assert computed.status_code == 200
    members = [m for c in computed.json()["clusters"] for m in c["members"]]
    assert {m["external_id"] for m in members} == {"case-01", "case-02"}

    stored = (await client.get(f"{ctx['base']}/runs/{run_id}/clusters", headers=ctx["headers"])).json()
    assert [c["label"] for c in stored["clusters"]] == [c["label"] for c in computed.json()["clusters"]]

    anchor = members[0]["run_case_id"]
    similar = await client.get(f"{ctx['base']}/run-cases/{anchor}/similar", headers=ctx["headers"])
    assert [s["external_id"] for s in similar.json()["similar"]] == ["case-02"]


async def test_cancelling_a_queued_run_skips_its_remaining_cases(
    client: Any, db: AsyncSession, patched_session_scope: None
) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    run_id = await start_run(client, ctx)

    cancelled = await client.post(f"{ctx['base']}/runs/{run_id}/cancel", headers=ctx["headers"])
    assert cancelled.json()["status"] == "cancelled"

    statuses = (
        (await db.execute(sa.text("SELECT DISTINCT status FROM run_cases WHERE run_id = :r"), {"r": run_id}))
        .scalars()
        .all()
    )
    assert statuses == ["skipped"]

    # Cancelling again is a no-op: CI cancels on an already-cancelled job all the time.
    assert (await client.post(f"{ctx['base']}/runs/{run_id}/cancel", headers=ctx["headers"])).status_code == 200


async def test_a_run_belonging_to_another_project_is_not_found(
    client: Any, db: AsyncSession, patched_session_scope: None
) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    run_id = await start_run(client, ctx)

    other = await client.post("/api/v1/projects", json={"name": "Other", "slug": "other"}, headers=auth(token))
    response = await client.get(f"/api/v1/projects/{other.json()['id']}/runs/{run_id}", headers=auth(token))
    assert response.status_code == 404
