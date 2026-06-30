import pytest
from fastapi import HTTPException

from app.security import assert_public_http_url, is_public_http_url


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
