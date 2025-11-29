"""Main recipe extraction orchestrator service."""

from dataclasses import dataclass
from typing import Optional

from app.services.video import video_service, VideoMetadata
from app.services.openai_client import openai_service


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
        progress_callback=None
    ) -> FullExtractionResult:
        """
        Extract a recipe from a video URL.
        
        Args:
            url: Video URL (TikTok, YouTube, or Instagram)
            location: Location for cost estimation
            notes: Additional user-provided notes
            progress_callback: Optional async callback for progress updates
            
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
        
        # Step 3: Try to download audio and transcribe with Whisper
        combined_content = ""
        audio_file_path = None
        
        if platform in ["youtube", "tiktok", "instagram"]:
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
        
        # Fallback: Use yt-dlp metadata if Whisper failed
        if not combined_content:
            print("📝 Falling back to metadata-only extraction")
            
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="metadata_fallback",
                    progress=40,
                    message="Using video metadata..."
                ))
            
            # Try to get richer metadata from yt-dlp
            ytdlp_metadata = await video_service.get_video_metadata_ytdlp(url)
            
            if ytdlp_metadata.title or ytdlp_metadata.description:
                content_parts = []
                if ytdlp_metadata.title:
                    content_parts.append(f"VIDEO TITLE: {ytdlp_metadata.title}")
                if ytdlp_metadata.description:
                    content_parts.append(f"VIDEO DESCRIPTION: {ytdlp_metadata.description}")
                combined_content = "\n\n".join(content_parts)
                extraction_method = "basic"
                extraction_quality = "medium" if ytdlp_metadata.description else "low"
                
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
        
        # Step 4: Extract recipe with GPT
        if not combined_content.strip():
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
        
        extraction_result = await openai_service.extract_recipe(
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

