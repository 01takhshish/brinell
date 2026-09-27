"""Create-or-reuse for the things a run points at: targets, dataset versions, suites.

Pushing is expected to be idempotent, because it is called from CI. `crucible push` runs on every
commit, and most commits do not change the prompt — so pushing an unchanged config must return the
existing version rather than minting `1.0.47` of something that has not moved. Identity is the
content hash, which is also why a run can say exactly what executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.domain.hashing import bump, content_hash, max_semver
from crucible.errors import BadRequest, Conflict, NotFound
from crucible.logging import get_logger

log = get_logger(__name__)


@dataclass
class Pushed:
    """`reused` is what the CLI prints, and what tells a user their edit did not actually land."""

    row: Any
    reused: bool


async def create_target(
    session: AsyncSession, *, project_id: UUID, name: str, kind: str, description: str | None
) -> Any:
    existing = (
        await session.execute(
            text("SELECT * FROM targets WHERE project_id = :p AND name = :n"),
            {"p": project_id, "n": name},
        )
    ).first()
    if existing is not None:
        raise Conflict(f"target {name!r} already exists in this project")
    return (
        await session.execute(
            text(
                """
                INSERT INTO targets (id, project_id, name, kind, description)
                VALUES (gen_random_uuid(), :p, :n, :k, :d)
                RETURNING *
                """
            ),
            {"p": project_id, "n": name, "k": kind, "d": description},
        )
    ).one()


async def get_target(session: AsyncSession, *, project_id: UUID, target_id: UUID) -> Any:
    row = (
        await session.execute(
            text("SELECT * FROM targets WHERE id = :id AND project_id = :p"),
            {"id": target_id, "p": project_id},
        )
    ).first()
    if row is None:
        raise NotFound("Target")
    return row


async def push_target_version(
    session: AsyncSession,
    *,
    project_id: UUID,
    target_id: UUID,
    config: dict[str, Any],
    semver: str | None = None,
    notes: str | None = None,
    created_by: UUID | None = None,
) -> Pushed:
    await get_target(session, project_id=project_id, target_id=target_id)
    digest = content_hash(config)

    existing = (
        await session.execute(
            text("SELECT * FROM target_versions WHERE target_id = :t AND content_hash = :h"),
            {"t": target_id, "h": digest},
        )
    ).first()
    if existing is not None:
        log.info("target_version_reused", target_id=str(target_id), semver=existing.semver)
        return Pushed(existing, True)

    versions = [
        r.semver
        for r in (
            await session.execute(text("SELECT semver FROM target_versions WHERE target_id = :t"), {"t": target_id})
        ).all()
    ]
    if semver is None:
        latest = max_semver(versions)
        semver = bump(latest, "patch") if latest else "1.0.0"
    elif semver in versions:
        raise Conflict(f"version {semver} already exists for this target")

    row = (
        await session.execute(
            text(
                """
                INSERT INTO target_versions
                    (id, project_id, target_id, semver, content_hash, config, notes, created_by)
                VALUES (gen_random_uuid(), :p, :t, :v, :h, CAST(:c AS jsonb), :n, :u)
                RETURNING *
                """
            ),
            {
                "p": project_id,
                "t": target_id,
                "v": semver,
                "h": digest,
                "c": json.dumps(config),
                "n": notes,
                "u": created_by,
            },
        )
    ).one()
    log.info("target_version_created", target_id=str(target_id), semver=semver, content_hash=digest[:12])
    return Pushed(row, False)


async def create_dataset(session: AsyncSession, *, project_id: UUID, name: str, description: str | None) -> Any:
    existing = (
        await session.execute(
            text("SELECT id FROM datasets WHERE project_id = :p AND name = :n"), {"p": project_id, "n": name}
        )
    ).first()
    if existing is not None:
        raise Conflict(f"dataset {name!r} already exists in this project")
    return (
        await session.execute(
            text(
                """
                INSERT INTO datasets (id, project_id, name, description)
                VALUES (gen_random_uuid(), :p, :n, :d)
                RETURNING *
                """
            ),
            {"p": project_id, "n": name, "d": description},
        )
    ).one()


async def get_dataset(session: AsyncSession, *, project_id: UUID, dataset_id: UUID) -> Any:
    row = (
        await session.execute(
            text("SELECT * FROM datasets WHERE id = :id AND project_id = :p"), {"id": dataset_id, "p": project_id}
        )
    ).first()
    if row is None:
        raise NotFound("Dataset")
    return row


async def push_dataset_version(
    session: AsyncSession,
    *,
    project_id: UUID,
    dataset_id: UUID,
    cases: list[dict[str, Any]],
) -> Pushed:
    """Cases are versioned as a set, not individually.

    A dataset version is the unit a run pins to, so "which cases existed" has to be answerable
    without reconstructing a history of edits. Re-pushing an unchanged file is a no-op.
    """
    await get_dataset(session, project_id=project_id, dataset_id=dataset_id)

    external_ids = [c["external_id"] for c in cases]
    duplicates = {e for e in external_ids if external_ids.count(e) > 1}
    if duplicates:
        # The whole comparison story is keyed on external_id, so a duplicate would silently make
        # one of the two cases invisible in every regression report.
        raise BadRequest(f"duplicate external_id in dataset: {sorted(duplicates)[:5]}")

    digest = content_hash(cases)
    existing = (
        await session.execute(
            text("SELECT * FROM dataset_versions WHERE dataset_id = :d AND content_hash = :h"),
            {"d": dataset_id, "h": digest},
        )
    ).first()
    if existing is not None:
        log.info("dataset_version_reused", dataset_id=str(dataset_id), version=existing.version)
        return Pushed(existing, True)

    next_version = int(
        (
            await session.execute(
                text("SELECT coalesce(max(version), 0) + 1 FROM dataset_versions WHERE dataset_id = :d"),
                {"d": dataset_id},
            )
        ).scalar_one()
    )

    row = (
        await session.execute(
            text(
                """
                INSERT INTO dataset_versions (id, project_id, dataset_id, version, content_hash, case_count)
                VALUES (gen_random_uuid(), :p, :d, :v, :h, :n)
                RETURNING *
                """
            ),
            {"p": project_id, "d": dataset_id, "v": next_version, "h": digest, "n": len(cases)},
        )
    ).one()

    for case in cases:
        await session.execute(
            text(
                """
                INSERT INTO cases (id, project_id, dataset_version_id, external_id, input, expected, tags, weight)
                VALUES (gen_random_uuid(), :p, :dv, :eid, CAST(:inp AS jsonb), CAST(:exp AS jsonb), :tags, :w)
                """
            ),
            {
                "p": project_id,
                "dv": row.id,
                "eid": case["external_id"],
                "inp": json.dumps(_case_input(case)),
                "exp": json.dumps(case["expected"]) if case.get("expected") is not None else None,
                "tags": case.get("tags") or [],
                "w": case.get("weight", 1.0),
            },
        )

    log.info("dataset_version_created", dataset_id=str(dataset_id), version=next_version, cases=len(cases))
    return Pushed(row, False)


def _case_input(case: dict[str, Any]) -> dict[str, Any]:
    """Tool fixtures ride along inside the input so a case is one self-contained row.

    The executor reads them back out as `case_input["tool_fixtures"]`; keeping them in `input` also
    means the dataset version's content hash covers the fixtures, which is what makes a rerun of an
    old version reproduce the old tool results rather than today's.
    """
    payload = dict(case["input"])
    if case.get("tool_fixtures"):
        payload["tool_fixtures"] = case["tool_fixtures"]
    return payload


async def create_suite(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    dataset_id: UUID,
    assertions: list[dict[str, Any]],
    judge_model: str | None,
    pass_threshold: float,
    description: str | None,
) -> Any:
    await get_dataset(session, project_id=project_id, dataset_id=dataset_id)
    existing = (
        await session.execute(
            text("SELECT id FROM suites WHERE project_id = :p AND name = :n"), {"p": project_id, "n": name}
        )
    ).first()
    if existing is not None:
        raise Conflict(f"suite {name!r} already exists in this project")
    return (
        await session.execute(
            text(
                """
                INSERT INTO suites
                    (id, project_id, dataset_id, name, description, assertions, judge_model, pass_threshold)
                VALUES (gen_random_uuid(), :p, :d, :n, :desc, CAST(:a AS jsonb), :j, :t)
                RETURNING *
                """
            ),
            {
                "p": project_id,
                "d": dataset_id,
                "n": name,
                "desc": description,
                "a": json.dumps(assertions),
                "j": judge_model,
                "t": pass_threshold,
            },
        )
    ).one()
