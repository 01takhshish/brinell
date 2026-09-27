"""Regression comparison and failure clustering over two real runs.

The comparison is the feature CI actually gates on, so the case it has to get right is the one
where a *different dataset version* is involved: cases are matched on `external_id`, never on row
id, or every dataset edit would look like a total rewrite.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.analysis.cluster import cluster_run, similar_cases
from crucible.analysis.compare import compare_runs
from crucible.providers.mock import MockBehaviour, MockProvider
from crucible.providers.registry import ProviderRegistry
from crucible.runner.service import CreateRunRequest, create_run
from crucible.runner.worker import Worker
from tests.integration.factories import create_fixture, default_cases


def worker(quality: float) -> Worker:
    registry = ProviderRegistry()
    registry.register(MockProvider(MockBehaviour(quality=quality)))
    return Worker(registry=registry, redis=None, lease_seconds=30, batch_size=4)


async def run_suite(db: AsyncSession, fx, *, quality: float, label: str):
    created = await create_run(
        db,
        CreateRunRequest(
            project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id, label=label
        ),
    )
    await db.commit()
    await worker(quality).drain(created.run_id)
    return created.run_id


async def test_a_regression_is_reported_per_case(db: AsyncSession, patched_session_scope: None):
    fx = await create_fixture(db, slug="regress", cases=default_cases(6))
    good = await run_suite(db, fx, quality=1.0, label="baseline")
    bad = await run_suite(db, fx, quality=0.0, label="candidate")

    comparison = await compare_runs(db, project_id=fx.project_id, baseline_run_id=good, current_run_id=bad)

    assert comparison.has_regressions is True
    assert comparison.summary.broken == 6
    assert comparison.summary.fixed == 0
    assert comparison.pass_rate_delta == -1.0
    assert [d.verdict for d in comparison.deltas] == ["broken"] * 6
    assert all(d.detail for d in comparison.deltas), "a broken case must say which assertion broke"


async def test_a_green_rerun_shows_no_regressions(db: AsyncSession, patched_session_scope: None):
    fx = await create_fixture(db, slug="stable", cases=default_cases(4))
    first = await run_suite(db, fx, quality=1.0, label="baseline")
    second = await run_suite(db, fx, quality=1.0, label="candidate")

    comparison = await compare_runs(db, project_id=fx.project_id, baseline_run_id=first, current_run_id=second)
    assert comparison.has_regressions is False
    assert comparison.summary.unchanged == 4
    assert comparison.as_dict()["summary"]["broken"] == 0


async def test_cases_are_matched_by_external_id_across_dataset_versions(db: AsyncSession, patched_session_scope: None):
    """The baseline ran on 6 cases; the candidate runs on a version where one was dropped and two
    were added. Those must show up as removed/added rather than as silent disappearances."""
    fx = await create_fixture(db, slug="drift", cases=default_cases(6))
    baseline = await run_suite(db, fx, quality=1.0, label="baseline")

    v2 = (
        await db.execute(
            sa.text(
                """
                INSERT INTO dataset_versions (id, project_id, dataset_id, version, content_hash, case_count)
                VALUES (gen_random_uuid(), :p, :d, 2, 'hash-v2', 7) RETURNING id
                """
            ),
            {"p": fx.project_id, "d": fx.dataset_id},
        )
    ).scalar_one()
    # case-00 is dropped; case-06 and case-07 are new.
    for spec in [*default_cases(6)[1:], *default_cases(8)[6:]]:
        await db.execute(
            sa.text(
                """
                INSERT INTO cases (id, project_id, dataset_version_id, external_id, input, expected)
                VALUES (gen_random_uuid(), :p, :dv, :eid, CAST(:inp AS jsonb), CAST(:exp AS jsonb))
                """
            ),
            {
                "p": fx.project_id,
                "dv": v2,
                "eid": spec["external_id"],
                "inp": json.dumps(spec["input"]),
                "exp": json.dumps(spec["expected"]),
            },
        )
    await db.commit()

    created = await create_run(
        db,
        CreateRunRequest(
            project_id=fx.project_id,
            target_version_id=fx.target_version_id,
            suite_id=fx.suite_id,
            dataset_version_id=v2,
            label="candidate",
        ),
    )
    await db.commit()
    await worker(1.0).drain(created.run_id)

    comparison = await compare_runs(
        db, project_id=fx.project_id, baseline_run_id=baseline, current_run_id=created.run_id
    )
    verdicts = {d.external_id: d.verdict for d in comparison.deltas}
    assert verdicts["case-00"] == "removed"
    assert verdicts["case-06"] == "added"
    assert verdicts["case-07"] == "added"
    assert verdicts["case-03"] == "unchanged"
    assert comparison.has_regressions is False, "adding and removing cases is not a regression"


async def test_failures_cluster_and_similar_cases_come_back(db: AsyncSession, patched_session_scope: None):
    """Twelve failures with three distinct shapes should come back as groups a human can triage,
    and any one of them should be able to find its neighbours through pgvector."""
    shapes = [
        ("refund", "What is the refund_id for order {i}? [[expect:RF-{i}]]", "RF-{i}"),
        ("latency", "How long did request {i} take in ms? [[expect:{i}00ms]]", "{i}00ms"),
        ("policy", "Quote the escalation policy for tier {i}. [[expect:POLICY-{i}]]", "POLICY-{i}"),
    ]
    cases = [
        {
            "external_id": f"{name}-{i}",
            "input": {"question": question.format(i=i)},
            "expected": {"answer": answer.format(i=i)},
        }
        for name, question, answer in shapes
        for i in range(4)
    ]
    fx = await create_fixture(db, slug="triage", cases=cases)
    run_id = await run_suite(db, fx, quality=0.0, label="all-red")

    clusters = await cluster_run(db, project_id=fx.project_id, run_id=run_id)
    await db.commit()

    assert clusters, "twelve failures should not produce zero clusters"
    assert sum(c.size for c in clusters) <= 12
    assert all(c.label and c.top_terms for c in clusters), "a cluster without a label is not triage"

    persisted = (
        await db.execute(sa.text("SELECT count(*) FROM failure_clusters WHERE run_id = :r"), {"r": run_id})
    ).scalar_one()
    assert persisted == len(clusters)

    # Re-clustering replaces rather than appends.
    again = await cluster_run(db, project_id=fx.project_id, run_id=run_id)
    await db.commit()
    persisted_again = (
        await db.execute(sa.text("SELECT count(*) FROM failure_clusters WHERE run_id = :r"), {"r": run_id})
    ).scalar_one()
    assert persisted_again == len(again) == len(clusters)

    anchor = clusters[0].members[0].run_case_id
    neighbours = await similar_cases(db, project_id=fx.project_id, run_case_id=anchor, limit=5)
    assert neighbours, "pgvector lookup returned nothing"
    assert all(n["run_case_id"] != anchor for n in neighbours)
    assert neighbours == sorted(neighbours, key=lambda n: n["distance"]), "nearest must come first"
