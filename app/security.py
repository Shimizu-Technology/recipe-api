"""Security helpers for validating outbound URLs.

The API fetches user-provided recipe pages and external thumbnails. These helpers
keep those fetches limited to public HTTP(S) destinations so the server cannot be
used to reach localhost, cloud metadata, or private network hosts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException, status

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


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


async def assert_public_http_url(url: str) -> None:
    """Raise HTTPException if a URL does not point to a public HTTP(S) host."""
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

    try:
        addr_info = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except socket.gaierror:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to resolve this URL",
        )

    for item in addr_info:
        ip_text = item[4][0]
        try:
            if _ip_is_blocked(ipaddress.ip_address(ip_text)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This URL is not supported",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This URL is not supported",
            )


def is_public_http_url(url: str) -> bool:
    """Synchronous lightweight URL validation for non-FastAPI callers."""
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme.lower() in _ALLOWED_SCHEMES and bool(parsed.hostname) and not _hostname_is_blocked(parsed.hostname)
    except Exception:
        return False
