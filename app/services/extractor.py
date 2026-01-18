"""Main recipe extraction orchestrator service."""

from dataclasses import dataclass
from typing import Optional

import sentry_sdk

from app.services.video import video_service, VideoMetadata, VideoService
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
    error_code: Optional[str] = None  # Machine-readable error code
    friendly_error: Optional[str] = None  # User-friendly error message
    low_confidence: bool = False  # True if extraction quality is uncertain
    confidence_warning: Optional[str] = None  # Warning message for low confidence


def _check_extraction_confidence(
    recipe: Optional[dict],
    raw_text: str,
    extraction_quality: str,
    has_audio_transcript: bool
) -> tuple[bool, Optional[str]]:
    """
    Check if the extraction result is low confidence and needs user review.
    
    Returns:
        Tuple of (is_low_confidence, warning_message)
    """
    warnings = []
    
    # Check 1: No audio transcript (metadata-only extraction)
    if not has_audio_transcript:
        if extraction_quality == "low":
            warnings.append("extracted from limited metadata only")
    
    # Check 2: Very short content
    word_count = len(raw_text.split()) if raw_text else 0
    if word_count < 50:
        warnings.append("very little content was found")
    
    # Check 3: Detect if transcript is mostly music/non-recipe content
    # But SKIP this warning if the metadata contains detailed recipe info
    if raw_text:
        raw_lower = raw_text.lower()
        # Count music indicators (🎵, ♪, Ž symbols often appear in music transcripts)
        music_indicators = raw_text.count('🎵') + raw_text.count('♪') + raw_text.count('Ž')
        
        # Check for recipe-related words in the SPOKEN CONTENT section
        spoken_content = ""
        if "SPOKEN CONTENT" in raw_text:
            spoken_content = raw_text.split("SPOKEN CONTENT")[-1].lower()
        
        # Also check the metadata section (VIDEO TITLE, description, etc.)
        metadata_content = ""
        if "VIDEO TITLE" in raw_text:
            # Get everything from VIDEO TITLE to SPOKEN CONTENT (if present)
            metadata_start = raw_text.find("VIDEO TITLE")
            if "SPOKEN CONTENT" in raw_text:
                metadata_end = raw_text.find("SPOKEN CONTENT")
                metadata_content = raw_text[metadata_start:metadata_end].lower()
            else:
                metadata_content = raw_text[metadata_start:].lower()
        
        recipe_keywords = ["cup", "tablespoon", "teaspoon", "tbsp", "tsp", "ounce", "oz", 
                          "pound", "lb", "ingredient", "add", "mix", "stir", "cook", 
                          "bake", "fry", "boil", "simmer", "chop", "dice", "slice",
                          "minutes", "degrees", "oven", "pan", "pot", "bowl"]
        
        # Quantity keywords indicate detailed measurements in metadata
        quantity_keywords = ["cup", "tbsp", "tsp", "oz", "lb", "gram", "g ", "ml", 
                            "1/2", "1/4", "1/3", "3/4", "½", "¼", "¾"]
        
        recipe_word_count_spoken = sum(1 for kw in recipe_keywords if kw in spoken_content)
        recipe_word_count_metadata = sum(1 for kw in recipe_keywords if kw in metadata_content)
        quantity_count_metadata = sum(1 for kw in quantity_keywords if kw in metadata_content)
        
        # Metadata is considered "rich" if it has recipe keywords AND quantity indicators
        metadata_has_rich_recipe_info = recipe_word_count_metadata >= 3 and quantity_count_metadata >= 2
        
        # If there are music indicators and very few recipe words in BOTH audio and metadata, flag it
        if music_indicators >= 3 and recipe_word_count_spoken < 3 and not metadata_has_rich_recipe_info:
            warnings.append("the audio appears to be mostly music without recipe instructions")
        elif has_audio_transcript and recipe_word_count_spoken < 2 and len(spoken_content) > 100:
            # Only flag if metadata doesn't have rich recipe info to compensate
            if not metadata_has_rich_recipe_info:
                warnings.append("the audio didn't contain clear recipe instructions")
    
    # Check 4: Check recipe completeness
    if recipe:
        # Check for missing or empty ingredients
        components = recipe.get("components", [])
        total_ingredients = 0
        vague_ingredients = 0
        
        for comp in components:
            ingredients = comp.get("ingredients", [])
            total_ingredients += len(ingredients)
            
            # Check for vague/placeholder quantities
            for ing in ingredients:
                quantity = str(ing.get("quantity", "")).lower()
                unit = str(ing.get("unit", "")).lower()
                name = str(ing.get("name", "")).lower()
                notes = str(ing.get("notes", "")).lower()
                
                # Detect vague quantities that indicate AI is guessing
                vague_patterns = ["to taste", "optional", "your choice", "as needed", 
                                 "to your liking", "to preference", "adjust to"]
                if any(vp in quantity or vp in unit or vp in notes or vp in name for vp in vague_patterns):
                    vague_ingredients += 1
        
        if total_ingredients == 0:
            warnings.append("no ingredients could be identified")
        elif total_ingredients < 3:
            warnings.append("very few ingredients were found")
        elif vague_ingredients > 0 and vague_ingredients >= total_ingredients * 0.5:
            # More than half the ingredients are vague
            warnings.append("many ingredient quantities are unclear")
        
        # Check for missing steps
        total_steps = 0
        for comp in components:
            steps = comp.get("steps", [])
            total_steps += len(steps)
        
        if total_steps == 0:
            warnings.append("no cooking steps could be identified")
        
        # Check for generic/placeholder title
        title = recipe.get("title", "").lower()
        generic_titles = ["recipe", "dish", "food", "meal", "untitled", "unknown"]
        if any(title == generic for generic in generic_titles) or len(title) < 3:
            warnings.append("the recipe title may need to be updated")
    
    if warnings:
        warning_msg = "This recipe may need review: " + ", and ".join(warnings[:2]) + "."
        return True, warning_msg
    
    return False, None


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
        
        # Step 1.5: For TikTok, resolve short URLs first to detect photo posts correctly
        # Short URLs like /t/xxx don't contain /photo/ until resolved
        resolved_url = url
        if platform == "tiktok":
            if "/t/" in url or "vm.tiktok.com" in url:
                print(f"🔗 Resolving TikTok short URL before photo detection...")
                resolved_url = await VideoService.normalize_url(url)
            
            # Now check for photo posts with the resolved URL
            if VideoService.is_tiktok_photo_post(resolved_url):
                print(f"📸 Detected TikTok photo/slideshow post - using vision extraction")
                return await self._extract_from_tiktok_photo(
                    url=resolved_url,  # Use resolved URL
                    location=location,
                    notes=notes,
                    progress_callback=progress_callback
                )
        
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
        audio_error_code = None  # Track audio error for better error messages
        audio_friendly_error = None
        
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
                
                # Store error info for later use if metadata also fails
                audio_error_code = audio_result.error_code
                audio_friendly_error = audio_result.friendly_error
                
                # Check for Instagram-specific auth error
                if audio_result.error_code == "INSTAGRAM_AUTH_REQUIRED":
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
            # If we had an audio error with a specific cause, use that
            if audio_error_code and audio_friendly_error:
                # For certain error codes, the video is definitively unavailable
                if audio_error_code in ["VIDEO_UNAVAILABLE", "VIDEO_REMOVED", "VIDEO_PRIVATE", 
                                        "NOT_FOUND", "ACCOUNT_NOT_FOUND", "MEMBERS_ONLY"]:
                    return FullExtractionResult(
                        success=False,
                        error=audio_friendly_error,
                        error_code=audio_error_code,
                        friendly_error=audio_friendly_error
                    )
            
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
                          "• If the recipe is in the caption, copy it to the Notes field and try again",
                    error_code="INSTAGRAM_AUTH_REQUIRED",
                    friendly_error="This Instagram video requires login to access."
                )
            
            # Use friendly error if we have one, otherwise generic
            friendly_msg = audio_friendly_error or "We couldn't extract any content from this video. Please check the link and try again."
            return FullExtractionResult(
                success=False,
                error="No content could be extracted from the video",
                error_code=audio_error_code or "NO_CONTENT",
                friendly_error=friendly_msg
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
                error=extraction_result.error,
                error_code="LLM_EXTRACTION_FAILED",
                friendly_error="We couldn't extract a recipe from this video. The content may not contain a clear recipe."
            )
        
        # Add thumbnail to recipe
        recipe = extraction_result.recipe
        if recipe:
            recipe["media"] = {"thumbnail": thumbnail_url}
        
        # Check extraction confidence
        low_confidence, confidence_warning = _check_extraction_confidence(
            recipe=recipe,
            raw_text=combined_content,
            extraction_quality=extraction_quality,
            has_audio_transcript=has_audio_transcript
        )
        
        if low_confidence:
            print(f"⚠️ Low confidence extraction: {confidence_warning}")
        
        # Note: Don't send "complete" here - let the router handle that
        # after S3 upload is done to avoid progress going backwards
        
        return FullExtractionResult(
            success=True,
            recipe=recipe,
            raw_text=combined_content,
            thumbnail_url=thumbnail_url,
            extraction_method=extraction_method,
            extraction_quality=extraction_quality,
            has_audio_transcript=has_audio_transcript,
            low_confidence=low_confidence,
            confidence_warning=confidence_warning
        )
    
    async def _extract_from_tiktok_photo(
        self,
        url: str,
        location: str = "Guam",
        notes: str = "",
        progress_callback=None
    ) -> FullExtractionResult:
        """
        Extract recipe from a TikTok photo/slideshow post using vision AI.
        
        TikTok photo posts are image carousels without audio, so we use
        Gemini 2.0 Flash Vision to analyze the images directly.
        """
        print(f"📸 Starting TikTok photo extraction for: {url}")
        
        thumbnail_url = None
        
        try:
            # Step 1: Fetch images from the photo post
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="fetching_images",
                    progress=20,
                    message="Fetching slideshow images..."
                ))
            
            image_urls = await video_service.fetch_tiktok_photo_images(url)
            
            if not image_urls:
                print("❌ No images found in TikTok photo post")
                return FullExtractionResult(
                    success=False,
                    error="Could not extract images from this TikTok post. It may be private or unavailable.",
                    error_code="NO_IMAGES_FOUND",
                    friendly_error="We couldn't find any images in this TikTok post. Please try a different post."
                )
            
            # Use first image as thumbnail
            thumbnail_url = image_urls[0] if image_urls else None
            print(f"✅ Found {len(image_urls)} images, thumbnail: {thumbnail_url[:80] if thumbnail_url else 'None'}...")
            
            # Step 2: Download images as base64
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="downloading_images",
                    progress=35,
                    message=f"Downloading {len(image_urls)} images..."
                ))
            
            base64_images = await video_service.download_images_as_base64(image_urls)
            
            if not base64_images:
                print("❌ Failed to download any images")
                return FullExtractionResult(
                    success=False,
                    error="Could not download images from this TikTok post.",
                    error_code="IMAGE_DOWNLOAD_FAILED",
                    friendly_error="We couldn't download the images from this post. Please try again later."
                )
            
            print(f"✅ Downloaded {len(base64_images)} images as base64")
            
            # Step 3: Use vision AI to extract recipe from images
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="analyzing",
                    progress=50,
                    message="Analyzing images with AI..."
                ))
            
            # Use TikTok slideshow extraction (visual analysis prompt)
            # TikTok slideshows show recipes visually with minimal text,
            # so we use a specialized prompt that emphasizes visual analysis
            result = await llm_service.extract_from_tiktok_slideshow(
                images_base64=base64_images,
                location=location
            )
            
            if not result.success:
                print(f"❌ Vision extraction failed: {result.error}")
                return FullExtractionResult(
                    success=False,
                    error=result.error or "Failed to extract recipe from images",
                    error_code="VISION_EXTRACTION_FAILED",
                    friendly_error="We couldn't find a recipe in these images. Make sure the post contains recipe information."
                )
            
            # Step 4: Parse the extracted recipe
            if progress_callback:
                await progress_callback(ExtractionProgress(
                    step="extracting",
                    progress=80,
                    message="Extracting recipe details..."
                ))
            
            recipe = result.recipe
            
            # Add source URL to the recipe
            if recipe:
                recipe["source_url"] = url
            
            print(f"✅ TikTok photo extraction successful: {recipe.get('title', 'Untitled')}")
            
            # Check confidence
            raw_text = f"[TikTok Photo Post - {len(base64_images)} images analyzed]"
            low_confidence, confidence_warning = _check_extraction_confidence(
                recipe=recipe,
                raw_text=raw_text,
                extraction_quality="good",  # Vision extraction is typically good
                has_audio_transcript=False
            )
            
            return FullExtractionResult(
                success=True,
                recipe=recipe,
                raw_text=raw_text,
                thumbnail_url=thumbnail_url,
                extraction_method="tiktok_photo_vision",
                extraction_quality="good",
                has_audio_transcript=False,
                low_confidence=low_confidence,
                confidence_warning=confidence_warning
            )
            
        except Exception as e:
            print(f"❌ TikTok photo extraction error: {e}")
            import traceback
            traceback.print_exc()
            
            # Capture in Sentry
            sentry_sdk.capture_exception(e)
            
            return FullExtractionResult(
                success=False,
                error=str(e),
                error_code="TIKTOK_PHOTO_ERROR",
                friendly_error="Something went wrong while processing this TikTok photo post. Please try again.",
                thumbnail_url=thumbnail_url
            )


# Singleton instance
recipe_extractor = RecipeExtractor()

