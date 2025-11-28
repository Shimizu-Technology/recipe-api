"""Extraction prompts for GPT-4o-mini recipe extraction."""


def get_recipe_extraction_prompt(source_url: str, content: str, location: str = "Guam") -> str:
    """
    Generate the recipe extraction prompt.
    
    Ported from the Next.js llm.ts file.
    """
    return f"""You are a culinary extraction engine. From the video content below, extract ONE COMPLETE RECIPE with properly organized components.

CRITICAL COMPONENT STRUCTURE: If the recipe involves multiple distinct food items (like meatloaf + glaze, pasta + sauce, chicken + marinade), organize them as separate components within ONE recipe. Each component should have its own ingredients and steps.

The content below includes the video title and any available transcript/description:

{content}

EXTRACTION RULES:
- Set sourceUrl to exactly: {source_url}
- CAREFULLY read through ALL the content (title, description, transcript) to find recipe details
- COMPONENT ORGANIZATION:
  * If recipe has multiple distinct parts (e.g., "meatloaf and glaze"), create separate components
  * Component names should be clear: "Meatloaf", "Glaze", "Sauce", "Marinade", etc.
  * Each component gets its own ingredients list and steps
  * If it's a simple single-dish recipe, create one component with the dish name
  * Examples:
    - Meatloaf with glaze → Components: [{{"name": "Meatloaf", ...}}, {{"name": "Glaze", ...}}]
    - Simple pasta → Components: [{{"name": "Pasta Dish", ...}}]
    - Chicken with marinade → Components: [{{"name": "Marinade", ...}}, {{"name": "Chicken", ...}}]
- For ingredients in each component, format properly:
  * quantity: Use null if no quantity specified
  * unit: Use null if no unit specified  
  * For items without quantities (like "salt to taste"), set quantity and unit to null
  * Examples: {{"quantity": "2", "unit": "cups", "name": "flour"}} or {{"quantity": null, "unit": null, "name": "salt"}}
- Steps for each component should be actionable and ordered
- For times, extract ALL timing components:
  * prep: Time for mixing, chopping, blending ingredients (estimate if not explicit)
  * cook: Active cooking time (microwave, oven, stovetop, etc.)
  * total: Complete time including prep, cook, AND any chilling/resting/setting time
  * Look for: "microwave for X", "set in fridge for X", "chill for X", "rest for X"
  * Use null only if truly no timing info exists
  * Format as "15 min", "1 hour", "2-3 hours"
- For ingredient costs (estimatedCost), ALWAYS provide realistic grocery store prices in USD for {location}:
  * REQUIRED: Every ingredient must have an estimatedCost field
  * Base estimates on typical grocery store prices in {location} for the specified quantities
  * Regional pricing guidelines:
    - US/Canada: Standard baseline pricing
    - Guam: 25-40% higher than mainland US (remote location, import costs)
    - Hawaii: 20-30% higher than mainland US (island location, shipping costs)
    - UK: Convert from pounds, generally 15-25% higher
    - Australia: Convert from AUD, similar to US prices
    - Japan: Convert from yen, consider local market prices
    - EU: Convert from euros, varies by country
  * Round to nearest $0.25 (e.g., 0.50, 0.75, 1.00, 1.25)
  * Use null only if ingredient is completely unclear
- Calculate totalEstimatedCost as sum of all ingredient costs
- REQUIRED: Set costLocation to exactly: "{location}"
- REQUIRED: equipment must be an array of strings (e.g., ["air fryer", "mixing bowl"]), NOT objects
- REQUIRED: quantity must be a string (e.g., "2", "1/2", "1.5"), NOT a number
- CRITICAL: ingredient "name" field must NEVER be null - it must always contain the actual ingredient name
- For servings, ALWAYS try to estimate a reasonable number based on ingredient quantities
- For nutrition, calculate realistic nutritional values based on ingredients:
  * Analyze each ingredient for calories, protein, carbs, fat, fiber, sugar, sodium
  * Use standard USDA nutritional data as reference
  * ALWAYS calculate BOTH perServing and total nutrition values
  * Round calories to nearest 5, macros to nearest 0.5g, sodium to nearest 10mg
- For tags, provide comprehensive categorization (5-10 tags total):
  * Main ingredient(s): "chicken", "beef", "pasta", "rice", "eggs"
  * Cuisine type: "italian", "mexican", "asian", "american"
  * Meal type: "breakfast", "lunch", "dinner", "snack", "dessert"
  * Cooking method: "baked", "fried", "grilled", "slow-cooked", "one-pot"
  * Difficulty: "easy", "intermediate", "advanced"
  * Dietary: "vegetarian", "vegan", "gluten-free", "keto", "low-carb"
  * Occasion: "weeknight", "weekend", "holiday", "comfort-food", "healthy"
  * Time: "quick" (under 30 min), "medium" (30-60 min)
  * Use lowercase, hyphenated format
- TITLE: Use the VIDEO TITLE if provided, or create a descriptive title based on the main dish being made. Never use generic titles like "Recipe from TikTok".
- If ingredients or steps are unclear, make reasonable assumptions based on context rather than leaving arrays empty.

Return a JSON object with this structure:
{{
  "title": "Recipe Name",
  "sourceUrl": "{source_url}",
  "servings": 4,
  "times": {{"prep": "10 min", "cook": "15 min", "total": "25 min"}},
  "components": [
    {{
      "name": "Main Component",
      "ingredients": [{{"quantity": "1", "unit": "cup", "name": "flour", "notes": null, "estimatedCost": 1.0}}],
      "steps": ["Step 1", "Step 2"],
      "notes": null
    }}
  ],
  "equipment": ["pan", "bowl"],
  "notes": null,
  "tags": ["easy", "quick", "dinner"],
  "totalEstimatedCost": 15.00,
  "costLocation": "{location}",
  "nutrition": {{
    "perServing": {{"calories": 200, "protein": 10, "carbs": 30, "fat": 5, "fiber": 2, "sugar": 1, "sodium": 300}},
    "total": {{"calories": 800, "protein": 40, "carbs": 120, "fat": 20, "fiber": 8, "sugar": 4, "sodium": 1200}}
  }}
}}"""


