"""Minimal object graph builders.

Every integration test needs a project, a target version, a dataset version with cases and a
suite before it can create a run. These helpers keep that setup out of the tests so each test
reads as the behaviour it is checking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.domain.hashing import content_hash


@dataclass
class Fixture:
    project_id: UUID
    target_id: UUID
    target_version_id: UUID
    dataset_id: UUID
    dataset_version_id: UUID
    suite_id: UUID


DEFAULT_TARGET_CONFIG: dict[str, Any] = {
    "architecture": "single_shot",
    "model": "mock-small",
    "system": "Answer with the exact value requested.",
    "prompt_template": "{{question}}",
    "max_tokens": 256,
}

DEFAULT_ASSERTIONS: list[dict[str, Any]] = [{"type": "equals", "value_from": "expected.answer"}]


async def create_fixture(
    session: AsyncSession,
    *,
    slug: str = "acme",
    cases: list[dict[str, Any]] | None = None,
    target_config: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
) -> Fixture:
    config = {**DEFAULT_TARGET_CONFIG, **(target_config or {})}
    case_specs = cases if cases is not None else default_cases()

    project_id = (
        await session.execute(
            sa.text("INSERT INTO projects (id, name, slug) VALUES (gen_random_uuid(), :n, :s) RETURNING id"),
            {"n": slug.title(), "s": slug},
        )
    ).scalar_one()

    target_id = (
        await session.execute(
            sa.text(
                """
                INSERT INTO targets (id, project_id, name, kind)
                VALUES (gen_random_uuid(), :p, 'support-agent', 'prompt') RETURNING id
                """
            ),
            {"p": project_id},
        )
    ).scalar_one()

    target_version_id = (
        await session.execute(
            sa.text(
                """
                INSERT INTO target_versions (id, project_id, target_id, semver, content_hash, config, created_by)
                VALUES (gen_random_uuid(), :p, :t, '1.0.0', :h, CAST(:c AS jsonb), NULL) RETURNING id
                """
            ),
            {"p": project_id, "t": target_id, "h": content_hash(config), "c": json.dumps(config)},
        )
    ).scalar_one()

    dataset_id = (
        await session.execute(
            sa.text(
                """
                INSERT INTO datasets (id, project_id, name, description)
                VALUES (gen_random_uuid(), :p, 'golden', 'integration fixture') RETURNING id
                """
            ),
            {"p": project_id},
        )
    ).scalar_one()

    dataset_version_id = (
        await session.execute(
            sa.text(
                """
                INSERT INTO dataset_versions (id, project_id, dataset_id, version, content_hash, case_count)
                VALUES (gen_random_uuid(), :p, :d, 1, :h, :n) RETURNING id
                """
            ),
            {"p": project_id, "d": dataset_id, "h": content_hash(case_specs), "n": len(case_specs)},
        )
    ).scalar_one()

    for spec in case_specs:
        await session.execute(
            sa.text(
                """
                INSERT INTO cases (id, project_id, dataset_version_id, external_id, input, expected, tags, weight)
                VALUES (gen_random_uuid(), :p, :dv, :eid, CAST(:inp AS jsonb), CAST(:exp AS jsonb), :tags, 1.0)
                """
            ),
            {
                "p": project_id,
                "dv": dataset_version_id,
                "eid": spec["external_id"],
                "inp": json.dumps(spec["input"]),
                "exp": json.dumps(spec.get("expected")) if spec.get("expected") is not None else None,
                "tags": spec.get("tags", []),
            },
        )

    suite_id = (
        await session.execute(
            sa.text(
                """
                INSERT INTO suites (id, project_id, dataset_id, name, assertions, judge_model, pass_threshold)
                VALUES (gen_random_uuid(), :p, :d, 'default', CAST(:a AS jsonb), NULL, 1.0) RETURNING id
                """
            ),
            {"p": project_id, "d": dataset_id, "a": json.dumps(assertions or DEFAULT_ASSERTIONS)},
        )
    ).scalar_one()

    await session.commit()
    return Fixture(project_id, target_id, target_version_id, dataset_id, dataset_version_id, suite_id)


def default_cases(n: int = 6) -> list[dict[str, Any]]:
    """`[[expect:VALUE]]` is the mock provider's directive: it answers correctly at its configured
    quality, which makes pass/fail scriptable without a live model."""
    return [
        {
            "external_id": f"case-{i:02d}",
            "input": {"question": f"What is the code for order {i}? [[expect:CODE-{i}]]"},
            "expected": {"answer": f"CODE-{i}"},
        }
        for i in range(n)
    ]
