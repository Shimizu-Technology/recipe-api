"""Main recipe extraction orchestrator service."""

from dataclasses import dataclass
from typing import Optional

import sentry_sdk

from app.services.video import video_service, VideoMetadata
from app.services.openai_client import openai_service  # Still used for Whisper
from app.services.llm_client import llm_service  # New: Gemini + GPT fallback


@dataclass
class ExtractionProgress:
    """Progress update during extraction."""
    step: str
    progress: int  # 0-100
    message: str


@dataclass
class FullExtractionResult:
    """Complete result of recipe extraction."""
    success: bool
    recipe: Optional[dict] = None
    raw_text: Optional[str] = None
    thumbnail_url: Optional[str] = None
    extraction_method: str = "oembed"
    extraction_quality: str = "low"
    has_audio_transcript: bool = False
    error: Optional[str] = None


class RecipeExtractor:
    """
    Main extraction orchestrator.
    
    Coordinates video download, transcription, and recipe extraction.
    """
    
    async def extract(
        self,
        url: str,
        location: str = "Guam",
        notes: str = "",
        progress_callback=None,
        fast_mode: bool = False
    ) -> FullExtractionResult:
        """
        Extract a recipe from a video URL.
        
        Args:
            url: Video URL (TikTok, YouTube, or Instagram)
            location: Location for cost estimation
            notes: Additional user-provided notes
            progress_callback: Optional async callback for progress updates
            fast_mode: If True, skip audio download and use metadata only (faster for re-extraction)
            
        Returns:
            FullExtractionResult with recipe data
        """
        print(f"🚀 Starting extraction for: {url}")
        print(f"📍 Location: {location}")
        
        # Track extraction metadata
        extraction_method = "oembed"
        extraction_quality = "low"
        has_audio_transcript = False
        thumbnail_url = None
        
        # Step 1: Detect platform
        platform = video_service.detect_platform(url)
        print(f"📱 Detected platform: {platform}")
        
        if progress_callback:
            await progress_callback(ExtractionProgress(
                step="detecting",
                progress=10,
                message=f"Detected {platform} video"
            ))
        
        # Step 2: Get video metadata via oEmbed
        if progress_callback:
            await progress_callback(ExtractionProgress(
                step="metadata",
                progress=20,
                message="Fetching video metadata..."
            ))
        
        metadata = await video_service.fetch_oembed(url, platform)
        thumbnail_url = metadata.thumbnail
        
        # For Instagram (or if oEmbed fails), fetch metadata from yt-dlp
        # oEmbed for Instagram requires a Facebook Graph API token which we may not have
        # yt-dlp also provides richer descriptions for Instagram
        ytdlp_metadata = None
        if platform == "instagram" or not thumbnail_url or not metadata.description:
            print(f"📷 Fetching metadata from yt-dlp for {platform}...")
            ytdlp_metadata = await video_service.get_video_metadata_ytdlp(url)
            
            if ytdlp_metadata.thumbnail and not thumbnail_url:
                thumbnail_url = ytdlp_metadata.thumbnail
                print(f"✅ Got thumbnail from yt-dlp: {thumbnail_url[:80]}...")
            
            # Use yt-dlp metadata if oEmbed didn't provide it (common for Instagram)
            if ytdlp_metadata.title and not metadata.title:
                metadata = VideoMetadata(
                    title=ytdlp_metadata.title,
                    description=ytdlp_metadata.description,
                    thumbnail=thumbnail_url,
                    uploader=ytdlp_metadata.uploader or metadata.uploader
                )
            elif ytdlp_metadata.description and not metadata.description:
                # Keep oEmbed title but use yt-dlp description
                metadata = VideoMetadata(
                    title=metadata.title,
                    description=ytdlp_metadata.description,
                    thumbnail=thumbnail_url,
                    uploader=ytdlp_metadata.uploader or metadata.uploader
                )
        
        # Step 3: Try to download audio and transcribe with Whisper
        # Skip audio download in fast_mode (used for re-extraction)
        combined_content = ""
        audio_file_path = None
        
        if fast_mode:
            print("⚡ Fast mode: skipping audio download, using metadata only")
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="metadata_fast",
                    progress=35,
                    message="Using fast metadata extraction..."
                ))
        elif platform in ["youtube", "tiktok", "instagram"]:
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="downloading",
                    progress=30,
                    message="Downloading audio..."
                ))
            
            # Download audio
            audio_result = await video_service.download_audio(url)
            
            if audio_result.success and audio_result.file_path:
                audio_file_path = audio_result.file_path
                
                if progress_callback:
                    await progress_callback(ExtractionProgress(
                        step="transcribing",
                        progress=50,
                        message="Transcribing audio with Whisper..."
                    ))
                
                # Transcribe with Whisper
                transcription = await openai_service.transcribe_audio(audio_file_path)
                
                if transcription.success and transcription.text:
                    print(f"✅ Whisper transcription: {len(transcription.text)} chars")
                    
                    # Build combined content
                    content_parts = []
                    if metadata.title:
                        content_parts.append(f"VIDEO TITLE: {metadata.title}")
                    if metadata.description:
                        content_parts.append(f"VIDEO DESCRIPTION: {metadata.description}")
                    content_parts.append(f"SPOKEN CONTENT (from audio):\n{transcription.text}")
                    
                    combined_content = "\n\n".join(content_parts)
                    extraction_method = "whisper"
                    extraction_quality = "high"
                    has_audio_transcript = True
                else:
                    print(f"⚠️ Whisper failed: {transcription.error}")
            else:
                print(f"⚠️ Audio download failed: {audio_result.error}")
                
                # Check for Instagram-specific auth error
                if audio_result.error == "INSTAGRAM_AUTH_REQUIRED":
                    # Don't give up yet - try metadata-only, but flag it
                    print("📝 Instagram requires login - trying metadata-only extraction")
        
        # Fallback: Use metadata-only if Whisper failed
        if not combined_content:
            print("📝 Falling back to metadata-only extraction")
            
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="metadata_fallback",
                    progress=40,
                    message="Using video metadata..."
                ))
            
            # Use ytdlp_metadata if we already fetched it, otherwise fetch now
            if not ytdlp_metadata:
                ytdlp_metadata = await video_service.get_video_metadata_ytdlp(url)
            
            # Prefer yt-dlp data over oEmbed for fallback
            title = ytdlp_metadata.title if ytdlp_metadata.title else metadata.title
            description = ytdlp_metadata.description if ytdlp_metadata.description else metadata.description
            
            if title or description:
                content_parts = []
                if title:
                    content_parts.append(f"VIDEO TITLE: {title}")
                if description:
                    content_parts.append(f"VIDEO DESCRIPTION: {description}")
                combined_content = "\n\n".join(content_parts)
                extraction_method = "basic"
                extraction_quality = "medium" if description else "low"
                
                # Update thumbnail if we got one from yt-dlp
                if ytdlp_metadata.thumbnail and not thumbnail_url:
                    thumbnail_url = ytdlp_metadata.thumbnail
            else:
                # Last resort: use oEmbed title
                combined_content = f"VIDEO TITLE: {metadata.title}" if metadata.title else ""
                extraction_method = "oembed"
                extraction_quality = "low"
        
        # Add user notes if provided
        if notes:
            combined_content = f"{combined_content}\n\nADDITIONAL NOTES FROM USER:\n{notes}"
        
        # Clean up audio file
        if audio_file_path:
            video_service.cleanup_audio_file(audio_file_path)
        
        # Step 4: Extract recipe with LLM (Gemini primary, GPT fallback)
        if not combined_content.strip():
            # Provide platform-specific error messages
            if platform == "instagram":
                # Report Instagram auth failure to Sentry for monitoring
                sentry_sdk.capture_message(
                    "Instagram extraction failed - auth required",
                    level="warning",
                    extras={
                        "url": url,
                        "platform": platform,
                        "has_cookies": video_service._get_instagram_cookies_path() is not None,
                    },
                    tags={
                        "platform": "instagram",
                        "error_type": "auth_required",
                    }
                )
                print("⚠️ Instagram auth failure reported to Sentry")
                
                return FullExtractionResult(
                    success=False,
                    error="Instagram requires login to access this content. Try one of these alternatives:\n\n"
                          "• Use Photo Scan to capture the recipe from a screenshot\n"
                          "• Try a TikTok or YouTube video instead\n"
                          "• If the recipe is in the caption, copy it to the Notes field and try again"
                )
            return FullExtractionResult(
                success=False,
                error="No content could be extracted from the video"
            )
        
        if progress_callback:
            await progress_callback(ExtractionProgress(
                step="extracting",
                progress=70,
                message="Extracting recipe with AI..."
            ))
        
        extraction_result = await llm_service.extract_recipe(
            source_url=url,
            content=combined_content,
            location=location
        )
        
        if not extraction_result.success:
            return FullExtractionResult(
                success=False,
                error=extraction_result.error
            )
        
        # Add thumbnail to recipe
        recipe = extraction_result.recipe
        if recipe:
            recipe["media"] = {"thumbnail": thumbnail_url}
        
        # Note: Don't send "complete" here - let the router handle that
        # after S3 upload is done to avoid progress going backwards
        
        return FullExtractionResult(
            success=True,
            recipe=recipe,
            raw_text=combined_content,
            thumbnail_url=thumbnail_url,
            extraction_method=extraction_method,
            extraction_quality=extraction_quality,
            has_audio_transcript=has_audio_transcript
        )


# Singleton instance
recipe_extractor = RecipeExtractor()

