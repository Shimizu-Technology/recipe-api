"""Video processing service using yt-dlp for audio extraction."""

import os
import re
import asyncio
import tempfile
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import httpx

from app.config import get_settings

settings = get_settings()


# ============================================================
# Friendly Error Messages for Video Extraction
# ============================================================

# Maps yt-dlp error patterns to user-friendly messages
VIDEO_ERROR_PATTERNS = {
    # Video unavailable/deleted
    "video unavailable": {
        "code": "VIDEO_UNAVAILABLE",
        "message": "This video is no longer available. It may have been deleted by the creator.",
    },
    "this video has been removed": {
        "code": "VIDEO_REMOVED",
        "message": "This video has been removed by the creator or the platform.",
    },
    "video is private": {
        "code": "VIDEO_PRIVATE",
        "message": "This video is private. Only the creator can view it.",
    },
    "private video": {
        "code": "VIDEO_PRIVATE",
        "message": "This video is private. Only the creator can view it.",
    },
    "sign in to confirm your age": {
        "code": "AGE_RESTRICTED",
        "message": "This video is age-restricted and cannot be extracted.",
    },
    "age-restricted": {
        "code": "AGE_RESTRICTED",
        "message": "This video is age-restricted and cannot be extracted.",
    },
    # Instagram-specific
    "login required": {
        "code": "INSTAGRAM_AUTH_REQUIRED",
        "message": "Instagram requires authentication. Please try again later.",
    },
    "rate-limit": {
        "code": "RATE_LIMITED",
        "message": "Too many requests. Please wait a few minutes and try again.",
    },
    # TikTok-specific
    "couldn't find this account": {
        "code": "ACCOUNT_NOT_FOUND",
        "message": "This TikTok account no longer exists or has been banned.",
    },
    "video is currently unavailable": {
        "code": "VIDEO_UNAVAILABLE",
        "message": "This video is currently unavailable. It may be under review or deleted.",
    },
    # YouTube-specific
    "video has been removed by the uploader": {
        "code": "VIDEO_REMOVED",
        "message": "This video has been removed by the uploader.",
    },
    "video has been removed for violating": {
        "code": "VIDEO_REMOVED",
        "message": "This video has been removed for violating platform guidelines.",
    },
    "this video is not available": {
        "code": "VIDEO_UNAVAILABLE",
        "message": "This video is not available. It may be region-restricted or deleted.",
    },
    "join this channel to get access": {
        "code": "MEMBERS_ONLY",
        "message": "This video is only available to channel members.",
    },
    # Generic errors
    "unable to extract": {
        "code": "EXTRACTION_FAILED",
        "message": "We couldn't extract this video. It may be in an unsupported format.",
    },
    "http error 404": {
        "code": "NOT_FOUND",
        "message": "This video doesn't exist or the link is broken.",
    },
    "http error 403": {
        "code": "ACCESS_DENIED",
        "message": "Access to this video is denied. It may be private or region-restricted.",
    },
}


def get_friendly_video_error(raw_error: str, platform: str = "video") -> tuple[str, str]:
    """
    Parse a raw yt-dlp error and return a friendly error message.
    
    Args:
        raw_error: The raw error message from yt-dlp
        platform: The video platform (youtube, tiktok, instagram)
        
    Returns:
        Tuple of (error_code, friendly_message)
    """
    error_lower = raw_error.lower()
    
    for pattern, error_info in VIDEO_ERROR_PATTERNS.items():
        if pattern in error_lower:
            return error_info["code"], error_info["message"]
    
    # Default fallback based on platform
    if platform == "instagram":
        return "INSTAGRAM_ERROR", f"We couldn't access this Instagram video. It may be private, deleted, or temporarily unavailable."
    elif platform == "tiktok":
        return "TIKTOK_ERROR", f"We couldn't access this TikTok video. It may be private, deleted, or temporarily unavailable."
    elif platform == "youtube":
        return "YOUTUBE_ERROR", f"We couldn't access this YouTube video. It may be private, deleted, or region-restricted."
    else:
        return "UNKNOWN_ERROR", f"We couldn't process this video. Please check the link and try again."


@dataclass
class VideoMetadata:
    """Metadata extracted from video platforms."""
    title: str = ""
    description: str = ""
    thumbnail: Optional[str] = None
    duration: int = 0
    uploader: str = ""


@dataclass
class AudioExtractionResult:
    """Result of audio extraction from video."""
    success: bool
    file_path: Optional[str] = None
    duration: Optional[float] = None
    error: Optional[str] = None
    error_code: Optional[str] = None  # Machine-readable error code
    friendly_error: Optional[str] = None  # User-friendly error message


