"""Failure clustering.

A red run with sixty failures is usually not sixty problems. It is three problems, one of which
hit forty cases. Clustering turns the case list into that shape, so triage starts with "the model
stopped emitting the `refund_id` field" instead of scrolling.

Agglomerative (average-linkage) rather than k-means, for one reason: nobody knows k in advance,
and being wrong about k is worse than being slow. The distance threshold is the knob, and it means
something concrete — "cases whose failure text is at least this similar belong together".

Embeddings are the deterministic hashing embedder by default (docs/adr/0004), so clustering the
same run twice gives the same clusters, and clustering costs no model calls.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.domain.embedding import Embedder, default_embedder
from crucible.logging import get_logger

log = get_logger(__name__)

DEFAULT_DISTANCE_THRESHOLD = 0.35
MIN_CLUSTER_SIZE = 2
_STOPWORDS = frozenset(
    """a an the and or but if then than that this these those is are was were be been being to of in on for with
    as at by from it its into not no yes do does did have has had will would should could can may might must
    expected actual got value none null true false error failed failure case run test output input model
    """.split()  # noqa: SIM905 - a readable block beats a 60-element list literal
)

FAILING_CASES_SQL = text(
    """
    SELECT rc.id, rc.external_id, rc.status, rc.error, rc.output, rc.assertion_results
    FROM run_cases rc
    WHERE rc.run_id = :run_id AND rc.status IN ('failed', 'error')
    ORDER BY rc.external_id
    """
)


@dataclass
class ClusterMemberView:
    run_case_id: UUID
    external_id: str
    distance: float
    text: str


@dataclass
class Cluster:
    label: str
    summary: str
    size: int
    top_terms: list[str]
    centroid: list[float]
    members: list[ClusterMemberView] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "summary": self.summary,
            "size": self.size,
            "top_terms": self.top_terms,
            "members": [
                {"run_case_id": str(m.run_case_id), "external_id": m.external_id, "distance": round(m.distance, 4)}
                for m in self.members
            ],
        }


def failure_text(row: Any) -> str:
    """What we cluster on: the *reason* it failed, not the whole transcript.

    Two cases that failed the same assertion in the same way should land close together even if
    their inputs are unrelated, which means the assertion detail matters more than the output and
    the output matters more than the input.
    """
    parts: list[str] = []
    if row.error:
        parts.append(str(row.error))
    for outcome in row.assertion_results or []:
        if not outcome.get("passed"):
            parts.append(f"{outcome.get('type')} {outcome.get('detail') or ''}")
    output = row.output or {}
    if isinstance(output, dict):
        snippet = output.get("text") or ""
        if snippet:
            parts.append(str(snippet)[:600])
    return "\n".join(p for p in parts if p).strip() or "unknown failure"


def agglomerate(vectors: np.ndarray, threshold: float) -> list[list[int]]:
    """Average-linkage agglomerative clustering on cosine distance.

    O(n^2) memory in the number of failures. That is fine: this runs over the *failures* of one
    run, and a run with more than a few thousand failures has a bigger problem than clustering.
    """
    n = vectors.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [[0]]

    # Vectors are L2-normalised, so cosine distance is 1 - dot.
    distances = 1.0 - vectors @ vectors.T
    np.fill_diagonal(distances, np.inf)

    clusters: dict[int, list[int]] = {i: [i] for i in range(n)}
    active = distances.copy()
    sizes = np.ones(n)

    while len(clusters) > 1:
        flat = int(np.argmin(active))
        a, b = divmod(flat, active.shape[1])
        if not np.isfinite(active[a, b]) or active[a, b] > threshold:
            break
        i, j = (a, b) if a < b else (b, a)

        # Lance-Williams update for average linkage: the merged cluster's distance to k is the
        # size-weighted mean of its parts' distances.
        merged = (sizes[i] * active[i, :] + sizes[j] * active[j, :]) / (sizes[i] + sizes[j])
        active[i, :] = merged
        active[:, i] = merged
        active[i, i] = np.inf
        active[j, :] = np.inf
        active[:, j] = np.inf

        clusters[i] = clusters[i] + clusters.pop(j)
        sizes[i] += sizes[j]
        sizes[j] = 0

    return [sorted(members) for members in clusters.values()]


_WORD_RE = re.compile(r"[a-z][a-z0-9_]{2,}")


def top_terms(texts: list[str], limit: int = 6) -> list[str]:
    """Shared vocabulary across a cluster's members, used as a fallback label and as UI chips.

    Terms must start with a letter, so ids, byte counts and millisecond figures — the parts that
    differ between two instances of the *same* failure — do not become the cluster's name.
    """
    counts: Counter[str] = Counter()
    for t in texts:
        for word in set(_WORD_RE.findall(t.lower())):
            if word not in _STOPWORDS:
                counts[word] += 1
    return [w for w, _ in counts.most_common(limit)]


def _label_for(texts: list[str], terms: list[str]) -> str:
    """Prefer the shortest member's first line — it reads like an error message, which is what a
    human scanning a cluster list is looking for. Fall back to the shared vocabulary."""
    first_lines = sorted((t.split("\n", 1)[0].strip() for t in texts if t.strip()), key=len)
    if first_lines and len(first_lines[0]) <= 120:
        return first_lines[0][:200]
    if terms:
        return " · ".join(terms[:4])[:200]
    return "unclassified failures"


def cluster_failures(
    rows: Sequence[Any], *, embedder: Embedder | None = None, threshold: float = DEFAULT_DISTANCE_THRESHOLD
) -> tuple[list[Cluster], list[tuple[UUID, list[float]]]]:
    """Pure function: rows in, clusters plus per-case embeddings out. No database access, so it is
    directly testable and the persistence layer stays a thin wrapper."""
    embedder = embedder or default_embedder()
    if not rows:
        return [], []

    texts = [failure_text(r) for r in rows]
    vectors = np.array([embedder.embed(t) for t in texts], dtype=np.float32)
    embeddings = [(row.id, vectors[i].tolist()) for i, row in enumerate(rows)]

    groups = agglomerate(vectors, threshold)
    clusters: list[Cluster] = []
    for members in groups:
        member_vectors = vectors[members]
        centroid = member_vectors.mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm:
            centroid = centroid / norm
        member_texts = [texts[i] for i in members]
        terms = top_terms(member_texts)
        distances = 1.0 - member_vectors @ centroid

        views = [
            ClusterMemberView(
                run_case_id=rows[idx].id,
                external_id=rows[idx].external_id,
                distance=float(distances[k]),
                text=texts[idx],
            )
            for k, idx in enumerate(members)
        ]
        views.sort(key=lambda m: m.distance)

        clusters.append(
            Cluster(
                label=_label_for(member_texts, terms),
                summary=_summarise(views, len(rows)),
                size=len(members),
                top_terms=terms,
                centroid=centroid.tolist(),
                members=views,
            )
        )

    # Biggest first: the cluster covering half the failures is the one to fix.
    clusters.sort(key=lambda c: (-c.size, c.label))
    return clusters, embeddings


def _summarise(views: list[ClusterMemberView], total_failures: int) -> str:
    share = 100.0 * len(views) / max(1, total_failures)
    example = views[0].external_id if views else "?"
    noun = "failure" if len(views) == 1 else "failures"
    return f"{len(views)} {noun} ({share:.0f}% of all failures), e.g. {example}"


async def cluster_run(
    session: AsyncSession,
    *,
    project_id: UUID,
    run_id: UUID,
    threshold: float = DEFAULT_DISTANCE_THRESHOLD,
    min_size: int = MIN_CLUSTER_SIZE,
    embedder: Embedder | None = None,
) -> list[Cluster]:
    """Recompute and persist clusters for a run. Idempotent: previous clusters are replaced."""
    embedder = embedder or default_embedder()
    rows = (await session.execute(FAILING_CASES_SQL, {"run_id": run_id})).all()
    clusters, embeddings = cluster_failures(rows, embedder=embedder, threshold=threshold)

    await session.execute(text("DELETE FROM failure_clusters WHERE run_id = :run_id"), {"run_id": run_id})

    for run_case_id, vector in embeddings:
        await session.execute(
            text(
                """
                INSERT INTO case_embeddings (run_case_id, project_id, run_id, embedder, embedding)
                VALUES (:run_case_id, :project_id, :run_id, :embedder, :embedding)
                ON CONFLICT (run_case_id) DO UPDATE
                    SET embedding = EXCLUDED.embedding, embedder = EXCLUDED.embedder
                """
            ),
            {
                "run_case_id": run_case_id,
                "project_id": project_id,
                "run_id": run_id,
                "embedder": embedder.name,
                "embedding": _vector_literal(vector),
            },
        )

    persisted: list[Cluster] = []
    for cluster in clusters:
        # Singletons are noise in the UI; the cases are still listed on the run page.
        if cluster.size < min_size:
            continue
        cluster_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO failure_clusters (id, project_id, run_id, label, summary, size, centroid, top_terms)
                    VALUES (gen_random_uuid(), :project_id, :run_id, :label, :summary, :size, :centroid, :top_terms)
                    RETURNING id
                    """
                ),
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "label": cluster.label,
                    "summary": cluster.summary,
                    "size": cluster.size,
                    "centroid": _vector_literal(cluster.centroid),
                    "top_terms": cluster.top_terms,
                },
            )
        ).scalar_one()
        for member in cluster.members:
            await session.execute(
                text(
                    """
                    INSERT INTO cluster_members (id, cluster_id, run_case_id, distance)
                    VALUES (gen_random_uuid(), :cluster_id, :run_case_id, :distance)
                    ON CONFLICT (cluster_id, run_case_id) DO NOTHING
                    """
                ),
                {"cluster_id": cluster_id, "run_case_id": member.run_case_id, "distance": member.distance},
            )
        persisted.append(cluster)

    log.info(
        "run_clustered",
        run_id=str(run_id),
        failures=len(rows),
        clusters=len(persisted),
        singletons=len(clusters) - len(persisted),
    )
    return persisted


