"""The configuration guards that only fire in production.

These are worth a test precisely because nothing exercises them in development: the failure they
prevent is invisible on a laptop and total in production.
"""

import pytest
from pydantic import ValidationError

from crucible.config import DEV_JWT_SECRET, Settings

REAL_SECRET = "a" * 40


def test_development_boots_with_the_default_secret() -> None:
    # The default has to keep working, or `make api` needs a secret nobody has generated yet.
    assert Settings(env="dev").jwt_secret == DEV_JWT_SECRET


def test_production_refuses_the_committed_default_secret() -> None:
    with pytest.raises(ValidationError, match="jwt_secret is still the development default"):
        Settings(env="prod")


def test_production_boots_with_a_real_secret() -> None:
    assert Settings(env="prod", jwt_secret=REAL_SECRET).jwt_secret == REAL_SECRET


def test_production_refuses_an_insecure_cookie_over_https() -> None:
    with pytest.raises(ValidationError, match="cookie_secure is False but app_url is https"):
        Settings(env="prod", jwt_secret=REAL_SECRET, app_url="https://crucible.example", cookie_secure=False)


def test_production_allows_an_insecure_cookie_over_http() -> None:
    # An http app_url is a proxy-terminated or single-host deployment, not necessarily a mistake,
    # and refusing it would block the one setup where it is legitimate.
    settings = Settings(env="prod", jwt_secret=REAL_SECRET, app_url="http://localhost:3200")
    assert settings.cookie_secure is False


def test_the_length_check_alone_would_have_passed() -> None:
    """The regression this guards: `min_length=32` is satisfied by a public string."""
    assert len(DEV_JWT_SECRET) >= 32
