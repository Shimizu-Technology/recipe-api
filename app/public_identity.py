"""Stable public contributor identifiers.

Clerk subject IDs are authorization identifiers, not public profile IDs. Public
recipe responses use a one-way digest so clients can filter by contributor
without learning another user's Clerk ID. The prefix/version makes the value
easy to recognize and gives us room to migrate the format later.
"""

import base64
import hashlib

PUBLIC_CONTRIBUTOR_NAMESPACE = b"hafa-recipes:public-contributor:v1\0"


def public_contributor_id(user_id: str | None) -> str | None:
    """Return a stable opaque identifier for a high-entropy Clerk subject ID."""
    if not user_id:
        return None

    digest = hashlib.sha256(PUBLIC_CONTRIBUTOR_NAMESPACE + user_id.encode("utf-8")).digest()
    encoded = base64.urlsafe_b64encode(digest[:18]).decode("ascii").rstrip("=")
    return f"chef_{encoded}"


def visible_recipe_user_id(owner_user_id: str | None, viewer_user_id: str | None) -> str | None:
    """Keep current-client owner checks working without exposing other subjects."""
    if owner_user_id and viewer_user_id and owner_user_id == viewer_user_id:
        return owner_user_id
    return public_contributor_id(owner_user_id)
