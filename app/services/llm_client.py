"""LLM service for recipe extraction with Gemini (primary) and GPT fallback."""

import re
import json
import asyncio
from dataclasses import dataclass
from typing import Optional
import httpx

from app.config import get_settings
from app.services.prompts import get_recipe_extraction_prompt, get_ocr_extraction_prompt, get_multi_image_ocr_prompt

settings = get_settings()


@dataclass
class ExtractionResult:
    """Result of LLM recipe extraction."""
    success: bool
    recipe: Optional[dict] = None
    error: Optional[str] = None
    model_used: Optional[str] = None
    latency_seconds: Optional[float] = None


class LLMService:
    """
    Service for LLM-based recipe extraction.
    
    Primary: Gemini 2.0 Flash via OpenRouter (fast, cheap)
    Fallback: GPT-4o-mini via OpenAI (reliable)
    """
    
    SYSTEM_PROMPT = "You are a culinary extraction engine. Extract recipe information and return valid JSON only."
    
    # Model configurations
    GEMINI_CONFIG = {
        "name": "Gemini 2.0 Flash",
        "model": "google/gemini-2.0-flash-001",
        "base_url": "https://openrouter.ai/api/v1",
        "timeout": 60,
        "max_retries": 2,
    }
    
    GPT_CONFIG = {
        "name": "GPT-4o-mini",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "timeout": 120,
        "max_retries": 1,
    }
    
    # Vision model configurations for OCR
    GEMINI_VISION_CONFIG = {
        "name": "Gemini 2.0 Flash (Vision)",
        "model": "google/gemini-2.0-flash-001",
        "base_url": "https://openrouter.ai/api/v1",
        "timeout": 90,
        "max_retries": 2,
    }
    
    GPT_VISION_CONFIG = {
        "name": "GPT-4o (Vision)",
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
        "timeout": 120,
        "max_retries": 1,
    }
    
    def __init__(self):
        self.openrouter_api_key = settings.openrouter_api_key
        self.openai_api_key = settings.openai_api_key
        
    async def extract_recipe(
        self,
        source_url: str,
        content: str,
        location: str = "Guam",
        use_fallback: bool = True
    ) -> ExtractionResult:
        """
        Extract structured recipe data using LLM.
        
        Tries Gemini first (faster, cheaper), falls back to GPT if needed.
        
        Args:
            source_url: Original video URL
            content: Combined text content (title + description + transcript)
            location: Location for cost estimation
            use_fallback: Whether to fall back to GPT if Gemini fails
            
        Returns:
            ExtractionResult with recipe dict or error
        """
        print(f"🤖 Extracting recipe...")
        print(f"📍 Location: {location}")
        print(f"📝 Content length: {len(content)} chars")
        
        # Sanitize content
        sanitized_content = self._sanitize_text(content)
        
        # Generate prompt
        prompt = get_recipe_extraction_prompt(source_url, sanitized_content, location)
        
        # Try Gemini first (if API key available)
        if self.openrouter_api_key:
            print(f"🚀 Trying {self.GEMINI_CONFIG['name']}...")
            result = await self._try_extraction(
                config=self.GEMINI_CONFIG,
                api_key=self.openrouter_api_key,
                prompt=prompt,
                source_url=source_url,
                location=location,
                is_openrouter=True
            )
            
            if result.success:
                return result
            else:
                print(f"⚠️ {self.GEMINI_CONFIG['name']} failed: {result.error}")
        
        # Fall back to GPT
        if use_fallback and self.openai_api_key:
            print(f"🔄 Falling back to {self.GPT_CONFIG['name']}...")
            result = await self._try_extraction(
                config=self.GPT_CONFIG,
                api_key=self.openai_api_key,
                prompt=prompt,
                source_url=source_url,
                location=location,
                is_openrouter=False
            )
            
            if result.success:
                return result
            else:
                print(f"❌ {self.GPT_CONFIG['name']} also failed: {result.error}")
        
        # Both failed
        return ExtractionResult(
            success=False,
            error="All extraction attempts failed"
        )
    
    async def extract_from_image(
        self,
        image_base64: str,
        location: str = "Guam",
        use_fallback: bool = True
    ) -> ExtractionResult:
        """
        Extract recipe from an image (OCR) using vision models.
        
        Tries Gemini Vision first (faster, cheaper), falls back to GPT-4o Vision if needed.
        
        Args:
            image_base64: Base64 encoded image data
            location: Location for cost estimation
            use_fallback: Whether to fall back to GPT-4o if Gemini fails
            
        Returns:
            ExtractionResult with recipe dict or error
        """
        print(f"📸 Extracting recipe from image...")
        print(f"📍 Location: {location}")
        print(f"🖼️ Image size: {len(image_base64) // 1024}KB (base64)")
        
        # Generate OCR prompt
        prompt = get_ocr_extraction_prompt(location)
        
        # Try Gemini Vision first (if API key available)
        if self.openrouter_api_key:
            print(f"🚀 Trying {self.GEMINI_VISION_CONFIG['name']}...")
            result = await self._try_vision_extraction(
                config=self.GEMINI_VISION_CONFIG,
                api_key=self.openrouter_api_key,
                prompt=prompt,
                image_base64=image_base64,
                location=location,
                is_openrouter=True
            )
            
            if result.success:
                return result
            else:
                print(f"⚠️ {self.GEMINI_VISION_CONFIG['name']} failed: {result.error}")
        
        # Fall back to GPT-4o Vision
        if use_fallback and self.openai_api_key:
            print(f"🔄 Falling back to {self.GPT_VISION_CONFIG['name']}...")
            result = await self._try_vision_extraction(
                config=self.GPT_VISION_CONFIG,
                api_key=self.openai_api_key,
                prompt=prompt,
                image_base64=image_base64,
                location=location,
                is_openrouter=False
            )
            
            if result.success:
                return result
            else:
                print(f"❌ {self.GPT_VISION_CONFIG['name']} also failed: {result.error}")
        
        # Both failed
        return ExtractionResult(
            success=False,
            error="All vision extraction attempts failed"
        )
    
    async def extract_from_images(
        self,
        images_base64: list[str],
        location: str = "Guam",
        use_fallback: bool = True
    ) -> ExtractionResult:
        """
        Extract recipe from multiple images (OCR) using vision models.
        
        Used for multi-page recipes, front/back recipe cards, etc.
        
        Args:
            images_base64: List of base64 encoded image data
            location: Location for cost estimation
            use_fallback: Whether to fall back to GPT-4o if Gemini fails
            
        Returns:
            ExtractionResult with recipe dict or error
        """
        num_images = len(images_base64)
        print(f"📸 Extracting recipe from {num_images} images...")
        print(f"📍 Location: {location}")
        total_size = sum(len(img) for img in images_base64) // 1024
        print(f"🖼️ Total size: {total_size}KB (base64)")
        
        # Use multi-image prompt
        prompt = get_multi_image_ocr_prompt(num_images, location)
        
        # Try Gemini Vision first (if API key available)
        if self.openrouter_api_key:
            print(f"🚀 Trying {self.GEMINI_VISION_CONFIG['name']} with {num_images} images...")
            result = await self._try_multi_image_extraction(
                config=self.GEMINI_VISION_CONFIG,
                api_key=self.openrouter_api_key,
                prompt=prompt,
                images_base64=images_base64,
                location=location,
                is_openrouter=True
            )
            
            if result.success:
                return result
            else:
                print(f"⚠️ {self.GEMINI_VISION_CONFIG['name']} failed: {result.error}")
        
        # Fall back to GPT-4o Vision
        if use_fallback and self.openai_api_key:
            print(f"🔄 Falling back to {self.GPT_VISION_CONFIG['name']}...")
            result = await self._try_multi_image_extraction(
                config=self.GPT_VISION_CONFIG,
                api_key=self.openai_api_key,
                prompt=prompt,
                images_base64=images_base64,
                location=location,
                is_openrouter=False
            )
            
            if result.success:
                return result
            else:
                print(f"❌ {self.GPT_VISION_CONFIG['name']} also failed: {result.error}")
        
        # Both failed
        return ExtractionResult(
            success=False,
            error="All multi-image extraction attempts failed"
        )
    
    async def generate_json(self, prompt: str) -> Optional[dict]:
        """
        Generate JSON from a prompt using Gemini (primary) or GPT (fallback).
        
        Simpler interface for when you just need JSON output without
        the full recipe extraction pipeline.
        
        Returns the parsed JSON dict, or None if extraction failed.
        """
        import time
        
        # Try Gemini first
        if self.openrouter_api_key:
            try:
                result = await self._call_simple_llm(
                    config=self.GEMINI_CONFIG,
                    api_key=self.openrouter_api_key,
                    prompt=prompt,
                    is_openrouter=True
                )
                if result:
                    return result
            except Exception as e:
                print(f"⚠️ Gemini failed: {e}")
        
        # Fallback to GPT
        if self.openai_api_key:
            try:
                result = await self._call_simple_llm(
                    config=self.GPT_CONFIG,
                    api_key=self.openai_api_key,
                    prompt=prompt,
                    is_openrouter=False
                )
                if result:
                    return result
            except Exception as e:
                print(f"⚠️ GPT also failed: {e}")
        
        return None
    
    async def _call_simple_llm(
        self,
        config: dict,
        api_key: str,
        prompt: str,
        is_openrouter: bool
    ) -> Optional[dict]:
        """Make a simple LLM API call and return parsed JSON."""
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        if is_openrouter:
            headers["HTTP-Referer"] = "https://recipe-extractor.app"
            headers["X-Title"] = "Recipe Extractor"
        
        payload = {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 4000,
        }
        
        if not is_openrouter:
            payload["response_format"] = {"type": "json_object"}
        
        url = f"{config['base_url']}/chat/completions"
        
        async with httpx.AsyncClient(timeout=config["timeout"]) as client:
            response = await client.post(url, headers=headers, json=payload)
            
            if response.status_code != 200:
                print(f"❌ LLM error: HTTP {response.status_code}")
                return None
            
            data = response.json()
            raw_content = data["choices"][0]["message"]["content"]
            
            if not raw_content:
                return None
            
            return self._parse_json_response(raw_content)
    
    async def _try_multi_image_extraction(
        self,
        config: dict,
        api_key: str,
        prompt: str,
        images_base64: list[str],
        location: str,
        is_openrouter: bool
    ) -> ExtractionResult:
        """Try multi-image extraction with a specific model, with retries."""
        
        last_error = None
        
        for attempt in range(config["max_retries"] + 1):
            if attempt > 0:
                wait_time = 2 ** (attempt - 1)
                print(f"   Retry {attempt}/{config['max_retries']} after {wait_time}s...")
                await asyncio.sleep(wait_time)
            
            try:
                result = await self._call_multi_image_vision_llm(
                    config=config,
                    api_key=api_key,
                    prompt=prompt,
                    images_base64=images_base64,
                    location=location,
                    is_openrouter=is_openrouter
                )
                
                if result.success:
                    return result
                else:
                    last_error = result.error
                    
            except Exception as e:
                last_error = str(e)
                print(f"   Attempt {attempt + 1} error: {last_error[:100]}")
        
        return ExtractionResult(
            success=False,
            error=last_error,
            model_used=config["name"]
        )
    
    async def _call_multi_image_vision_llm(
        self,
        config: dict,
        api_key: str,
        prompt: str,
        images_base64: list[str],
        location: str,
        is_openrouter: bool
    ) -> ExtractionResult:
        """Make a multi-image vision LLM API call."""
        
        import time
        start_time = time.time()
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        # OpenRouter specific headers
        if is_openrouter:
            headers["HTTP-Referer"] = "https://recipe-extractor.app"
            headers["X-Title"] = "Recipe Extractor"
        
        # Build content array with all images, labeled by page number
        content = []
        for i, img_base64 in enumerate(images_base64):
            # Add page label before each image
            content.append({
                "type": "text",
                "text": f"[PAGE {i + 1} OF {len(images_base64)}]"
            })
            mime_type = self._get_mime_type(img_base64)
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{img_base64}"
                }
            })
        
        # Add the prompt at the end
        content.append({
            "type": "text",
            "text": prompt
        })
        
        # Build the message with all images
        payload = {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": 0.1,
            "max_tokens": 5000,  # Increased for multi-image
        }
        
        url = f"{config['base_url']}/chat/completions"
        
        # Increase timeout for multi-image
        timeout = config["timeout"] + (len(images_base64) * 15)
        
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            
            latency = time.time() - start_time
            
            if response.status_code != 200:
                return ExtractionResult(
                    success=False,
                    error=f"HTTP {response.status_code}: {response.text[:200]}",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            data = response.json()
            
            # Extract content
            raw_content = data["choices"][0]["message"]["content"]
            
            if not raw_content:
                return ExtractionResult(
                    success=False,
                    error="Empty response from vision model",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            # Parse JSON (handle markdown code blocks)
            recipe_data = self._parse_json_response(raw_content)
            
            if recipe_data is None:
                return ExtractionResult(
                    success=False,
                    error="Failed to parse JSON from response",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            # Post-process
            recipe_data = self._post_process_recipe(recipe_data, "photo-upload", location)
            
            print(f"✅ Recipe extracted from {len(images_base64)} images with {config['name']}: {recipe_data.get('title', 'Untitled')}")
            print(f"   Latency: {latency:.1f}s | Components: {len(recipe_data.get('components', []))}")
            
            return ExtractionResult(
                success=True,
                recipe=recipe_data,
                model_used=config["name"],
                latency_seconds=latency
            )
    
    def _get_mime_type(self, image_base64: str) -> str:
        """Determine MIME type from base64 image data."""
        if image_base64.startswith("/9j/"):
            return "image/jpeg"
        elif image_base64.startswith("iVBOR"):
            return "image/png"
        elif image_base64.startswith("R0lG"):
            return "image/gif"
        elif image_base64.startswith("UklG"):
            return "image/webp"
        return "image/jpeg"  # Default to jpeg
    
    async def _try_vision_extraction(
        self,
        config: dict,
        api_key: str,
        prompt: str,
        image_base64: str,
        location: str,
        is_openrouter: bool
    ) -> ExtractionResult:
        """Try vision extraction with a specific model, with retries."""
        
        last_error = None
        
        for attempt in range(config["max_retries"] + 1):
            if attempt > 0:
                wait_time = 2 ** (attempt - 1)
                print(f"   Retry {attempt}/{config['max_retries']} after {wait_time}s...")
                await asyncio.sleep(wait_time)
            
            try:
                result = await self._call_vision_llm(
                    config=config,
                    api_key=api_key,
                    prompt=prompt,
                    image_base64=image_base64,
                    location=location,
                    is_openrouter=is_openrouter
                )
                
                if result.success:
                    return result
                else:
                    last_error = result.error
                    
            except Exception as e:
                last_error = str(e)
                print(f"   Attempt {attempt + 1} error: {last_error[:100]}")
        
        return ExtractionResult(
            success=False,
            error=last_error,
            model_used=config["name"]
        )
    
    async def _call_vision_llm(
        self,
        config: dict,
        api_key: str,
        prompt: str,
        image_base64: str,
        location: str,
        is_openrouter: bool
    ) -> ExtractionResult:
        """Make a single vision LLM API call."""
        
        import time
        start_time = time.time()
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        # OpenRouter specific headers
        if is_openrouter:
            headers["HTTP-Referer"] = "https://recipe-extractor.app"
            headers["X-Title"] = "Recipe Extractor"
        
        # Determine image MIME type (default to jpeg)
        mime_type = "image/jpeg"
        if image_base64.startswith("/9j/"):
            mime_type = "image/jpeg"
        elif image_base64.startswith("iVBOR"):
            mime_type = "image/png"
        elif image_base64.startswith("R0lG"):
            mime_type = "image/gif"
        elif image_base64.startswith("UklG"):
            mime_type = "image/webp"
        
        # Build the message with image
        payload = {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ],
            "temperature": 0.1,
            "max_tokens": 4000,
        }
        
        url = f"{config['base_url']}/chat/completions"
        
        async with httpx.AsyncClient(timeout=config["timeout"]) as client:
            response = await client.post(url, headers=headers, json=payload)
            
            latency = time.time() - start_time
            
            if response.status_code != 200:
                return ExtractionResult(
                    success=False,
                    error=f"HTTP {response.status_code}: {response.text[:200]}",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            data = response.json()
            
            # Extract content
            raw_content = data["choices"][0]["message"]["content"]
            
            if not raw_content:
                return ExtractionResult(
                    success=False,
                    error="Empty response from vision model",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            # Parse JSON (handle markdown code blocks)
            recipe_data = self._parse_json_response(raw_content)
            
            if recipe_data is None:
                return ExtractionResult(
                    success=False,
                    error="Failed to parse JSON from response",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            # Post-process
            recipe_data = self._post_process_recipe(recipe_data, "photo-upload", location)
            
            print(f"✅ Recipe extracted from image with {config['name']}: {recipe_data.get('title', 'Untitled')}")
            print(f"   Latency: {latency:.1f}s | Components: {len(recipe_data.get('components', []))}")
            
            return ExtractionResult(
                success=True,
                recipe=recipe_data,
                model_used=config["name"],
                latency_seconds=latency
            )
    
    async def _try_extraction(
        self,
        config: dict,
        api_key: str,
        prompt: str,
        source_url: str,
        location: str,
        is_openrouter: bool
    ) -> ExtractionResult:
        """Try extraction with a specific model, with retries."""
        
        last_error = None
        
        for attempt in range(config["max_retries"] + 1):
            if attempt > 0:
                # Exponential backoff: 1s, 2s, 4s...
                wait_time = 2 ** (attempt - 1)
                print(f"   Retry {attempt}/{config['max_retries']} after {wait_time}s...")
                await asyncio.sleep(wait_time)
            
            try:
                result = await self._call_llm(
                    config=config,
                    api_key=api_key,
                    prompt=prompt,
                    source_url=source_url,
                    location=location,
                    is_openrouter=is_openrouter
                )
                
                if result.success:
                    return result
                else:
                    last_error = result.error
                    
            except Exception as e:
                last_error = str(e)
                print(f"   Attempt {attempt + 1} error: {last_error[:100]}")
        
        return ExtractionResult(
            success=False,
            error=last_error,
            model_used=config["name"]
        )
    
    async def _call_llm(
        self,
        config: dict,
        api_key: str,
        prompt: str,
        source_url: str,
        location: str,
        is_openrouter: bool
    ) -> ExtractionResult:
        """Make a single LLM API call."""
        
        import time
        start_time = time.time()
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        # OpenRouter specific headers
        if is_openrouter:
            headers["HTTP-Referer"] = "https://recipe-extractor.app"
            headers["X-Title"] = "Recipe Extractor"
        
        payload = {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 4000,
        }
        
        # OpenAI supports response_format for guaranteed JSON
        if not is_openrouter:
            payload["response_format"] = {"type": "json_object"}
        
        url = f"{config['base_url']}/chat/completions"
        
        async with httpx.AsyncClient(timeout=config["timeout"]) as client:
            response = await client.post(url, headers=headers, json=payload)
            
            latency = time.time() - start_time
            
            if response.status_code != 200:
                return ExtractionResult(
                    success=False,
                    error=f"HTTP {response.status_code}: {response.text[:200]}",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            data = response.json()
            
            # Extract content
            raw_content = data["choices"][0]["message"]["content"]
            
            if not raw_content:
                return ExtractionResult(
                    success=False,
                    error="Empty response from LLM",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            # Parse JSON (handle markdown code blocks)
            recipe_data = self._parse_json_response(raw_content)
            
            if recipe_data is None:
                return ExtractionResult(
                    success=False,
                    error="Failed to parse JSON from response",
                    model_used=config["name"],
                    latency_seconds=latency
                )
            
            # Post-process
            recipe_data = self._post_process_recipe(recipe_data, source_url, location)
            
            print(f"✅ Recipe extracted with {config['name']}: {recipe_data.get('title', 'Untitled')}")
            print(f"   Latency: {latency:.1f}s | Components: {len(recipe_data.get('components', []))}")
            
            return ExtractionResult(
                success=True,
                recipe=recipe_data,
                model_used=config["name"],
                latency_seconds=latency
            )
    
    def _parse_json_response(self, raw_content: str) -> Optional[dict]:
        """Parse JSON from LLM response, handling markdown code blocks."""
        
        # Try direct parse first
        try:
            return json.loads(raw_content)
        except json.JSONDecodeError:
            pass
        
        # Try extracting from markdown code block
        json_str = raw_content
        
        if "```json" in json_str:
            try:
                json_str = json_str.split("```json")[1].split("```")[0]
                return json.loads(json_str.strip())
            except (IndexError, json.JSONDecodeError):
                pass
        
        if "```" in json_str:
            try:
                json_str = json_str.split("```")[1].split("```")[0]
                return json.loads(json_str.strip())
            except (IndexError, json.JSONDecodeError):
                pass
        
        # Try finding JSON object in content
        try:
            # Find first { and last }
            start = raw_content.find("{")
            end = raw_content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(raw_content[start:end])
        except json.JSONDecodeError:
            pass
        
        return None
    
    def _sanitize_text(self, text: str) -> str:
        """Clean text to prevent Unicode issues with the API."""
        # Remove emojis and high Unicode characters
        text = re.sub(
            r'[\U0001F600-\U0001F64F]|[\U0001F300-\U0001F5FF]|'
            r'[\U0001F680-\U0001F6FF]|[\U0001F1E0-\U0001F1FF]|'
            r'[\U00002600-\U000026FF]|[\U00002700-\U000027BF]',
            ' ', text
        )
        # Replace smart quotes
        text = text.replace('"', '"').replace('"', '"')
        text = text.replace(''', "'").replace(''', "'")
        # Replace ellipsis
        text = text.replace('…', '...')
        # Replace em/en dashes
        text = text.replace('—', '-').replace('–', '-')
        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    def _post_process_recipe(
        self,
        recipe: dict,
        source_url: str,
        location: str
    ) -> dict:
        """Post-process recipe data to ensure consistency."""
        # Ensure required fields
        recipe["sourceUrl"] = source_url
        recipe["costLocation"] = location
        
        # Ensure components exist
        if "components" not in recipe or not recipe["components"]:
            recipe["components"] = [{
                "name": recipe.get("title", "Main Dish"),
                "ingredients": recipe.get("ingredients", []),
                "steps": recipe.get("steps", []),
                "notes": None
            }]
        
        # Flatten ingredients and steps from components for legacy support
        all_ingredients = []
        all_steps = []
        for component in recipe.get("components", []):
            all_ingredients.extend(component.get("ingredients", []))
            if len(recipe.get("components", [])) > 1:
                for step in component.get("steps", []):
                    all_steps.append(f"{component.get('name', 'Step')}: {step}")
            else:
                all_steps.extend(component.get("steps", []))
        
        recipe["ingredients"] = all_ingredients
        recipe["steps"] = all_steps
        
        # Ensure media object
        if "media" not in recipe:
            recipe["media"] = {"thumbnail": None}
        
        # Ensure nutrition object
        if "nutrition" not in recipe:
            recipe["nutrition"] = {
                "perServing": {
                    "calories": None, "protein": None, "carbs": None,
                    "fat": None, "fiber": None, "sugar": None, "sodium": None
                },
                "total": {
                    "calories": None, "protein": None, "carbs": None,
                    "fat": None, "fiber": None, "sugar": None, "sodium": None
                }
            }
        
        # Sanitize nutrition values - convert floats to integers
        # (Pydantic schema expects int, but LLMs sometimes return floats like 187.5)
        nutrition = recipe.get("nutrition", {})
        for section in ["perServing", "total"]:
            if section in nutrition and isinstance(nutrition[section], dict):
                for key in ["calories", "protein", "carbs", "fat", "fiber", "sugar", "sodium"]:
                    value = nutrition[section].get(key)
                    if value is not None and isinstance(value, (int, float)):
                        nutrition[section][key] = int(round(value))
        recipe["nutrition"] = nutrition
        
        # Ensure times object
        if "times" not in recipe:
            recipe["times"] = {"prep": None, "cook": None, "total": None}
        
        # Ensure tags is a list
        if "tags" not in recipe or not isinstance(recipe["tags"], list):
            recipe["tags"] = []
        
        # Ensure equipment is a list
        if "equipment" not in recipe or not isinstance(recipe["equipment"], list):
            recipe["equipment"] = []
        
        return recipe


# Singleton instance
llm_service = LLMService()

