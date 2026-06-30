import socket

import pytest
from fastapi import HTTPException

from app.security import assert_public_http_url, is_public_http_url, resolve_public_http_url


def test_is_public_http_url_rejects_private_and_non_http_urls():
    assert is_public_http_url("https://example.com/recipes") is True
    assert is_public_http_url("http://localhost:8000/internal") is False
    assert is_public_http_url("http://127.0.0.1:8000/internal") is False
    assert is_public_http_url("file:///etc/passwd") is False


@pytest.mark.asyncio
async def test_assert_public_http_url_rejects_localhost_before_dns_lookup():
    with pytest.raises(HTTPException) as exc_info:
        await assert_public_http_url("http://localhost:8000/internal")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_assert_public_http_url_rejects_non_http_schemes():
    with pytest.raises(HTTPException) as exc_info:
        await assert_public_http_url("ftp://example.com/recipe")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_public_http_url_rejects_private_dns_results(monkeypatch):
    def fake_getaddrinfo(hostname, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(HTTPException) as exc_info:
        await resolve_public_http_url("https://example.com/recipe")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_public_http_url_returns_validated_public_ip(monkeypatch):
    def fake_getaddrinfo(hostname, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert await resolve_public_http_url("https://example.com/recipe") == (
        "example.com",
        443,
        "93.184.216.34",
    )