class VideoService:
    """Service for extracting audio and metadata from video platforms."""
    
    SUPPORTED_PLATFORMS = ["youtube", "tiktok", "instagram"]
    
    @staticmethod
    def detect_platform(url: str) -> str:
        """Detect video platform from URL."""
        url_lower = url.lower()
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            return "youtube"
        elif "tiktok.com" in url_lower:
            return "tiktok"
        elif "instagram.com" in url_lower:
            return "instagram"
        return "web"
    
    @staticmethod
    async def normalize_url(url: str) -> str:
        """
        Normalize a video URL to a canonical form for duplicate detection.
        
        TikTok short URLs (tiktok.com/t/xxxxx) redirect to different URLs each time
        they're shared, so we need to resolve them to the full video URL.
        
        YouTube and Instagram URLs are generally stable.
        """
        url = url.strip()
        platform = VideoService.detect_platform(url)
        
        if platform == "tiktok":
            # TikTok short URLs need to be resolved
            if "/t/" in url or "vm.tiktok.com" in url:
                try:
                    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                        response = await client.head(url)
                        resolved_url = str(response.url)
                        print(f"🔗 Resolved TikTok URL: {url} → {resolved_url}")
                        
                        # Clean up query params but keep the full path with username
                        # e.g., https://www.tiktok.com/@user/video/123?_r=1 -> https://www.tiktok.com/@user/video/123
                        if '?' in resolved_url:
                            resolved_url = resolved_url.split('?')[0]
                        
                        return resolved_url
                except Exception as e:
                    print(f"⚠️ Failed to resolve TikTok URL: {e}")
                    return url
            else:
                # Already a full URL, just clean up query params
                if '?' in url:
                    url = url.split('?')[0]
                return url
        
        elif platform == "youtube":
            # Normalize YouTube URLs to a standard format
            video_id = VideoService.extract_youtube_id(url)
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
        
        # For other platforms, return as-is
        return url
    
    @staticmethod
    def extract_tiktok_video_id(url: str) -> Optional[str]:
        """Extract video ID from a TikTok URL for duplicate matching."""
        video_id_match = re.search(r'/video/(\d+)', url)
        if video_id_match:
            return video_id_match.group(1)
        return None
    
    @staticmethod
    def extract_youtube_id(url: str) -> Optional[str]:
        """Extract video ID from YouTube URL."""
        patterns = [
            r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/shorts\/)([^&\n?#]+)',
            r'^([a-zA-Z0-9_-]{11})$'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None
    
    async def fetch_oembed(self, url: str, platform: str) -> VideoMetadata:
        """Fetch oEmbed metadata from platform."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if platform == "youtube":
                    endpoint = f"https://www.youtube.com/oembed?format=json&url={url}"
                    response = await client.get(endpoint)
                    if response.status_code == 200:
                        data = response.json()
                        return VideoMetadata(
                            title=data.get("title", ""),
                            thumbnail=data.get("thumbnail_url"),
                            uploader=data.get("author_name", "")
                        )
                
                elif platform == "tiktok":
                    endpoint = f"https://www.tiktok.com/oembed?url={url}"
                    response = await client.get(endpoint)
                    if response.status_code == 200:
                        data = response.json()
                        return VideoMetadata(
                            title=data.get("title", ""),
                            thumbnail=data.get("thumbnail_url"),
                            uploader=data.get("author_name", "")
                        )
                
                elif platform == "instagram":
                    token = settings.ig_oembed_token
                    if not token:
                        print("⚠️ Instagram oEmbed token not configured")
                        return VideoMetadata()
                    endpoint = f"https://graph.facebook.com/v17.0/instagram_oembed?url={url}&access_token={token}"
                    response = await client.get(endpoint)
                    if response.status_code == 200:
                        data = response.json()
                        return VideoMetadata(
                            title=data.get("title", ""),
                            thumbnail=data.get("thumbnail_url"),
                            uploader=data.get("author_name", "")
                        )
        
        except Exception as e:
            print(f"❌ oEmbed fetch failed for {platform}: {e}")
        
        return VideoMetadata()
    
    def _get_instagram_cookies_path(self) -> Optional[str]:
        """
        Get path to Instagram cookies file.
        
        If INSTAGRAM_COOKIES env var contains cookie content (starts with '# Netscape'),
        write it to a temp file and return the path.
        If it's a file path, return it directly.
        """
        cookies = settings.instagram_cookies
        if not cookies:
            return None
        
        # Check if it's raw cookie content vs a file path
        if cookies.strip().startswith("# Netscape") or cookies.strip().startswith("#HttpOnly"):
            # It's cookie content - write to temp file
            cookies_path = "/tmp/instagram_cookies.txt"
            try:
                with open(cookies_path, "w") as f:
                    f.write(cookies)
                return cookies_path
            except Exception as e:
                print(f"⚠️ Failed to write Instagram cookies: {e}")
                return None
        else:
            # It's a file path
            if os.path.exists(cookies):
                return cookies
            else:
                print(f"⚠️ Instagram cookies file not found: {cookies}")
                return None

    async def download_audio(self, url: str) -> AudioExtractionResult:
        """
        Download audio from video using yt-dlp.
        
        Returns the path to the downloaded audio file.
        """
        print(f"📥 Downloading audio from: {url}")
        
        # Create a temp directory for the audio file
        temp_dir = tempfile.mkdtemp(prefix="recipe-audio-")
        output_template = os.path.join(temp_dir, "audio.%(ext)s")
        
        # Detect platform for Instagram-specific handling
        platform = self.detect_platform(url)
        
        try:
            # Build yt-dlp command
            command = [
                "yt-dlp",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--output", output_template,
                "--no-playlist",
                "--quiet",
                url
            ]
            
            # Add cookies for Instagram if configured
            if platform == "instagram":
                cookies_path = self._get_instagram_cookies_path()
                if cookies_path:
                    command.insert(-1, "--cookies")  # Insert before URL
                    command.insert(-1, cookies_path)
                    print(f"🍪 Using Instagram cookies from: {cookies_path}")
                else:
                    print("⚠️ Instagram extraction may fail without cookies")
            
            print(f"🎵 Executing: {' '.join(command)}")
            
            # Run yt-dlp asynchronously
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=120  # 2 minute timeout
            )
            
            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                print(f"❌ yt-dlp failed: {error_msg}")
                
                # Get friendly error message
                error_code, friendly_error = get_friendly_video_error(error_msg, platform)
                print(f"📝 Error code: {error_code}, Message: {friendly_error}")
                
                return AudioExtractionResult(
                    success=False,
                    error=error_msg,  # Keep raw error for logging
                    error_code=error_code,
                    friendly_error=friendly_error
                )
            
            # Find the downloaded audio file
            audio_files = list(Path(temp_dir).glob("audio.*"))
            if not audio_files:
                return AudioExtractionResult(
                    success=False,
                    error="No audio file found after download",
                    error_code="NO_AUDIO",
                    friendly_error="We couldn't extract audio from this video. It may not contain audio."
                )
            
            audio_file = str(audio_files[0])
            print(f"✅ Audio downloaded: {audio_file}")
            
            # Try to get duration using ffprobe
            duration = await self._get_audio_duration(audio_file)
            
            return AudioExtractionResult(
                success=True,
                file_path=audio_file,
                duration=duration
            )
            
        except asyncio.TimeoutError:
            return AudioExtractionResult(
                success=False,
                error="Audio download timed out after 120 seconds",
                error_code="TIMEOUT",
                friendly_error="The video took too long to download. Please try again later."
            )
        except FileNotFoundError:
            return AudioExtractionResult(
                success=False,
                error="yt-dlp not found",
                error_code="SYSTEM_ERROR",
                friendly_error="A system error occurred. Please try again later."
            )
        except Exception as e:
            return AudioExtractionResult(
                success=False,
                error=str(e),
                error_code="UNKNOWN_ERROR",
                friendly_error="An unexpected error occurred. Please try again."
            )
    
    async def _get_audio_duration(self, file_path: str) -> Optional[float]:
        """Get audio duration using ffprobe."""
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            if stdout:
                return float(stdout.decode().strip())
        except Exception as e:
            print(f"⚠️ Could not get audio duration: {e}")
        return None
    
    async def get_video_metadata_ytdlp(self, url: str) -> VideoMetadata:
        """Get video metadata using yt-dlp (no download)."""
        platform = self.detect_platform(url)
        
        try:
            command = [
                "yt-dlp",
                "--dump-json",
                "--no-download",
                "--quiet",
                url,
            ]
            
            # Add cookies for Instagram if configured
            if platform == "instagram":
                cookies_path = self._get_instagram_cookies_path()
                if cookies_path:
                    command.insert(-1, "--cookies")
                    command.insert(-1, cookies_path)
            
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30
            )
            
            if process.returncode == 0 and stdout:
                import json
                data = json.loads(stdout.decode())
                return VideoMetadata(
                    title=data.get("title", ""),
                    description=data.get("description", ""),
                    thumbnail=data.get("thumbnail"),
                    duration=data.get("duration", 0),
                    uploader=data.get("uploader", "")
                )
        except Exception as e:
            print(f"⚠️ yt-dlp metadata extraction failed: {e}")
        
        return VideoMetadata()
    
    @staticmethod
    def cleanup_audio_file(file_path: str) -> None:
        """Clean up temporary audio file and its directory."""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                # Also remove the temp directory
                temp_dir = os.path.dirname(file_path)
                if temp_dir and os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
                print(f"🗑️ Cleaned up temp audio file: {file_path}")
        except Exception as e:
            print(f"⚠️ Failed to clean up temp file: {e}")


# Singleton instance
video_service = VideoService()

