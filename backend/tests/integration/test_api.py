"""The API end to end: sign up, create a project, push a target and a dataset, start a run.

These go through the real ASGI app and a real Postgres. What they are actually checking is the
authorization boundary and the idempotency contract — the two things that are cheap to get subtly
wrong and expensive to discover in production.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

# `[[expect:VALUE]]` is the mock provider's directive, so a run's pass/fail is scriptable.
CASES = [
    {
        "external_id": f"case-{i:02d}",
        "input": {"question": f"code for {i}? [[expect:CODE-{i}]]"},
        "expected": {"answer": f"CODE-{i}"},
    }
    for i in (1, 2)
]

TARGET_CONFIG = {
    "architecture": "single_shot",
    "model": "mock-small",
    "prompt_template": "{{question}}",
    "max_tokens": 128,
}


async def signup(client: Any, email: str = "dev@example.com") -> str:
    response = await client.post(
        "/api/v1/auth/signup", json={"email": email, "name": "Dev", "password": "correct-horse-battery"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["access_token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def bootstrap(client: Any, token: str) -> dict[str, Any]:
    """Project, target version, dataset version, suite — the minimum a run needs."""
    headers = auth(token)
    project = (await client.post("/api/v1/projects", json={"name": "Acme", "slug": "acme"}, headers=headers)).json()
    base = f"/api/v1/projects/{project['id']}"

    target = (
        await client.post(f"{base}/targets", json={"name": "support-agent", "kind": "prompt"}, headers=headers)
    ).json()
    version = (
        await client.post(f"{base}/targets/{target['id']}/versions", json={"config": TARGET_CONFIG}, headers=headers)
    ).json()

    dataset = (await client.post(f"{base}/datasets", json={"name": "golden"}, headers=headers)).json()
    dataset_version = (
        await client.post(f"{base}/datasets/{dataset['id']}/versions", json={"cases": CASES}, headers=headers)
    ).json()

    suite = (
        await client.post(
            f"{base}/suites",
            json={
                "name": "default",
                "dataset_id": dataset["id"],
                "assertions": [{"type": "equals", "value_from": "expected.answer"}],
            },
            headers=headers,
        )
    ).json()

    return {
        "project_id": project["id"],
        "base": base,
        "target_id": target["id"],
        "target_version_id": version["id"],
        "dataset_id": dataset["id"],
        "dataset_version_id": dataset_version["id"],
        "suite_id": suite["id"],
        "headers": headers,
    }


async def test_health_does_not_require_credentials(client: Any) -> None:
    assert (await client.get("/healthz")).status_code == 200
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"


async def test_unauthenticated_requests_get_the_error_envelope(client: Any) -> None:
    response = await client.get("/api/v1/projects")
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "UNAUTHORIZED"
    # Every error carries the id that ties it to the server-side log line.
    assert body["request_id"] == response.headers["X-Request-ID"]


async def test_signup_login_and_me(client: Any) -> None:
    token = await signup(client)
    me = await client.get("/api/v1/auth/me", headers=auth(token))
    assert me.json()["email"] == "dev@example.com"

    login = await client.post(
        "/api/v1/auth/login", json={"email": "dev@example.com", "password": "correct-horse-battery"}
    )
    assert login.status_code == 200

    wrong = await client.post("/api/v1/auth/login", json={"email": "dev@example.com", "password": "nope-nope-nope"})
    assert wrong.status_code == 401
    # Identical message for an unknown address, so the form cannot be used to enumerate accounts.
    unknown = await client.post("/api/v1/auth/login", json={"email": "who@example.com", "password": "nope-nope-nope"})
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


async def test_duplicate_signup_is_a_conflict(client: Any) -> None:
    await signup(client)
    again = await client.post(
        "/api/v1/auth/signup", json={"email": "dev@example.com", "name": "Dev", "password": "correct-horse-battery"}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


async def test_pushing_an_unchanged_target_config_reuses_the_version(client: Any) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)

    again = await client.post(
        f"{ctx['base']}/targets/{ctx['target_id']}/versions", json={"config": TARGET_CONFIG}, headers=ctx["headers"]
    )
    assert again.json()["reused"] is True
    assert again.json()["id"] == ctx["target_version_id"]

    changed = await client.post(
        f"{ctx['base']}/targets/{ctx['target_id']}/versions",
        json={"config": {**TARGET_CONFIG, "system": "Be terse."}},
        headers=ctx["headers"],
    )
    assert changed.json()["reused"] is False
    assert changed.json()["semver"] == "1.0.1"


async def test_pushing_an_unchanged_dataset_reuses_the_version(client: Any) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)

    again = await client.post(
        f"{ctx['base']}/datasets/{ctx['dataset_id']}/versions", json={"cases": CASES}, headers=ctx["headers"]
    )
    assert again.json() == {**again.json(), "reused": True, "version": 1}

    extended = await client.post(
        f"{ctx['base']}/datasets/{ctx['dataset_id']}/versions",
        json={"cases": [*CASES, {"external_id": "case-03", "input": {"question": "x"}}]},
        headers=ctx["headers"],
    )
    assert extended.json()["version"] == 2
    assert extended.json()["case_count"] == 3


async def test_duplicate_external_ids_are_rejected(client: Any) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    response = await client.post(
        f"{ctx['base']}/datasets/{ctx['dataset_id']}/versions",
        json={"cases": [CASES[0], CASES[0]]},
        headers=ctx["headers"],
    )
    assert response.status_code == 400
    assert "duplicate external_id" in response.json()["error"]["message"]


async def test_invalid_assertions_are_rejected_before_any_model_call(client: Any) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    response = await client.post(
        f"{ctx['base']}/suites",
        json={"name": "broken", "dataset_id": ctx["dataset_id"], "assertions": [{"type": "no-such-assertion"}]},
        headers=ctx["headers"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_run_is_queued_and_idempotent(client: Any) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    payload = {
        "target_version_id": ctx["target_version_id"],
        "suite_id": ctx["suite_id"],
        "idempotency_key": "ci-run-42",
    }

    first = await client.post(f"{ctx['base']}/runs", json=payload, headers=ctx["headers"])
    assert first.status_code == 202
    assert first.json()["total_cases"] == 2
    assert first.json()["reused"] is False

    # The retry a CI job makes after a network blip must not pay for the suite twice.
    second = await client.post(f"{ctx['base']}/runs", json=payload, headers=ctx["headers"])
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["reused"] is True

    run = await client.get(f"{ctx['base']}/runs/{first.json()['id']}", headers=ctx["headers"])
    assert run.json()["status"] == "queued"
    assert run.json()["total_cases"] == 2

    cases = await client.get(f"{ctx['base']}/runs/{first.json()['id']}/cases", headers=ctx["headers"])
    assert [c["external_id"] for c in cases.json()["items"]] == ["case-01", "case-02"]
    assert all(c["status"] == "pending" for c in cases.json()["items"])


async def test_another_users_project_is_not_found_not_forbidden(client: Any) -> None:
    owner_token = await signup(client, "owner@example.com")
    ctx = await bootstrap(client, owner_token)

    intruder_token = await signup(client, "intruder@example.com")
    response = await client.get(ctx["base"], headers=auth(intruder_token))

    # 404, not 403: a 403 would confirm the project exists to someone who cannot see it.
    assert response.status_code == 404
    assert (await client.get("/api/v1/projects", headers=auth(intruder_token))).json() == []


async def test_api_key_is_scoped_to_its_project(client: Any) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)

    created = await client.post(f"{ctx['base']}/keys", json={"name": "ci"}, headers=ctx["headers"])
    assert created.status_code == 201
    key = created.json()["key"]
    assert key.startswith("crk_")

    # The key works on its own project...
    assert (await client.get(ctx["base"], headers={"X-API-Key": key})).status_code == 200

    # ...and cannot mint more keys, so a leaked CI secret is not an escalation path.
    assert (await client.post(f"{ctx['base']}/keys", json={"name": "x"}, headers={"X-API-Key": key})).status_code == 403

    other = await client.post("/api/v1/projects", json={"name": "Other", "slug": "other"}, headers=ctx["headers"])
    denied = await client.get(f"/api/v1/projects/{other.json()['id']}", headers={"X-API-Key": key})
    assert denied.status_code == 404

    # Listing keys never returns the secret — only the prefix, which is enough to tell two keys
    # apart in the UI and useless to anyone who steals the listing.
    listed = (await client.get(f"{ctx['base']}/keys", headers=ctx["headers"])).json()
    assert "key" not in listed[0]
    assert listed[0]["prefix"] == key[:12]
    assert key[12:] not in str(listed)


async def test_revoked_key_stops_working(client: Any) -> None:
    token = await signup(client)
    ctx = await bootstrap(client, token)
    created = (await client.post(f"{ctx['base']}/keys", json={"name": "ci"}, headers=ctx["headers"])).json()
    key = created["key"]

    assert (await client.get(ctx["base"], headers={"X-API-Key": key})).status_code == 200
    assert (await client.delete(f"{ctx['base']}/keys/{created['id']}", headers=ctx["headers"])).status_code == 204
    assert (await client.get(ctx["base"], headers={"X-API-Key": key})).status_code == 401


async def test_validation_errors_use_the_same_envelope(client: Any) -> None:
    token = await signup(client)
    response = await client.post("/api/v1/projects", json={"name": "Acme", "slug": "Not A Slug"}, headers=auth(token))
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"][0]["field"] == "slug"


async def test_listing_runs_reports_what_it_did_not_return(client: Any) -> None:
    """The regression: a page that is full tells a caller nothing about what is behind it.

    `limit` was always enforced, so this endpoint had been silently correct and visibly wrong —
    three runs came back as three runs whether or not there were thirty. `total` counts the rows
    matching the filter, not the rows in the page, which is what lets a client know to ask again.
    """
    token = await signup(client)
    ctx = await bootstrap(client, token)
    for key in ("a", "b", "c"):
        response = await client.post(
            f"{ctx['base']}/runs",
            json={
                "target_version_id": ctx["target_version_id"],
                "suite_id": ctx["suite_id"],
                "idempotency_key": key,
            },
            headers=ctx["headers"],
        )
        assert response.status_code == 202

    first = (await client.get(f"{ctx['base']}/runs", params={"limit": 2}, headers=ctx["headers"])).json()
    assert len(first["items"]) == 2
    assert first["total"] == 3
    assert (first["limit"], first["offset"]) == (2, 0)

    second = (await client.get(f"{ctx['base']}/runs", params={"limit": 2, "offset": 2}, headers=ctx["headers"])).json()
    assert len(second["items"]) == 1
    assert second["total"] == 3

    # No run is served twice and none is skipped — the ordering is stable across the page boundary.
    paged = [r["id"] for r in first["items"] + second["items"]]
    assert len(set(paged)) == 3

    # The total has to answer for the filter, not for the table. Counting all three here would make
    # a filtered view claim there was a second page that does not exist.
    filtered = (await client.get(f"{ctx['base']}/runs", params={"status": "running"}, headers=ctx["headers"])).json()
    assert filtered["items"] == []
    assert filtered["total"] == 0
