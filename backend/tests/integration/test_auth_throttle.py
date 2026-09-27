"""The auth throttle, exercised against real Redis.

These tests hit the counters the way an attacker would — repeated requests — rather than calling
`throttle.check` directly, because the property that matters is end-to-end: the 429 leaves through
the same envelope as every other error, carries Retry-After, and fires *before* the password hash
is verified.
"""

from typing import Any

PASSWORD = "correct-horse-battery"


async def _signup(client: Any, email: str) -> None:
    response = await client.post("/api/v1/auth/signup", json={"email": email, "name": "Dev", "password": PASSWORD})
    assert response.status_code == 201


async def test_login_is_throttled_per_email(client: Any) -> None:
    """Ten attempts against one account in five minutes, then a 429 — success or failure alike.

    Attempts are counted before the password check on purpose: the throttle is the cheap gate in
    front of the expensive Argon2 verify, and counting only failures would mean paying for the
    hash on every guess anyway.
    """
    await _signup(client, "victim@example.com")

    for _ in range(9):  # signup consumed nothing on the login scope; 9 wrong + 1 more = 10
        response = await client.post(
            "/api/v1/auth/login", json={"email": "victim@example.com", "password": "wrong-wrong-wrong"}
        )
        assert response.status_code == 401

    # The tenth attempt still gets through (right password, proving success is also counted)…
    response = await client.post("/api/v1/auth/login", json={"email": "victim@example.com", "password": PASSWORD})
    assert response.status_code == 200

    # …and the eleventh is refused with the standard envelope and a Retry-After.
    response = await client.post("/api/v1/auth/login", json={"email": "victim@example.com", "password": PASSWORD})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert int(response.headers["Retry-After"]) >= 1


async def test_throttling_one_email_does_not_lock_out_another(client: Any) -> None:
    """The per-email budget is per email — a spray against one account must not dim the site.

    (The per-IP budget does exist and is shared, but its limit is higher precisely so that one
    throttled victim plus normal traffic never collide with it.)
    """
    await _signup(client, "victim@example.com")
    await _signup(client, "bystander@example.com")

    for _ in range(11):
        await client.post("/api/v1/auth/login", json={"email": "victim@example.com", "password": "wrong-wrong-wrong"})

    response = await client.post("/api/v1/auth/login", json={"email": "bystander@example.com", "password": PASSWORD})
    assert response.status_code == 200
