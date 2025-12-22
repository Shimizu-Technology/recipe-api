"""
Website recipe extraction service.

Extracts recipes from recipe blog/website URLs using:
1. JSON-LD structured data (Schema.org Recipe) - preferred, very reliable
2. AI extraction from main content - fallback for sites without structured data
"""

import httpx
import json
import re
from dataclasses import dataclass
from typing import Optional, Any
from bs4 import BeautifulSoup

try:
    import extruct
except ImportError:
    extruct = None

try:
    import trafilatura
except ImportError:
    trafilatura = None


@dataclass
class WebsiteExtractionResult:
    """Result of website recipe extraction."""
    success: bool
    recipe: Optional[dict] = None
    raw_text: Optional[str] = None
    thumbnail_url: Optional[str] = None
    extraction_method: str = "website"
    extraction_quality: str = "good"
    error: Optional[str] = None


class WebsiteService:
    """Service for extracting recipes from websites."""
    
    # Common recipe site domains that we know have good structured data
    KNOWN_RECIPE_SITES = [
        "allrecipes.com",
        "foodnetwork.com",
        "epicurious.com",
        "bonappetit.com",
        "seriouseats.com",
        "tasty.co",
        "delish.com",
        "simplyrecipes.com",
        "cookinglight.com",
        "myrecipes.com",
        "food52.com",
        "thekitchn.com",
        "eatingwell.com",
        "tasteofhome.com",
        "bettycrocker.com",
        "pillsbury.com",
        "kingarthurbaking.com",
        "sallysbakingaddiction.com",
        "budgetbytes.com",
        "minimalistbaker.com",
        "pinchofyum.com",
        "halfbakedharvest.com",
        "damndelicious.net",
        "skinnytaste.com",
    ]
    
    # User agent to avoid being blocked
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    @classmethod
    async def extract(
        cls,
        url: str,
        location: str = "",
        notes: str = "",
    ) -> WebsiteExtractionResult:
        """
        Extract recipe from a website URL.
        
        Strategy:
        1. Fetch HTML content
        2. Try to extract JSON-LD Recipe schema (most reliable)
        3. If no JSON-LD, use AI to extract from page content
        """
        try:
            # Fetch the HTML
            html = await cls._fetch_html(url)
            if not html:
                return WebsiteExtractionResult(
                    success=False,
                    error="Failed to fetch webpage"
                )
            
            # Try JSON-LD first (most reliable)
            jsonld_recipe = cls._extract_jsonld_recipe(html, url)
            if jsonld_recipe:
                print(f"✅ Found JSON-LD recipe schema")
                recipe = cls._convert_jsonld_to_recipe(jsonld_recipe, url, location, notes)
                thumbnail = cls._extract_thumbnail(html, jsonld_recipe)
                return WebsiteExtractionResult(
                    success=True,
                    recipe=recipe,
                    raw_text=json.dumps(jsonld_recipe, indent=2),
                    thumbnail_url=thumbnail,
                    extraction_method="website-jsonld",
                    extraction_quality="high",
                )
            
            # Fallback: Extract main content and use AI
            print(f"⚠️ No JSON-LD found, using AI extraction")
            main_content = cls._extract_main_content(html)
            if not main_content or len(main_content) < 100:
                return WebsiteExtractionResult(
                    success=False,
                    error="Could not extract recipe content from page"
                )
            
            # Use AI to extract recipe
            from app.services.llm_client import llm_service
            recipe = await cls._ai_extract_recipe(main_content, url, location, notes)
            
            if not recipe:
                return WebsiteExtractionResult(
                    success=False,
                    error="AI could not extract recipe from page content"
                )
            
            thumbnail = cls._extract_thumbnail(html, None)
            return WebsiteExtractionResult(
                success=True,
                recipe=recipe,
                raw_text=main_content[:5000],  # Truncate for storage
                thumbnail_url=thumbnail,
                extraction_method="website-ai",
                extraction_quality="good",
            )
            
        except Exception as e:
            print(f"❌ Website extraction error: {e}")
            return WebsiteExtractionResult(
                success=False,
                error=str(e)
            )
    
    @classmethod
    async def _fetch_html(cls, url: str) -> Optional[str]:
        """Fetch HTML content from URL."""
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                response = await client.get(url, headers=cls.HEADERS)
                response.raise_for_status()
                return response.text
        except Exception as e:
            print(f"❌ Failed to fetch {url}: {e}")
            return None
    
    @classmethod
    def _extract_jsonld_recipe(cls, html: str, url: str) -> Optional[dict]:
        """Extract JSON-LD Recipe schema from HTML."""
        try:
            # Method 1: Use extruct library (preferred)
            if extruct:
                data = extruct.extract(html, base_url=url, syntaxes=['json-ld'])
                jsonld_items = data.get('json-ld', [])
                
                for item in jsonld_items:
                    # Handle @graph format (common in some sites)
                    if isinstance(item, dict) and '@graph' in item:
                        for graph_item in item['@graph']:
                            if cls._is_recipe_schema(graph_item):
                                return graph_item
                    # Direct Recipe type
                    if cls._is_recipe_schema(item):
                        return item
            
            # Method 2: Manual parsing fallback
            soup = BeautifulSoup(html, 'lxml')
            scripts = soup.find_all('script', type='application/ld+json')
            
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    
                    # Handle array format
                    if isinstance(data, list):
                        for item in data:
                            if cls._is_recipe_schema(item):
                                return item
                    # Handle @graph format
                    elif isinstance(data, dict) and '@graph' in data:
                        for item in data['@graph']:
                            if cls._is_recipe_schema(item):
                                return item
                    # Direct Recipe
                    elif cls._is_recipe_schema(data):
                        return data
                except json.JSONDecodeError:
                    continue
            
            return None
            
        except Exception as e:
            print(f"❌ JSON-LD extraction error: {e}")
            return None
    
    @staticmethod
    def _is_recipe_schema(item: Any) -> bool:
        """Check if an item is a Recipe schema."""
        if not isinstance(item, dict):
            return False
        item_type = item.get('@type', '')
        # Handle both string and list types
        if isinstance(item_type, list):
            return 'Recipe' in item_type
        return item_type == 'Recipe'
    
    @classmethod
    def _convert_jsonld_to_recipe(
        cls,
        jsonld: dict,
        url: str,
        location: str = "",
        notes: str = "",
    ) -> dict:
        """Convert JSON-LD Recipe schema to our recipe format."""
        # Parse ingredients
        ingredients = []
        raw_ingredients = jsonld.get('recipeIngredient', [])
        for ing in raw_ingredients:
            if isinstance(ing, str):
                parsed = cls._parse_ingredient_string(ing)
                ingredients.append(parsed)
        
        # Parse instructions/steps
        steps = []
        raw_instructions = jsonld.get('recipeInstructions', [])
        for instruction in raw_instructions:
            if isinstance(instruction, str):
                steps.append({"text": instruction})
            elif isinstance(instruction, dict):
                # HowToStep format
                text = instruction.get('text', instruction.get('name', ''))
                if text:
                    steps.append({"text": text})
            elif isinstance(instruction, list):
                # Nested sections
                for sub in instruction:
                    if isinstance(sub, dict):
                        text = sub.get('text', sub.get('name', ''))
                        if text:
                            steps.append({"text": text})
        
        # Parse times
        times = {}
        if jsonld.get('prepTime'):
            times['prep'] = cls._parse_iso_duration(jsonld['prepTime'])
        if jsonld.get('cookTime'):
            times['cook'] = cls._parse_iso_duration(jsonld['cookTime'])
        if jsonld.get('totalTime'):
            times['total'] = cls._parse_iso_duration(jsonld['totalTime'])
        
        # Parse nutrition
        nutrition = {}
        if jsonld.get('nutrition'):
            raw_nutrition = jsonld['nutrition']
            per_serving = {}
            if raw_nutrition.get('calories'):
                cal_str = str(raw_nutrition['calories'])
                cal_match = re.search(r'(\d+)', cal_str)
                if cal_match:
                    per_serving['calories'] = int(cal_match.group(1))
            if raw_nutrition.get('proteinContent'):
                prot_str = str(raw_nutrition['proteinContent'])
                prot_match = re.search(r'(\d+)', prot_str)
                if prot_match:
                    per_serving['protein'] = int(prot_match.group(1))
            if raw_nutrition.get('carbohydrateContent'):
                carb_str = str(raw_nutrition['carbohydrateContent'])
                carb_match = re.search(r'(\d+)', carb_str)
                if carb_match:
                    per_serving['carbs'] = int(carb_match.group(1))
            if raw_nutrition.get('fatContent'):
                fat_str = str(raw_nutrition['fatContent'])
                fat_match = re.search(r'(\d+)', fat_str)
                if fat_match:
                    per_serving['fat'] = int(fat_match.group(1))
            if per_serving:
                nutrition = {"perServing": per_serving}
        
        # Parse servings
        servings = None
        if jsonld.get('recipeYield'):
            yield_val = jsonld['recipeYield']
            if isinstance(yield_val, list):
                yield_val = yield_val[0] if yield_val else None
            if yield_val:
                match = re.search(r'(\d+)', str(yield_val))
                if match:
                    servings = int(match.group(1))
        
        # Parse tags/keywords
        tags = []
        if jsonld.get('keywords'):
            keywords = jsonld['keywords']
            if isinstance(keywords, str):
                tags = [k.strip().lower() for k in keywords.split(',') if k.strip()]
            elif isinstance(keywords, list):
                tags = [k.strip().lower() for k in keywords if isinstance(k, str)]
        
        # Parse category for meal types
        meal_types = []
        if jsonld.get('recipeCategory'):
            categories = jsonld['recipeCategory']
            if isinstance(categories, str):
                categories = [categories]
            for cat in categories:
                cat_lower = cat.lower()
                if 'breakfast' in cat_lower:
                    meal_types.append('breakfast')
                elif 'lunch' in cat_lower:
                    meal_types.append('lunch')
                elif 'dinner' in cat_lower or 'main' in cat_lower:
                    meal_types.append('dinner')
                elif 'dessert' in cat_lower:
                    meal_types.append('dessert')
                elif 'snack' in cat_lower or 'appetizer' in cat_lower:
                    meal_types.append('snack')
        
        # Build recipe object
        recipe = {
            "title": jsonld.get('name', 'Untitled Recipe'),
            "description": jsonld.get('description', ''),
            "servings": servings,
            "times": times if times else None,
            "ingredients": ingredients,
            "steps": steps,
            "tags": tags[:10],  # Limit to 10 tags
            "mealTypes": list(set(meal_types)),
            "nutrition": nutrition if nutrition else None,
            "notes": notes or jsonld.get('description', ''),
            "location": location,
            "media": {
                "sourceUrl": url,
            }
        }
        
        # Get author
        if jsonld.get('author'):
            author = jsonld['author']
            if isinstance(author, dict):
                recipe['author'] = author.get('name', '')
            elif isinstance(author, str):
                recipe['author'] = author
            elif isinstance(author, list) and author:
                first_author = author[0]
                if isinstance(first_author, dict):
                    recipe['author'] = first_author.get('name', '')
                else:
                    recipe['author'] = str(first_author)
        
        return recipe
    
    @staticmethod
    def _parse_ingredient_string(ing_str: str) -> dict:
        """Parse an ingredient string into structured format."""
        # Simple parsing - AI extraction does better for complex cases
        # Format: "2 cups all-purpose flour, sifted"
        ing_str = ing_str.strip()
        
        # Try to extract quantity and unit
        quantity_pattern = r'^([\d\s\.\-\/]+(?:\s*to\s*[\d\.\-\/]+)?)\s*'
        unit_pattern = r'(cup|cups|tablespoon|tablespoons|tbsp|teaspoon|teaspoons|tsp|pound|pounds|lb|lbs|ounce|ounces|oz|gram|grams|g|kg|ml|liter|liters|l|piece|pieces|clove|cloves|can|cans|package|packages|bunch|bunches|pinch|dash|handful|stick|sticks)s?\s+'
        
        quantity = ""
        unit = ""
        name = ing_str
        
        # Extract quantity
        qty_match = re.match(quantity_pattern, ing_str, re.IGNORECASE)
        if qty_match:
            quantity = qty_match.group(1).strip()
            remaining = ing_str[qty_match.end():].strip()
            
            # Extract unit
            unit_match = re.match(unit_pattern, remaining, re.IGNORECASE)
            if unit_match:
                unit = unit_match.group(1).strip()
                name = remaining[unit_match.end():].strip()
            else:
                name = remaining
        
        # Clean up name - remove trailing commas and notes in parentheses for name
        name_clean = re.sub(r'\s*\([^)]*\)\s*$', '', name)
        name_clean = name_clean.rstrip(',').strip()
        
        return {
            "name": name_clean or ing_str,
            "quantity": quantity,
            "unit": unit,
            "notes": "",
            "original": ing_str,
        }
    
    @staticmethod
    def _parse_iso_duration(duration: str) -> str:
        """Parse ISO 8601 duration (PT30M) to human readable (30 min)."""
        if not duration:
            return ""
        
        # Already human readable
        if not duration.startswith('P'):
            return duration
        
        try:
            # Parse ISO 8601 duration
            pattern = r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
            match = re.match(pattern, duration)
            if not match:
                return duration
            
            days, hours, minutes, seconds = match.groups()
            parts = []
            
            if days:
                parts.append(f"{days} day{'s' if int(days) > 1 else ''}")
            if hours:
                parts.append(f"{hours} hour{'s' if int(hours) > 1 else ''}")
            if minutes:
                parts.append(f"{minutes} min")
            if seconds and not minutes:  # Only show seconds if no minutes
                parts.append(f"{seconds} sec")
            
            return ' '.join(parts) if parts else duration
        except:
            return duration
    
    @classmethod
    def _extract_main_content(cls, html: str) -> Optional[str]:
        """Extract main text content from HTML using trafilatura."""
        try:
            if trafilatura:
                # trafilatura is excellent at extracting main content
                content = trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=True,
                    no_fallback=False,
                )
                return content
            
            # Fallback: Basic BeautifulSoup extraction
            soup = BeautifulSoup(html, 'lxml')
            
            # Remove unwanted elements
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
                tag.decompose()
            
            # Try to find main content area
            main = soup.find('main') or soup.find('article') or soup.find(class_=re.compile(r'recipe|content|post'))
            if main:
                return main.get_text(separator='\n', strip=True)
            
            # Fallback to body
            body = soup.find('body')
            if body:
                return body.get_text(separator='\n', strip=True)
            
            return soup.get_text(separator='\n', strip=True)
            
        except Exception as e:
            print(f"❌ Content extraction error: {e}")
            return None
    
    @classmethod
    def _extract_thumbnail(cls, html: str, jsonld: Optional[dict]) -> Optional[str]:
        """Extract thumbnail/image URL from page."""
        # Try JSON-LD first
        if jsonld:
            image = jsonld.get('image')
            if image:
                if isinstance(image, str):
                    return image
                elif isinstance(image, list) and image:
                    first_img = image[0]
                    if isinstance(first_img, str):
                        return first_img
                    elif isinstance(first_img, dict):
                        return first_img.get('url')
                elif isinstance(image, dict):
                    return image.get('url')
        
        # Try Open Graph image
        soup = BeautifulSoup(html, 'lxml')
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            return og_image['content']
        
        # Try Twitter image
        twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
        if twitter_image and twitter_image.get('content'):
            return twitter_image['content']
        
        return None
    
    @classmethod
    async def _ai_extract_recipe(
        cls,
        content: str,
        url: str,
        location: str = "",
        notes: str = "",
    ) -> Optional[dict]:
        """Use AI to extract recipe from text content."""
        from app.services.llm_client import llm_service
        
        prompt = f"""Extract the recipe from this webpage content. Return a JSON object with the recipe details.

URL: {url}

WEBPAGE CONTENT:
{content[:8000]}

Return a JSON object with these fields:
{{
  "title": "Recipe title",
  "description": "Brief description",
  "servings": number or null,
  "times": {{
    "prep": "prep time string",
    "cook": "cook time string", 
    "total": "total time string"
  }},
  "ingredients": [
    {{"name": "ingredient name", "quantity": "amount", "unit": "unit", "notes": "optional notes", "original": "full original text"}}
  ],
  "steps": [
    {{"text": "step instruction"}}
  ],
  "tags": ["tag1", "tag2"],
  "mealTypes": ["breakfast", "lunch", "dinner", "snack", "dessert"],
  "nutrition": {{
    "perServing": {{"calories": number, "protein": number, "carbs": number, "fat": number}}
  }},
  "notes": "{notes or 'any recipe notes'}",
  "location": "{location}"
}}

IMPORTANT:
- Extract ALL ingredients mentioned
- Extract ALL steps in order
- Use reasonable estimates for times if not explicitly stated
- Set fields to null if information is not available
- Only return valid JSON, no explanation"""

        try:
            result = await llm_service.generate_json(prompt)
            if result and isinstance(result, dict) and result.get('title'):
                # Add source URL to media
                result['media'] = {'sourceUrl': url}
                return result
            return None
        except Exception as e:
            print(f"❌ AI extraction error: {e}")
            return None


# Singleton instance
website_service = WebsiteService()
