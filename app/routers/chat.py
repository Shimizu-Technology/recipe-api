"""Recipe chat API endpoints - AI-powered recipe assistant."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from uuid import UUID
from typing import Optional
import json

from openai import AsyncOpenAI

from app.db import get_db
from app.models.recipe import Recipe
from app.auth import get_current_user, get_optional_user, ClerkUser
from app.config import get_settings
from app.services.storage import storage_service

router = APIRouter(prefix="/api/recipes", tags=["chat"])
settings = get_settings()

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=settings.openai_api_key)


# ============================================================
# Schemas
# ============================================================

class ChatMessage(BaseModel):
    """A single chat message."""
    role: str  # 'user' or 'assistant'
    content: str
    image_url: Optional[str] = None  # Optional image URL for vision


class ChatRequest(BaseModel):
    """Request to chat about a recipe."""
    message: str
    history: list[ChatMessage] = []  # Previous messages for context
    image_base64: Optional[str] = None  # Optional base64 image for vision


class ChatResponse(BaseModel):
    """Response from the recipe chat."""
    response: str


class SuggestTagsRequest(BaseModel):
    """Request to suggest tags for a recipe."""
    title: str
    ingredients: list[str]


class SuggestTagsResponse(BaseModel):
    """Response with suggested tags."""
    tags: list[str]


class EstimateNutritionRequest(BaseModel):
    """Request to estimate nutrition for a recipe."""
    ingredients: list[str]
    servings: int = 4


class NutritionEstimate(BaseModel):
    """Estimated nutrition values."""
    calories: int
    protein: int
    carbs: int
    fat: int


class EstimateNutritionResponse(BaseModel):
    """Response with estimated nutrition."""
    nutrition: NutritionEstimate


class UploadChatImageRequest(BaseModel):
    """Request to upload a chat image to S3."""
    image_base64: str  # Base64 encoded image


class UploadChatImageResponse(BaseModel):
    """Response with the S3 URL of the uploaded image."""
    image_url: str
    

# ============================================================
# Helper Functions
# ============================================================

def build_recipe_context(recipe: Recipe) -> str:
    """Build a detailed context string from a recipe for the AI."""
    extracted = recipe.extracted or {}
    
    # Basic info
    title = extracted.get("title", "Untitled Recipe")
    servings = extracted.get("servings", "Unknown")
    times = extracted.get("times") or {}
    total_time = times.get("total", "Unknown")
    prep_time = times.get("prep", "Unknown")
    cook_time = times.get("cook", "Unknown")
    
    # Ingredients
    components = extracted.get("components", [])
    ingredients_text = ""
    for component in components:
        comp_name = component.get("name", "Main")
        ingredients = component.get("ingredients", [])
        if len(components) > 1:
            ingredients_text += f"\n{comp_name}:\n"
        for ing in ingredients:
            qty = ing.get("quantity", "")
            unit = ing.get("unit", "")
            name = ing.get("name", "")
            notes = ing.get("notes", "")
            cost = ing.get("estimatedCost")
            
            line = f"- {qty} {unit} {name}".strip()
            if notes:
                line += f" ({notes})"
            if cost:
                line += f" [${cost:.2f}]"
            ingredients_text += line + "\n"
    
    # Steps
    steps_text = ""
    for component in components:
        comp_name = component.get("name", "Main")
        steps = component.get("steps", [])
        if len(components) > 1:
            steps_text += f"\n{comp_name}:\n"
        for i, step in enumerate(steps, 1):
            steps_text += f"{i}. {step}\n"
    
    # Nutrition
    nutrition = extracted.get("nutrition", {})
    per_serving = nutrition.get("perServing", {})
    nutrition_text = ""
    if per_serving:
        nutrition_text = f"""
Nutrition (per serving):
- Calories: {per_serving.get('calories', 'N/A')}
- Protein: {per_serving.get('protein', 'N/A')}g
- Carbs: {per_serving.get('carbs', 'N/A')}g
- Fat: {per_serving.get('fat', 'N/A')}g
"""
    
    # Equipment
    equipment = extracted.get("equipment", [])
    equipment_text = ""
    if equipment:
        equipment_text = "\nEquipment needed:\n" + "\n".join(f"- {e}" for e in equipment)
    
    # Tags
    tags = extracted.get("tags", [])
    tags_text = f"\nTags: {', '.join(tags)}" if tags else ""
    
    # Cost
    total_cost = extracted.get("totalEstimatedCost")
    cost_location = extracted.get("costLocation", "")
    cost_text = ""
    if total_cost:
        cost_text = f"\nEstimated total cost: ${total_cost:.2f}"
        if cost_location:
            cost_text += f" ({cost_location} pricing)"
    
    # Notes
    notes = extracted.get("notes", "")
    notes_text = f"\nChef's notes: {notes}" if notes else ""
    
    context = f"""
