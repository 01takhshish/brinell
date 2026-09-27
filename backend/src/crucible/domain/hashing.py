"""Content addressing.

A target version's identity is the hash of what it *does*, not when it was uploaded. Two pushes
of an identical config resolve to the same version, so a run can never be pinned to an ambiguous
"latest" and a comparison between versions is a comparison between real differences.
"""

import hashlib
import json
import re
from typing import Any

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def canonical_json(value: Any) -> str:
    """Stable serialisation: sorted keys, no insignificant whitespace, UTF-8 preserved.

    Key order is the classic silent hash-buster — the same dict from YAML and from JSON must
    hash identically.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_semver(v: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(v)
    if not m:
        raise ValueError(f"not a semantic version: {v!r}")
    return int(m[1]), int(m[2]), int(m[3])


def bump(v: str, part: str = "patch") -> str:
    major, minor, patch = parse_semver(v)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def max_semver(versions: list[str]) -> str | None:
    parsed = [(parse_semver(v), v) for v in versions if SEMVER_RE.match(v)]
    return max(parsed)[1] if parsed else None


def request_fingerprint(provider: str, model: str, payload: dict[str, Any]) -> str:
    """Cache key for a provider call.

    Deliberately includes everything that can change the output (model, messages, system, tools,
    sampling, thinking) and nothing that cannot (request ids, timestamps, run ids). Getting this
    wrong in either direction is expensive: too loose serves wrong answers, too tight bills twice.
    """
    return content_hash({"p": provider, "m": model, "r": payload})