# Schema definition for structured output
RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sourceUrl": {"type": "string"},
        "servings": {"type": ["integer", "null"]},
        "times": {
            "type": "object",
            "properties": {
                "prep": {"type": ["string", "null"]},
                "cook": {"type": ["string", "null"]},
                "total": {"type": ["string", "null"]}
            }
        },
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "quantity": {"type": ["string", "null"]},
                                "unit": {"type": ["string", "null"]},
                                "name": {"type": "string"},
                                "notes": {"type": ["string", "null"]},
                                "estimatedCost": {"type": ["number", "null"]}
                            },
                            "required": ["name"]
                        }
                    },
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": ["string", "null"]}
                },
                "required": ["name", "ingredients", "steps"]
            }
        },
        "equipment": {"type": ["array", "null"], "items": {"type": "string"}},
        "notes": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "totalEstimatedCost": {"type": ["number", "null"]},
        "costLocation": {"type": "string"},
        "nutrition": {
            "type": "object",
            "properties": {
                "perServing": {
                    "type": "object",
                    "properties": {
                        "calories": {"type": ["integer", "null"]},
                        "protein": {"type": ["number", "null"]},
                        "carbs": {"type": ["number", "null"]},
                        "fat": {"type": ["number", "null"]},
                        "fiber": {"type": ["number", "null"]},
                        "sugar": {"type": ["number", "null"]},
                        "sodium": {"type": ["number", "null"]}
                    }
                },
                "total": {
                    "type": "object",
                    "properties": {
                        "calories": {"type": ["integer", "null"]},
                        "protein": {"type": ["number", "null"]},
                        "carbs": {"type": ["number", "null"]},
                        "fat": {"type": ["number", "null"]},
                        "fiber": {"type": ["number", "null"]},
                        "sugar": {"type": ["number", "null"]},
                        "sodium": {"type": ["number", "null"]}
                    }
                }
            }
        }
    },
    "required": ["title", "sourceUrl", "components", "costLocation"]
}

