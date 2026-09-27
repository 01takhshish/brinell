"""Content addressing is the backbone of "which version produced this result", so the properties
worth testing are the ones that would silently corrupt history if they broke."""

import pytest

from crucible.domain.hashing import (
    bump,
    canonical_json,
    content_hash,
    max_semver,
    parse_semver,
    request_fingerprint,
)


def test_hash_is_independent_of_key_order():
    a = {"model": "claude-haiku-4-5", "temperature": 0.0, "system": "be terse"}
    b = {"system": "be terse", "model": "claude-haiku-4-5", "temperature": 0.0}
    assert content_hash(a) == content_hash(b)


def test_hash_is_independent_of_nested_key_order():
    a = {"outer": {"x": 1, "y": [{"p": 1, "q": 2}]}}
    b = {"outer": {"y": [{"q": 2, "p": 1}], "x": 1}}
    assert content_hash(a) == content_hash(b)


def test_hash_changes_when_a_value_changes():
    assert content_hash({"temperature": 0.0}) != content_hash({"temperature": 0.1})


def test_hash_distinguishes_string_from_number():
    """`{"n": 1}` and `{"n": "1"}` are different configurations and must not collide."""
    assert content_hash({"n": 1}) != content_hash({"n": "1"})


def test_canonical_json_preserves_unicode():
    assert canonical_json({"t": "café"}) == '{"t":"café"}'


def test_fingerprint_separates_providers():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert request_fingerprint("anthropic", "m", payload) != request_fingerprint("openai", "m", payload)


def test_fingerprint_separates_models():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert request_fingerprint("anthropic", "a", payload) != request_fingerprint("anthropic", "b", payload)


@pytest.mark.parametrize(
    ("version", "part", "expected"),
    [("1.2.3", "patch", "1.2.4"), ("1.2.3", "minor", "1.3.0"), ("1.2.3", "major", "2.0.0")],
)
def test_bump(version, part, expected):
    assert bump(version, part) == expected


def test_max_semver_orders_numerically_not_lexically():
    assert max_semver(["1.9.0", "1.10.0", "1.2.0"]) == "1.10.0"


def test_max_semver_ignores_junk():
    assert max_semver(["1.0.0", "not-a-version"]) == "1.0.0"


def test_parse_semver_rejects_partial_versions():
    with pytest.raises(ValueError):
        parse_semver("1.2")
