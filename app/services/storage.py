"""S3 storage service for persisting recipe thumbnails."""

import httpx
import boto3
from botocore.exceptions import ClientError
from typing import Optional
from uuid import UUID
import hashlib
from io import BytesIO

from app.config import get_settings


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
            
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                image_data = response.content
                content_type = response.headers.get("content-type", "image/jpeg")
            
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


# Singleton instance
storage_service = StorageService()