RECIPE: {title}

Servings: {servings}
Prep time: {prep_time}
Cook time: {cook_time}
Total time: {total_time}

INGREDIENTS:
{ingredients_text}

INSTRUCTIONS:
{steps_text}
{nutrition_text}
{equipment_text}
{tags_text}
{cost_text}
{notes_text}
""".strip()
    
    return context


def build_system_prompt(recipe_context: str) -> str:
    """Build the system prompt for the recipe chat assistant."""
    return f"""You are a helpful, friendly cooking assistant. You have complete knowledge of the following recipe and can answer any questions about it.

{recipe_context}

Your role:
1. Answer questions about this specific recipe
2. Suggest ingredient substitutions when asked
3. Help scale the recipe up or down
4. Provide cooking tips and troubleshooting advice
5. Suggest wine/drink pairings
6. Explain cooking techniques mentioned in the recipe
7. Offer dietary modifications (dairy-free, gluten-free, vegan, etc.)
8. Analyze photos the user shares - this is VERY important!

IMPORTANT - When the user shares a photo:
- ALWAYS examine the image carefully and provide specific, helpful observations
- Read any text, labels, or measurements visible in the image
- For measuring cups/tools: identify the measurement markings and help the user find the right amount
- For food photos: assess doneness, color, texture, and provide specific feedback
- For ingredient photos: identify what you see and how it relates to the recipe
- Be confident in your visual analysis - users are counting on you to see details!
- If asked "how much is X on this cup", look at the cup markings and guide them

Guidelines:
- Be concise but helpful
- When analyzing images, be specific about what you see - don't say you can't read measurements
- When suggesting substitutions, explain how it might affect the dish
- For scaling, recalculate ingredient amounts accurately
- Be encouraging and supportive
- Use emojis sparingly to be friendly 🍳

If asked about something unrelated to cooking or this recipe, politely redirect the conversation back to the recipe."""


# ============================================================
# Endpoints
# ============================================================

@router.post("/{recipe_id}/chat", response_model=ChatResponse)
async def chat_about_recipe(
    recipe_id: UUID,
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user: Optional[ClerkUser] = Depends(get_optional_user)
):
    """
    Chat with an AI assistant about a specific recipe.
    
    The AI has full context of the recipe and can answer questions about:
    - Ingredient substitutions
    - Scaling the recipe
    - Cooking tips and troubleshooting
    - Dietary modifications
    - Wine pairings
    - And more!
    """
    # Get the recipe
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Check authorization - must be owner or recipe must be public
    if not recipe.is_public and (not user or recipe.user_id != user.id):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to access this recipe"
        )
    
    # Build the context and system prompt
    recipe_context = build_recipe_context(recipe)
    system_prompt = build_system_prompt(recipe_context)
    
    # Build messages for OpenAI
    messages = [
        {"role": "system", "content": system_prompt}
    ]
    
    # Add conversation history
    for msg in request.history[-10:]:  # Limit to last 10 messages for context
        # Check if this message has an image URL that we can use
        # Only use URLs that are actual web URLs (S3), not local file:// URIs
        if msg.image_url and msg.image_url.startswith("https://"):
            # S3 URL - OpenAI can access this
            messages.append({
                "role": msg.role,
                "content": [
                    {"type": "text", "text": msg.content},
                    {"type": "image_url", "image_url": {"url": msg.image_url}}
                ]
            })
        else:
            # No image or local file URI (can't be accessed by OpenAI)
            messages.append({
                "role": msg.role,
                "content": msg.content
            })
    
    # Add the current user message (with optional image)
    if request.image_base64:
        # Determine MIME type from base64 prefix
        mime_type = "image/jpeg"
        if request.image_base64.startswith("/9j/"):
            mime_type = "image/jpeg"
        elif request.image_base64.startswith("iVBOR"):
            mime_type = "image/png"
        elif request.image_base64.startswith("R0lG"):
            mime_type = "image/gif"
        elif request.image_base64.startswith("UklG"):
            mime_type = "image/webp"
        
        image_url = f"data:{mime_type};base64,{request.image_base64}"
        
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": request.message or "What do you see in this image? How does it relate to the recipe?"},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        })
    else:
        messages.append({
            "role": "user",
            "content": request.message
        })
    
    try:
        # Call GPT-4o for better conversational quality
        response = await openai_client.chat.completions.create(
            model="gpt-4o",  # Using GPT-4o for better chat quality
            messages=messages,
            max_tokens=1000,
            temperature=0.7,  # Slightly creative but still accurate
        )
        
        assistant_message = response.choices[0].message.content
        
        return ChatResponse(response=assistant_message)
        
    except Exception as e:
        print(f"❌ Chat error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to get response from AI. Please try again."
        )


@router.post("/ai/upload-chat-image", response_model=UploadChatImageResponse)
async def upload_chat_image(
    request: UploadChatImageRequest,
    user: ClerkUser = Depends(get_current_user)
):
    """
    Upload a chat image to S3 for persistent storage.
    
    This allows images to be stored with permanent URLs that can be
    included in chat history and re-sent to OpenAI for context.
    
    Returns the S3 URL of the uploaded image.
    """
    if not request.image_base64:
        raise HTTPException(status_code=400, detail="No image provided")
    
    # Upload to S3
    s3_url = await storage_service.upload_chat_image(
        image_base64=request.image_base64,
        user_id=user.id,
    )
    
    if not s3_url:
        raise HTTPException(
            status_code=500,
            detail="Failed to upload image. Please try again."
        )
    
    return UploadChatImageResponse(image_url=s3_url)


@router.post("/ai/suggest-tags", response_model=SuggestTagsResponse)
async def suggest_tags(
    request: SuggestTagsRequest,
    user: ClerkUser = Depends(get_current_user)
):
    """
    Suggest tags for a recipe based on title and ingredients.
    """
    ingredient_list = ", ".join(request.ingredients)
    
    prompt = f"""Based on this recipe information, suggest 5-8 relevant tags.

