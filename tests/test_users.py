import pytest

import app.routers.users as users


class _FakeSettings:
    def clerk_secret_key_for_issuer(self, issuer):
        return "test-secret"


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, *args, **kwargs):
        return _FakeResponse(self.payload)


def _patch_clerk_response(monkeypatch, payload):
    monkeypatch.setattr(users, "settings", _FakeSettings())
    monkeypatch.setattr(users.httpx, "AsyncClient", lambda timeout: _FakeAsyncClient(payload))


@pytest.mark.asyncio
async def test_fetch_clerk_primary_email_requires_verified_status(monkeypatch):
    _patch_clerk_response(monkeypatch, {
        "primary_email_address_id": "email_1",
        "email_addresses": [
            {
                "id": "email_1",
                "email_address": "person@example.com",
                "verification": {},
            }
        ],
    })

    assert await users._fetch_clerk_primary_email("user_123", "issuer") is None


@pytest.mark.asyncio
async def test_fetch_clerk_primary_email_returns_verified_email(monkeypatch):
    _patch_clerk_response(monkeypatch, {
        "primary_email_address_id": "email_1",
        "email_addresses": [
            {
                "id": "email_1",
                "email_address": " person@example.com ",
                "verification": {"status": "verified"},
            }
        ],
    })

    assert await users._fetch_clerk_primary_email("user_123", "issuer") == "person@example.com"
