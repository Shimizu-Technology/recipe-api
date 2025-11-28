"""OpenAI service for Whisper transcription and GPT recipe extraction."""

import re
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Any
from openai import AsyncOpenAI

from app.config import get_settings
from app.services.prompts import get_recipe_extraction_prompt

settings = get_settings()


@dataclass
class TranscriptionResult:
    """Result of Whisper transcription."""
    success: bool
    text: str = ""
    error: Optional[str] = None
    duration: Optional[float] = None


@dataclass  
class ExtractionResult:
    """Result of GPT recipe extraction."""
    success: bool
    recipe: Optional[dict] = None
    error: Optional[str] = None


class OpenAIService:
    """Service for OpenAI API interactions (Whisper + GPT)."""
    
    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
    
    async def transcribe_audio(self, audio_file_path: str) -> TranscriptionResult:
        """
        Transcribe audio using OpenAI Whisper API.
        
        Args:
            audio_file_path: Path to the audio file (mp3, wav, etc.)
            
        Returns:
            TranscriptionResult with text or error
        """
        print(f"🗣️ Transcribing audio with Whisper: {audio_file_path}")
        
        try:
            # Read the audio file
            audio_path = Path(audio_file_path)
            if not audio_path.exists():
                return TranscriptionResult(
                    success=False,
                    error=f"Audio file not found: {audio_file_path}"
                )
            
            # Open and send to Whisper API
            with open(audio_path, "rb") as audio_file:
                transcription = await self.client.audio.transcriptions.create(
                    file=audio_file,
                    model="whisper-1",
                    language="en",
                    response_format="text",
                    temperature=0.0  # More deterministic output
                )
            
            print(f"✅ Transcription complete: {len(transcription)} characters")
            
            return TranscriptionResult(
                success=True,
                text=transcription
            )
            
        except Exception as e:
            print(f"❌ Whisper transcription failed: {e}")
            return TranscriptionResult(
                success=False,
                error=str(e)
            )
    
    async def extract_recipe(
        self,
        source_url: str,
        content: str,
        location: str = "Guam"
    ) -> ExtractionResult:
        """
        Extract structured recipe data using GPT-4o-mini.
        
        Args:
            source_url: Original video URL
            content: Combined text content (title + description + transcript)
            location: Location for cost estimation
            
        Returns:
            ExtractionResult with recipe dict or error
        """
        print(f"🤖 Extracting recipe with GPT-4o-mini...")
        print(f"📍 Location: {location}")
        print(f"📝 Content length: {len(content)} chars")
        
        # Sanitize content
        sanitized_content = self._sanitize_text(content)
        
        # Generate prompt
        prompt = get_recipe_extraction_prompt(source_url, sanitized_content, location)
        
        try:
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a culinary extraction engine. Extract recipe information and return valid JSON only."
                    },
                    {
                        "role": "user", 
                        "content": prompt
                    }
                ],
                response_format={"type": "json_object"},
                temperature=0.1,  # Low temperature for consistent output
                max_tokens=4000
            )
            
            # Parse the response
            response_text = response.choices[0].message.content
            if not response_text:
                return ExtractionResult(
                    success=False,
                    error="Empty response from GPT"
                )
            
            recipe_data = json.loads(response_text)
            
            # Post-process and validate
            recipe_data = self._post_process_recipe(recipe_data, source_url, location)
            
            print(f"✅ Recipe extracted: {recipe_data.get('title', 'Untitled')}")
            print(f"   Components: {len(recipe_data.get('components', []))}")
            print(f"   Total cost: ${recipe_data.get('totalEstimatedCost', 0):.2f}")
            
            return ExtractionResult(
                success=True,
                recipe=recipe_data
            )
            
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse GPT response as JSON: {e}")
            return ExtractionResult(
                success=False,
                error=f"Invalid JSON response: {e}"
            )
        except Exception as e:
            print(f"❌ GPT extraction failed: {e}")
            return ExtractionResult(
                success=False,
                error=str(e)
            )
    
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
            # Prefix steps with component name if multiple components
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
    
    @staticmethod
    def estimate_whisper_cost(duration_minutes: float) -> float:
        """Estimate Whisper API cost based on audio duration."""
        # Whisper API costs $0.006 per minute
        return duration_minutes * 0.006
    
    @staticmethod
    def estimate_gpt_cost(input_tokens: int, output_tokens: int) -> float:
        """Estimate GPT-4o-mini cost based on tokens."""
        # GPT-4o-mini: $0.15/1M input, $0.60/1M output
        input_cost = (input_tokens / 1_000_000) * 0.15
        output_cost = (output_tokens / 1_000_000) * 0.60
        return input_cost + output_cost


# Singleton instance
openai_service = OpenAIService()

