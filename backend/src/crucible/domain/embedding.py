"""Embeddings for failure clustering.

The default embedder is deterministic, offline and dependency-free: character n-grams hashed into
a fixed-width vector, L2-normalised. It groups failures that *look* alike (same error string, same
malformed field, same refusal phrasing), which is what you actually want when triaging a red run,
and it costs nothing and cannot drift between runs.

It is lexical, not semantic. `docs/adr/0004` explains when to swap in a model-backed embedder; the
`Embedder` protocol is the seam for that.
"""

import hashlib
import itertools
import math
import re
from typing import Protocol

from crucible.config import settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Hashed bag of word-unigrams, word-bigrams and character 4-grams.

    Word features capture vocabulary ("timeout", "refund"); character features survive the typos,
    ids and punctuation noise that real model output is full of.
    """

    name = "hashing-v1"

    def __init__(self, dim: int | None = None):
        self.dim = dim or settings.embedding_dim

    def _bucket(self, feature: str) -> int:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        lowered = text.lower()
        words = _TOKEN_RE.findall(lowered)

        for w in words:
            vec[self._bucket(f"w:{w}")] += 1.0
        for a, b in itertools.pairwise(words):
            vec[self._bucket(f"b:{a}_{b}")] += 1.0

        squashed = re.sub(r"\s+", " ", lowered)
        for i in range(len(squashed) - 3):
            vec[self._bucket(f"c:{squashed[i : i + 4]}")] += 0.5

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            return vec
        return [v / norm for v in vec]


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return 1.0 - max(-1.0, min(1.0, dot))


def default_embedder() -> Embedder:
    return HashingEmbedder()
