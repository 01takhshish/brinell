"""The offline embedder.

Clustering is only useful if similar failures land near each other and unrelated ones do not, so
those two properties are what this asserts — not the exact numbers, which are an implementation
detail of the hashing scheme.
"""

from __future__ import annotations

from crucible.domain.embedding import HashingEmbedder, cosine_distance


def test_embeddings_are_deterministic():
    """Two runs of the same suite must produce the same clusters, so the same text must produce
    the same vector — in this process and in the next one."""
    a, b = HashingEmbedder(dim=128), HashingEmbedder(dim=128)
    assert a.embed("timeout after 30s") == b.embed("timeout after 30s")


def test_vectors_are_unit_length():
    vec = HashingEmbedder(dim=128).embed("refund_id missing from response")
    assert abs(sum(v * v for v in vec) ** 0.5 - 1.0) < 1e-9


def test_empty_text_does_not_explode():
    vec = HashingEmbedder(dim=64).embed("")
    assert vec == [0.0] * 64


def test_similar_failures_are_closer_than_unrelated_ones():
    e = HashingEmbedder(dim=512)
    a = e.embed("expected 'RF-1001', got 'refund unavailable'")
    b = e.embed("expected 'RF-2002', got 'refund unavailable'")
    c = e.embed("latency_budget_ms exceeded: 4200ms > 2000ms")

    assert cosine_distance(a, b) < 0.35, "same failure shape, different ids"
    assert cosine_distance(a, c) > cosine_distance(a, b) * 2


def test_a_typo_does_not_move_a_failure_into_a_different_cluster():
    """Character n-grams are what make this true; a pure word model would treat these as unrelated."""
    e = HashingEmbedder(dim=512)
    assert cosine_distance(e.embed("connection timeout"), e.embed("connection timout")) < 0.5


def test_distance_of_a_vector_to_itself_is_zero():
    vec = HashingEmbedder(dim=128).embed("anything at all")
    assert abs(cosine_distance(vec, vec)) < 1e-9