async def similar_cases(
    session: AsyncSession, *, project_id: UUID, run_case_id: UUID, limit: int = 10
) -> list[dict[str, Any]]:
    """ "Show me other cases that failed like this one" — a pgvector nearest-neighbour lookup over
    stored embeddings, scoped to the project so one tenant never sees another's failures."""
    rows = (
        await session.execute(
            text(
                """
                WITH anchor AS (
                    SELECT embedding FROM case_embeddings
                    WHERE run_case_id = :run_case_id AND project_id = :project_id
                )
                SELECT ce.run_case_id, rc.external_id, rc.run_id, rc.status,
                       (ce.embedding <=> (SELECT embedding FROM anchor)) AS distance
                FROM case_embeddings ce
                JOIN run_cases rc ON rc.id = ce.run_case_id
                WHERE ce.project_id = :project_id AND ce.run_case_id <> :run_case_id
                ORDER BY ce.embedding <=> (SELECT embedding FROM anchor)
                LIMIT :limit
                """
            ),
            {"run_case_id": run_case_id, "project_id": project_id, "limit": limit},
        )
    ).all()
    return [
        {
            "run_case_id": str(r.run_case_id),
            "external_id": r.external_id,
            "run_id": str(r.run_id),
            "status": r.status,
            "distance": round(float(r.distance), 4),
        }
        for r in rows
    ]


def _vector_literal(vector: list[float]) -> str:
    """pgvector accepts its text form; this keeps the raw-SQL path free of a driver-level codec."""
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