Recipe title: {request.title}
Ingredients: {ingredient_list}

Return ONLY a JSON array of lowercase tag strings. Tags should describe:
- Cuisine type (italian, mexican, asian, etc.)
- Meal type (breakfast, lunch, dinner, snack, dessert)
- Dietary info (vegetarian, vegan, gluten-free, dairy-free, keto, etc.)
- Cooking method (baked, grilled, fried, slow-cooker, etc.)
- Key characteristics (quick, easy, healthy, comfort-food, etc.)

Example response: ["italian", "dinner", "pasta", "quick", "vegetarian"]

Return ONLY the JSON array, no other text."""

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that suggests recipe tags. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.5,
        )
        
        result = response.choices[0].message.content.strip()
        
        # Parse JSON response
        try:
            # Handle potential markdown code blocks
            if result.startswith("```"):
                result = result.split("```")[1]
                if result.startswith("json"):
                    result = result[4:]
            
            tags = json.loads(result)
            if isinstance(tags, list):
                return SuggestTagsResponse(tags=tags[:10])  # Limit to 10 tags
        except json.JSONDecodeError:
            # Fallback: try to extract comma-separated values
            tags = [t.strip().lower().strip('"\'') for t in result.split(",")]
            return SuggestTagsResponse(tags=tags[:10])
        
        return SuggestTagsResponse(tags=[])
        
    except Exception as e:
        print(f"❌ Tag suggestion error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to suggest tags. Please try again."
        )


@router.post("/ai/estimate-nutrition", response_model=EstimateNutritionResponse)
async def estimate_nutrition(
    request: EstimateNutritionRequest,
    user: ClerkUser = Depends(get_current_user)
):
    """
    Estimate nutrition facts for a recipe based on ingredients.
    """
    ingredient_list = "\n".join(f"- {ing}" for ing in request.ingredients)
    
    prompt = f"""Estimate the nutrition facts PER SERVING for a recipe with {request.servings} servings.

Ingredients:
{ingredient_list}

Calculate reasonable estimates based on standard nutritional databases.
Return ONLY a JSON object with these numeric values (integers, no units):
{{"calories": number, "protein": number, "carbs": number, "fat": number}}

Example: {{"calories": 350, "protein": 25, "carbs": 30, "fat": 12}}

Return ONLY the JSON object, no other text."""

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a nutrition expert. Estimate nutrition facts accurately based on common ingredient values. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=100,
            temperature=0.3,  # More deterministic for calculations
        )
        
        result = response.choices[0].message.content.strip()
        
        # Parse JSON response
        try:
            # Handle potential markdown code blocks
            if result.startswith("```"):
                result = result.split("```")[1]
                if result.startswith("json"):
                    result = result[4:]
            
            # Find JSON object in response
            json_match = result
            if "{" in result:
                start = result.index("{")
                end = result.rindex("}") + 1
                json_match = result[start:end]
            
            nutrition = json.loads(json_match)
            
            return EstimateNutritionResponse(
                nutrition=NutritionEstimate(
                    calories=int(nutrition.get("calories", 0)),
                    protein=int(nutrition.get("protein", 0)),
                    carbs=int(nutrition.get("carbs", 0)),
                    fat=int(nutrition.get("fat", 0)),
                )
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Failed to parse nutrition JSON: {result}")
            raise HTTPException(
                status_code=500,
                detail="Failed to parse nutrition data. Please try again."
            )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Nutrition estimation error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to estimate nutrition. Please try again."
        )

