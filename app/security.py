"""Security helpers for validating and fetching outbound URLs.

The API fetches user-provided recipe pages and external thumbnails. These helpers
keep those fetches limited to public HTTP(S) destinations so the server cannot be
used to reach localhost, cloud metadata, or private network hosts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, status

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _hostname_is_blocked(hostname: str) -> bool:
    host = hostname.strip().lower().rstrip(".")

    if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        return True

    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_blocked(ip)
    except ValueError:
        return False


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return any(
        [
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        ]
    )


def _parse_public_url(url: str):
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL is required")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only HTTP and HTTPS URLs are supported",
        )

    hostname = parsed.hostname
    if not hostname or _hostname_is_blocked(hostname):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This URL is not supported",
        )

    return parsed


async def resolve_public_http_url(url: str) -> tuple[str, int, str]:
    """Validate a URL and return a public IP pinned for the outbound request."""
    parsed = _parse_public_url(url)
    hostname = parsed.hostname or ""
    port = parsed.port or _DEFAULT_PORTS[parsed.scheme.lower()]

    try:
        addr_info = await asyncio.to_thread(socket.getaddrinfo, hostname, port)
    except socket.gaierror:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to resolve this URL",
        )

    public_ips: list[str] = []
    for item in addr_info:
        ip_text = item[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This URL is not supported",
            )

        if _ip_is_blocked(ip):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This URL is not supported",
            )
        public_ips.append(ip_text)

    if not public_ips:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to resolve this URL",
        )

    return hostname, port, public_ips[0]


async def assert_public_http_url(url: str) -> None:
    """Raise HTTPException if a URL does not point to a public HTTP(S) host."""
    await resolve_public_http_url(url)


def _host_header(hostname: str, port: int, scheme: str) -> str:
    default_port = _DEFAULT_PORTS[scheme]
    return hostname if port == default_port else f"{hostname}:{port}"


class PublicHTTPTransport(httpx.AsyncBaseTransport):
    """HTTPX transport that pins requests to a validated public IP address.

    URL validation alone leaves a DNS-rebinding gap because the HTTP client may
    resolve the hostname again when it opens the socket. This transport resolves
    and validates the target itself, rewrites the socket destination to the
    validated IP, and preserves the original Host header and HTTPS SNI hostname.
    """

    def __init__(self, **kwargs):
        self._transport = httpx.AsyncHTTPTransport(trust_env=False, **kwargs)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_url = request.url
        scheme = original_url.scheme.lower()
        hostname, port, ip_text = await resolve_public_http_url(str(original_url))

        request.url = original_url.copy_with(host=ip_text)
        request.headers["Host"] = _host_header(hostname, port, scheme)
        if scheme == "https":
            request.extensions["sni_hostname"] = hostname

        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def is_public_http_url(url: str) -> bool:
    """Synchronous lightweight URL validation for non-FastAPI callers."""
    try:
        parsed = _parse_public_url(url)
        return parsed.scheme.lower() in _ALLOWED_SCHEMES
    except Exception:
        return False
