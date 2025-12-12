"""
Clerk JWT authentication for FastAPI.

Verifies JWT tokens issued by Clerk and extracts user information.
"""

import httpx
import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from functools import lru_cache
from typing import Optional
from pydantic import BaseModel

from app.config import get_settings

settings = get_settings()

# HTTP Bearer scheme for extracting tokens
security = HTTPBearer(auto_error=False)


class ClerkUser(BaseModel):
    """Authenticated user from Clerk JWT."""
    id: str  # Clerk user ID (e.g., "user_2abc123...")
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    image_url: Optional[str] = None
    role: Optional[str] = None  # From public_metadata (e.g., "admin")
    
    @property
    def is_admin(self) -> bool:
        """Check if user has admin role."""
        return self.role == "admin"

    @property
    def display_name(self) -> str:
        """
        Get a user-friendly display name.
        
        Priority:
        1. first_name + last_name (if both present)
        2. first_name only (if no last name)
        3. email prefix before @ (if no name)
        4. "A chef" (fallback)
        """
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        elif self.email:
            # Use part before @ as display name
            return self.email.split("@")[0]
        else:
            return "A chef"


# Cache the JWKS client to avoid repeated fetches
_jwks_client: Optional[PyJWKClient] = None


def get_jwks_client() -> PyJWKClient:
    """Get or create JWKS client for Clerk."""
    global _jwks_client
    if _jwks_client is None:
        # Clerk's JWKS endpoint
        jwks_url = f"https://{settings.clerk_frontend_api}/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url)
    return _jwks_client


def verify_clerk_token(token: str) -> ClerkUser:
    """
    Verify a Clerk JWT token and return user info.
    
    Raises HTTPException if token is invalid.
    """
    try:
        # Get the signing key from Clerk's JWKS
        jwks_client = get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        # Decode and verify the token
        # Add 60 second leeway to handle clock skew between client and server
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},  # Clerk doesn't always set audience
            leeway=60  # 60 seconds tolerance for clock differences
        )
        
        # Extract user info from token
        # public_metadata is included if you've configured your Clerk JWT template
        public_metadata = payload.get("public_metadata", {}) or {}
        
        return ClerkUser(
            id=payload.get("sub"),
            email=payload.get("email"),
            first_name=payload.get("first_name"),
            last_name=payload.get("last_name"),
            image_url=payload.get("image_url"),
            role=public_metadata.get("role"),
        )
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> ClerkUser:
    """
    FastAPI dependency to get the current authenticated user.
    
    Usage:
        @app.get("/protected")
        async def protected_route(user: ClerkUser = Depends(get_current_user)):
            return {"user_id": user.id}
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return verify_clerk_token(credentials.credentials)


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[ClerkUser]:
    """
    FastAPI dependency to get the current user if authenticated, or None.
    
    Useful for endpoints that work both authenticated and unauthenticated.
    
    Usage:
        @app.get("/recipes")
        async def list_recipes(user: Optional[ClerkUser] = Depends(get_optional_user)):
            if user:
                # Return user's private recipes
            else:
                # Return only public recipes
    """
    if credentials is None:
        return None
    
    try:
        return verify_clerk_token(credentials.credentials)
    except HTTPException:
        return None

