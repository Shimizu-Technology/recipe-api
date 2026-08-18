"""S3 storage service for persisting recipe thumbnails."""

import hashlib
from typing import Optional
from urllib.parse import urljoin, urlparse
from uuid import UUID

import boto3
import httpx
from botocore.exceptions import ClientError

from app.config import get_settings
from app.image_validation import (
    ImageValidationError,
    decode_and_validate_base64_image,
    validate_image_bytes,
)
from app.security import PublicHTTPTransport

MAX_THUMBNAIL_BYTES = 10 * 1024 * 1024
MAX_CHAT_IMAGE_BYTES = 8 * 1024 * 1024


class StorageService:
    """
    Handles uploading and managing images in S3.
    
    Thumbnails are stored with the pattern: thumbnails/{recipe_id}.jpg
    """
    
    def __init__(self):
        self._client = None
    
    @property
    def client(self):
        """Lazy-load S3 client."""
        if self._client is None:
            settings = get_settings()
            if settings.s3_enabled:
                self._client = boto3.client(
                    "s3",
                    aws_access_key_id=settings.aws_access_key_id,
                    aws_secret_access_key=settings.aws_secret_access_key,
                    region_name=settings.aws_region,
                )
        return self._client
    
    @property
    def bucket_name(self) -> Optional[str]:
        """Get bucket name from settings."""
        return get_settings().s3_bucket_name
    
    @property
    def is_enabled(self) -> bool:
        """Check if S3 storage is enabled."""
        return get_settings().s3_enabled
    
    async def _download_public_url(self, image_url: str) -> tuple[bytes, str]:
        """Download a public HTTP(S) URL, validating every redirect target."""
        current_url = image_url

        async with httpx.AsyncClient(timeout=30.0, transport=PublicHTTPTransport()) as client:
            for _ in range(6):
                async with client.stream("GET", current_url, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Redirect missing Location header")
                        current_url = urljoin(current_url, location)
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
                    if not content_type.lower().startswith("image/"):
                        raise ValueError("Thumbnail URL did not return an image")

                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit():
                        if int(content_length) > MAX_THUMBNAIL_BYTES:
                            raise ValueError("Thumbnail exceeds maximum size")

                    chunks = []
                    total_size = 0
                    async for chunk in response.aiter_bytes():
                        total_size += len(chunk)
                        if total_size > MAX_THUMBNAIL_BYTES:
                            raise ValueError("Thumbnail exceeds maximum size")
                        chunks.append(chunk)

                    return b"".join(chunks), content_type

        raise ValueError("Too many redirects downloading thumbnail")

    async def upload_thumbnail_from_url(
        self, 
        image_url: str, 
        recipe_id: str | UUID
    ) -> Optional[str]:
        """
        Download an image from URL and upload to S3.
        
        Args:
            image_url: External URL of the thumbnail
            recipe_id: Recipe ID to use as filename
            
        Returns:
            S3 URL if successful, None if failed or S3 not configured
        """
        if not self.is_enabled:
            print("⚠️ S3 not configured, skipping thumbnail upload")
            return None
        
        if not image_url:
            return None
        
        try:
            # Download image from external URL
            print(f"📥 Downloading thumbnail from: {image_url[:60]}...")
            image_data, content_type = await self._download_public_url(image_url)
            validated = validate_image_bytes(
                image_data,
                max_bytes=MAX_THUMBNAIL_BYTES,
                declared_content_type=content_type,
            )
            image_data = validated.data
            content_type = validated.content_type
            
            # Determine file extension
            if "png" in content_type:
                extension = "png"
            elif "webp" in content_type:
                extension = "webp"
            elif "gif" in content_type:
                extension = "gif"
            else:
                extension = "jpg"
            
            # Upload to S3
            s3_key = f"thumbnails/{recipe_id}.{extension}"
            
            print(f"📤 Uploading to S3: {s3_key}")
            
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=image_data,
                ContentType=content_type,
                # Note: Public access is controlled by bucket policy, not ACL
            )
            
            # Generate public URL
            settings = get_settings()
            s3_url = f"https://{self.bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
            
            print(f"✅ Thumbnail uploaded: {s3_url}")
            return s3_url
            
        except httpx.HTTPError as e:
            print(f"❌ Failed to download thumbnail: {e}")
            return None
        except ClientError as e:
            print(f"❌ Failed to upload to S3: {e}")
            return None
        except Exception as e:
            print(f"❌ Unexpected error uploading thumbnail: {e}")
            return None
    
    async def delete_thumbnail(self, recipe_id: str | UUID) -> bool:
        """
        Delete a thumbnail from S3.
        
        Args:
            recipe_id: Recipe ID
            
        Returns:
            True if deleted, False otherwise
        """
        if not self.is_enabled:
            return False
        
        try:
            # Try common extensions
            for ext in ["jpg", "png", "webp", "gif"]:
                s3_key = f"thumbnails/{recipe_id}.{ext}"
                try:
                    self.client.delete_object(
                        Bucket=self.bucket_name,
                        Key=s3_key,
                    )
                except ClientError:
                    continue
            
            print(f"🗑️ Thumbnail deleted for recipe: {recipe_id}")
            return True
            
        except Exception as e:
            print(f"❌ Failed to delete thumbnail: {e}")
            return False
    
    async def delete_prefix(self, prefix: str) -> int:
        """Delete all S3 objects under a prefix and return deleted count."""
        if not self.is_enabled:
            return 0

        deleted_count = 0
        continuation_token = None

        try:
            while True:
                kwargs = {"Bucket": self.bucket_name, "Prefix": prefix}
                if continuation_token:
                    kwargs["ContinuationToken"] = continuation_token

                response = self.client.list_objects_v2(**kwargs)
                objects = response.get("Contents", [])
                if objects:
                    delete_response = self.client.delete_objects(
                        Bucket=self.bucket_name,
                        Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                    )
                    errors = delete_response.get("Errors", [])
                    if errors:
                        print(
                            f"⚠️ Failed to delete {len(errors)} objects under S3 prefix {prefix}: "
                            f"{errors[:3]}"
                        )
                    deleted_count += len(delete_response.get("Deleted", []))

                if not response.get("IsTruncated"):
                    break
                continuation_token = response.get("NextContinuationToken")

            return deleted_count
        except Exception as e:
            print(f"❌ Failed to delete S3 prefix {prefix}: {e}")
            return deleted_count

    def get_thumbnail_url(self, recipe_id: str | UUID, extension: str = "jpg") -> str:
        """
        Get the S3 URL for a recipe's thumbnail.
        
        Args:
            recipe_id: Recipe ID
            extension: File extension
            
        Returns:
            S3 URL
        """
        settings = get_settings()
        return f"https://{self.bucket_name}.s3.{settings.aws_region}.amazonaws.com/thumbnails/{recipe_id}.{extension}"
    
    async def upload_thumbnail_from_bytes(
        self,
        image_data: bytes,
        recipe_id: str | UUID,
        content_type: str = "image/jpeg"
    ) -> Optional[str]:
        """
        Upload image bytes directly to S3.
        
        Args:
            image_data: Raw image bytes
            recipe_id: Recipe ID to use as filename
            content_type: MIME type of the image
            
        Returns:
            S3 URL if successful, None if failed or S3 not configured
        """
        if not self.is_enabled:
            print("⚠️ S3 not configured, skipping thumbnail upload")
            return None
        
        try:
            validated = validate_image_bytes(
                image_data,
                max_bytes=MAX_THUMBNAIL_BYTES,
                declared_content_type=content_type,
            )
            image_data = validated.data
            content_type = validated.content_type

            # Determine file extension from content type
            if "png" in content_type:
                extension = "png"
            elif "webp" in content_type:
                extension = "webp"
            elif "gif" in content_type:
                extension = "gif"
            else:
                extension = "jpg"
            
            # Upload to S3
            s3_key = f"thumbnails/{recipe_id}.{extension}"
            
            print(f"📤 Uploading to S3: {s3_key}")
            
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=image_data,
                ContentType=content_type,
            )
            
            # Generate public URL
            settings = get_settings()
            s3_url = f"https://{self.bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
            
            print(f"✅ Thumbnail uploaded: {s3_url}")
            return s3_url
            
        except ImageValidationError as e:
            print(f"❌ Invalid thumbnail image: {e}")
            return None
        except ClientError as e:
            print(f"❌ Failed to upload to S3: {e}")
            return None
        except Exception as e:
            print(f"❌ Unexpected error uploading thumbnail: {e}")
            return None

    async def upload_chat_image(
        self,
        image_base64: str,
        user_id: str,
    ) -> Optional[str]:
        """
        Upload a base64 chat image to S3.
        
        Chat images are stored with the pattern: chat-images/{user_id}/{hash}.jpg
        This allows images to persist across sessions and be re-sent in chat history.
        
        Args:
            image_base64: Base64 encoded image data
            user_id: User ID for organizing images
            
        Returns:
            S3 URL if successful, None if failed or S3 not configured
        """
        if not self.is_enabled:
            print("⚠️ S3 not configured, skipping chat image upload")
            return None
        
        if not image_base64:
            return None
        
        try:
            validated = decode_and_validate_base64_image(
                image_base64,
                max_bytes=MAX_CHAT_IMAGE_BYTES,
            )
            image_data = validated.data
            
            # Generate a hash-based filename for deduplication
            image_hash = hashlib.sha256(image_data).hexdigest()[:12]
            
            # Determine content type from base64 prefix
            content_type = validated.content_type
            extension = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/gif": "gif",
                "image/webp": "webp",
            }[content_type]
            
            # Upload to S3 under chat-images folder
            s3_key = f"chat-images/{user_id}/{image_hash}.{extension}"
            
            print(f"📤 Uploading chat image to S3: {s3_key}")
            
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=image_data,
                ContentType=content_type,
            )
            
            # Generate public URL
            settings = get_settings()
            s3_url = f"https://{self.bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
            
            print(f"✅ Chat image uploaded: {s3_url}")
            return s3_url
            
        except Exception as e:
            print(f"❌ Failed to upload chat image to S3: {e}")
            return None

    def is_owned_chat_image_url(self, image_url: str, user_id: str) -> bool:
        """Return whether a public URL points to this user's app-owned chat object."""
        if not self.bucket_name:
            return False

        parsed = urlparse(image_url)
        settings = get_settings()
        expected_host = f"{self.bucket_name}.s3.{settings.aws_region}.amazonaws.com"
        expected_prefix = f"/chat-images/{user_id}/"
        return (
            parsed.scheme == "https"
            and parsed.hostname == expected_host
            and parsed.path.startswith(expected_prefix)
            and ".." not in parsed.path
            and not parsed.query
            and not parsed.fragment
        )


# Singleton instance
storage_service = StorageService()
