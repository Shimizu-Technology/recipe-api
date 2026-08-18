import importlib

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.config import get_settings


def _reload_auth(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@example.com/db")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("CLERK_JWT_ISSUERS", "https://allowed.clerk.accounts.dev")
    get_settings.cache_clear()

    import app.auth as auth

    return importlib.reload(auth)


def _unsigned_token(payload: dict) -> str:
    return jwt.encode(payload, key="", algorithm="none")


def test_get_token_issuer_unverified_strips_trailing_slash(monkeypatch):
    auth = _reload_auth(monkeypatch)
    token = _unsigned_token({"iss": "https://allowed.clerk.accounts.dev/", "sub": "user_123"})

    assert auth.get_token_issuer_unverified(token) == "https://allowed.clerk.accounts.dev"


def test_verify_clerk_token_rejects_unallowed_issuer_before_jwks_lookup(monkeypatch):
    auth = _reload_auth(monkeypatch)
    token = _unsigned_token({"iss": "https://other.clerk.accounts.dev", "sub": "user_123"})

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_clerk_token(token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token issuer"


def test_get_token_issuer_unverified_rejects_missing_issuer(monkeypatch):
    auth = _reload_auth(monkeypatch)
    token = _unsigned_token({"sub": "user_123"})

    with pytest.raises(HTTPException) as exc_info:
        auth.get_token_issuer_unverified(token)

    assert exc_info.value.status_code == 401
    assert "missing issuer" in exc_info.value.detail


@pytest.mark.asyncio
async def test_optional_auth_treats_missing_credentials_as_guest(monkeypatch):
    auth = _reload_auth(monkeypatch)

    assert await auth.get_optional_user(None) is None


@pytest.mark.asyncio
async def test_optional_auth_rejects_invalid_credentials(monkeypatch):
    auth = _reload_auth(monkeypatch)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid-token")

    with pytest.raises(HTTPException) as exc_info:
        await auth.get_optional_user(credentials)

    assert exc_info.value.status_code == 401
