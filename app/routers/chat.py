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


class ChatRequest(BaseModel):
    """Request to chat about a recipe."""
    message: str
    history: list[ChatMessage] = []  # Previous messages for context


class ChatResponse(BaseModel):
    """Response from the recipe chat."""
    response: str
    

# ============================================================
# Helper Functions
# ============================================================

def build_recipe_context(recipe: Recipe) -> str:
    """Build a detailed context string from a recipe for the AI."""
    extracted = recipe.extracted or {}
    
    # Basic info
    title = extracted.get("title", "Untitled Recipe")
    servings = extracted.get("servings", "Unknown")
    times = extracted.get("times", {})
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

Guidelines:
- Be concise but helpful
- If you're unsure about something, say so
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
        messages.append({
            "role": msg.role,
            "content": msg.content
        })
    
    # Add the current user message
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

