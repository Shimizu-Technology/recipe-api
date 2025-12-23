"""
Website recipe extraction service.

Extracts recipes from recipe blog/website URLs using:
1. JSON-LD structured data (Schema.org Recipe) - preferred, very reliable
2. AI extraction from main content - fallback for sites without structured data
"""

import httpx
import json
import re
import sentry_sdk
from dataclasses import dataclass
from typing import Optional, Any
from urllib.parse import urlparse
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
    has_audio_transcript: bool = False  # Always False for websites (no audio)
    error: Optional[str] = None
    error_type: Optional[str] = None  # For Sentry categorization


# User-friendly error messages
ERROR_MESSAGES = {
    "fetch_failed": "We couldn't reach this website. It may be temporarily unavailable or blocking automated access.",
    "fetch_403": "This website is blocking our access. We've been notified and will work on adding support.",
    "fetch_404": "This page doesn't exist or has been moved.",
    "fetch_timeout": "The website took too long to respond. Please try again later.",
    "no_content": "We couldn't find recipe content on this page. Make sure the URL links directly to a recipe.",
    "ai_failed": "We couldn't extract the recipe from this page. The format may be unusual - we've been notified.",
    "parse_error": "Something went wrong while processing this recipe. We've been notified.",
}


def _get_domain(url: str) -> str:
    """Extract domain from URL for logging."""
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower().replace("www.", "")
    except:
        return "unknown"


def _log_extraction_failure(
    url: str,
    error_type: str,
    error_detail: str,
    extraction_method: str = "unknown",
    extra_context: dict = None,
):
    """Log extraction failure to Sentry with rich context."""
    domain = _get_domain(url)
    
    sentry_sdk.capture_message(
        f"Website extraction failed: {error_type}",
        level="warning",
        extras={
            "url": url,
            "domain": domain,
            "error_type": error_type,
            "error_detail": error_detail,
            "extraction_method": extraction_method,
            **(extra_context or {}),
        },
        tags={
            "feature": "website_extraction",
            "error_type": error_type,
            "domain": domain,
        }
    )
    print(f"📡 Logged to Sentry: {error_type} for {domain}")


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
    
    # Browser-like headers to avoid being blocked
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Don't set Accept-Encoding manually - let httpx handle it with its defaults
        # Setting "br" (Brotli) can cause issues if the brotli package isn't installed
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
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
        domain = _get_domain(url)
        
        try:
            # Fetch the HTML
            html, fetch_error = await cls._fetch_html_with_error(url)
            if not html:
                error_type = fetch_error or "fetch_failed"
                user_message = ERROR_MESSAGES.get(error_type, ERROR_MESSAGES["fetch_failed"])
                
                _log_extraction_failure(
                    url=url,
                    error_type=error_type,
                    error_detail=f"Failed to fetch HTML from {domain}",
                    extraction_method="fetch",
                )
                
                return WebsiteExtractionResult(
                    success=False,
                    error=user_message,
                    error_type=error_type,
                )
            
            # Try JSON-LD first (most reliable)
            jsonld_recipe = cls._extract_jsonld_recipe(html, url)
            if jsonld_recipe:
                print(f"✅ Found JSON-LD recipe schema")
                # Also extract ingredient sections from HTML (JSON-LD often flattens them)
                ingredient_groups = cls._extract_ingredient_groups_from_html(html)
                recipe = cls._convert_jsonld_to_recipe(jsonld_recipe, url, location, notes, ingredient_groups)
                
                # Check if JSON-LD actually has ingredients/steps - if not, fall back to AI
                has_ingredients = len(recipe.get('ingredients', [])) > 0
                has_steps = len(recipe.get('steps', [])) > 0
                
                if has_ingredients and has_steps:
                    thumbnail = cls._extract_thumbnail(html, jsonld_recipe)
                    return WebsiteExtractionResult(
                        success=True,
                        recipe=recipe,
                        raw_text=json.dumps(jsonld_recipe, indent=2),
                        thumbnail_url=thumbnail,
                        extraction_method="website-jsonld",
                        extraction_quality="high",
                    )
                else:
                    print(f"⚠️ JSON-LD missing ingredients/steps, falling back to AI")
                    # Keep the thumbnail from JSON-LD for later
                    jsonld_thumbnail = cls._extract_thumbnail(html, jsonld_recipe)
            else:
                print(f"⚠️ No JSON-LD recipe found, using AI extraction")
            
            # Fallback: Extract main content and use AI
            print(f"📄 Extracting main content for AI...")
            main_content = cls._extract_main_content(html)
            if not main_content or len(main_content) < 100:
                _log_extraction_failure(
                    url=url,
                    error_type="no_content",
                    error_detail=f"Could not extract recipe content (only {len(main_content) if main_content else 0} chars)",
                    extraction_method="content_extraction",
                    extra_context={"content_length": len(main_content) if main_content else 0},
                )
                return WebsiteExtractionResult(
                    success=False,
                    error=ERROR_MESSAGES["no_content"],
                    error_type="no_content",
                )
            
            # Use AI to extract recipe
            recipe = await cls._ai_extract_recipe(main_content, url, location, notes)
            
            if not recipe:
                _log_extraction_failure(
                    url=url,
                    error_type="ai_failed",
                    error_detail="AI could not extract recipe from page content",
                    extraction_method="website-ai",
                    extra_context={"content_length": len(main_content)},
                )
                return WebsiteExtractionResult(
                    success=False,
                    error=ERROR_MESSAGES["ai_failed"],
                    error_type="ai_failed",
                )
            
            # Use thumbnail from JSON-LD if we had incomplete JSON-LD, otherwise extract from HTML
            try:
                thumbnail = jsonld_thumbnail
            except NameError:
                thumbnail = cls._extract_thumbnail(html, None)
            
            print(f"✅ Successfully extracted recipe via AI from {domain}")
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
            
            # Log unexpected errors to Sentry with full context
            sentry_sdk.capture_exception(e)
            _log_extraction_failure(
                url=url,
                error_type="parse_error",
                error_detail=str(e),
                extraction_method="unknown",
                extra_context={"exception_type": type(e).__name__},
            )
            
            return WebsiteExtractionResult(
                success=False,
                error=ERROR_MESSAGES["parse_error"],
                error_type="parse_error",
            )
    
    @classmethod
    async def _fetch_html_with_error(cls, url: str) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch HTML content from URL with error type.
        
        Returns: (html_content, error_type)
        - If successful: (html, None)
        - If failed: (None, error_type)
        """
        try:
            # Add referer header based on domain
            parsed = urlparse(url)
            headers = cls.HEADERS.copy()
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
            
            async with httpx.AsyncClient(
                follow_redirects=True, 
                timeout=30.0,
                http2=True  # Some sites prefer HTTP/2
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.text, None
                
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            
            # If 403, try without some security headers (some sites don't like them)
            if status == 403:
                print(f"⚠️ Got 403, retrying with minimal headers...")
                try:
                    minimal_headers = {
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                        response = await client.get(url, headers=minimal_headers)
                        response.raise_for_status()
                        return response.text, None
                except Exception as e2:
                    print(f"❌ Retry also failed: {e2}")
                    return None, "fetch_403"
            
            if status == 404:
                print(f"❌ Page not found: {url}")
                return None, "fetch_404"
            
            print(f"❌ Failed to fetch {url}: HTTP {status}")
            return None, "fetch_failed"
            
        except httpx.TimeoutException:
            print(f"❌ Timeout fetching {url}")
            return None, "fetch_timeout"
            
        except Exception as e:
            print(f"❌ Failed to fetch {url}: {e}")
            return None, "fetch_failed"
    
    @classmethod
    async def _fetch_html(cls, url: str) -> Optional[str]:
        """Fetch HTML content from URL (legacy method)."""
        html, _ = await cls._fetch_html_with_error(url)
        return html
    
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
    def _extract_ingredient_groups_from_html(cls, html: str) -> list:
        """
        Extract ingredient section groups from HTML.
        Returns list of dicts: [{"name": "Sauce", "ingredients": ["1 cup tomatoes", ...]}, ...]
        Many sites have sections (like "For the sauce:", "For the pasta:") that JSON-LD flattens.
        """
        try:
            soup = BeautifulSoup(html, 'lxml')
            groups = []
            
            # Look for common ingredient group patterns
            # Pattern 1: WPRM plugin (Budget Bytes, many WordPress sites)
            wprm_groups = soup.find_all(class_='wprm-recipe-ingredient-group')
            if wprm_groups:
                for group in wprm_groups:
                    name_elem = group.find(class_='wprm-recipe-group-name')
                    name = name_elem.get_text(strip=True) if name_elem else ""
                    
                    ingredients = []
                    for li in group.find_all('li', class_=lambda x: x and 'ingredient' in str(x).lower()):
                        ing_text = li.get_text(separator=' ', strip=True)
                        if ing_text:
                            ingredients.append(ing_text)
                    
                    if ingredients:
                        groups.append({"name": name, "ingredients": ingredients})
                
                if groups:
                    print(f"📋 Found {len(groups)} ingredient groups from WPRM")
                    return groups
            
            # Pattern 2: Tasty Recipes plugin
            tasty_container = soup.find(class_='tasty-recipes-ingredients')
            if tasty_container:
                # Look for h4/h5 section headers that are NOT inside list items
                # (strong tags are often used for ingredient highlighting, not sections)
                for header in tasty_container.find_all(['h4', 'h5'], recursive=True):
                    # Skip if this header is inside a list item (it's just formatting)
                    if header.find_parent('li'):
                        continue
                    
                    name = header.get_text(strip=True)
                    # Skip if it's the main "Ingredients" header
                    if name.lower() in ['ingredients', 'ingredient']:
                        continue
                        
                    next_ul = header.find_next_sibling('ul') or header.find_next('ul')
                    if next_ul:
                        ingredients = [li.get_text(separator=' ', strip=True) for li in next_ul.find_all('li') if li.get_text(strip=True)]
                        if ingredients:
                            groups.append({"name": name, "ingredients": ingredients})
                
                if groups:
                    print(f"📋 Found {len(groups)} ingredient groups from Tasty Recipes")
                    return groups
            
            # Pattern 3: Hearst Media (Delish, Good Housekeeping, etc.) - ingredients-body with section divs
            hearst_container = soup.find(class_=lambda x: x and 'ingredients-body' in str(x).lower())
            if hearst_container:
                # Each direct div child is a section with h3 header + ul.ingredient-lists
                for section in hearst_container.find_all('div', recursive=False):
                    h3 = section.find('h3')
                    name = h3.get_text(strip=True) if h3 else ""
                    
                    # Skip if header is just "Ingredients"
                    if name.lower() in ['ingredients', 'ingredient']:
                        continue
                    
                    # Find ingredient list
                    ul = section.find('ul', class_=lambda x: x and 'ingredient' in str(x).lower())
                    if ul:
                        ingredients = []
                        for li in ul.find_all('li'):
                            ing_text = li.get_text(separator=' ', strip=True)
                            ing_text = ' '.join(ing_text.split())  # Clean whitespace
                            if ing_text:
                                ingredients.append(ing_text)
                        
                        if ingredients:
                            groups.append({"name": name, "ingredients": ingredients})
                
                if len(groups) > 1:  # Only return if we found actual sections
                    print(f"📋 Found {len(groups)} ingredient groups from Hearst Media")
                    return groups
            
            # Pattern 4: Generic - look for ingredient container with headers
            ing_container = soup.find(class_=lambda x: x and 'ingredient' in str(x).lower() and 'container' in str(x).lower())
            if ing_container:
                current_group = {"name": "", "ingredients": []}
                for elem in ing_container.descendants:
                    if elem.name in ['h3', 'h4', 'h5']:
                        # New section header
                        if current_group["ingredients"]:
                            groups.append(current_group)
                        current_group = {"name": elem.get_text(strip=True), "ingredients": []}
                    elif elem.name == 'li' and 'ingredient' in str(elem.get('class', [])).lower():
                        ing_text = elem.get_text(separator=' ', strip=True)
                        if ing_text:
                            current_group["ingredients"].append(ing_text)
                
                if current_group["ingredients"]:
                    groups.append(current_group)
                
                if groups:
                    print(f"📋 Found {len(groups)} ingredient groups from generic parsing")
                    return groups
            
            return []
            
        except Exception as e:
            print(f"⚠️ Could not extract ingredient groups: {e}")
            return []
    
    @classmethod
    def _convert_jsonld_to_recipe(
        cls,
        jsonld: dict,
        url: str,
        location: str = "",
        notes: str = "",
        ingredient_groups: list = None,
    ) -> dict:
        """Convert JSON-LD Recipe schema to our recipe format."""
        # Parse ingredients from JSON-LD
        ingredients = []
        raw_ingredients = jsonld.get('recipeIngredient', [])
        for ing in raw_ingredients:
            if isinstance(ing, str):
                parsed = cls._parse_ingredient_string(ing)
                ingredients.append(parsed)
        
        # Parse instructions/steps - normalize to plain strings
        steps = []
        raw_instructions = jsonld.get('recipeInstructions', [])
        for instruction in raw_instructions:
            if isinstance(instruction, str):
                steps.append(instruction)
            elif isinstance(instruction, dict):
                # HowToStep format
                text = instruction.get('text', instruction.get('name', ''))
                if text:
                    steps.append(text)
            elif isinstance(instruction, list):
                # Nested sections
                for sub in instruction:
                    if isinstance(sub, dict):
                        text = sub.get('text', sub.get('name', ''))
                        if text:
                            steps.append(text)
        
        # If we only have 1-2 steps but they contain numbered patterns, split them
        # This handles sites like Half Baked Harvest that combine all steps into one
        if len(steps) <= 2:
            combined_text = ' '.join(steps)
            # Check if text contains numbered steps like "1. Do this 2. Do that"
            if re.search(r'\d+\.\s+\w', combined_text):
                # Split by numbered pattern - handle cases with/without space before number
                # Pattern matches: start of string, whitespace, or period/punctuation before a number
                split_steps = re.split(r'(?:^|(?<=\s)|(?<=[.!?]))(\d+)\.\s+', combined_text)
                new_steps = []
                i = 1  # Start at 1 to skip any text before "1."
                while i < len(split_steps):
                    if split_steps[i].isdigit() and i + 1 < len(split_steps):
                        step_text = split_steps[i + 1].strip()
                        if step_text:
                            new_steps.append(step_text)
                        i += 2
                    else:
                        i += 1
                
                if len(new_steps) > len(steps):
                    print(f"📋 Split {len(steps)} combined step(s) into {len(new_steps)} individual steps")
                    steps = new_steps
        
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
                nutrition = {"perServing": per_serving, "total": {}}
        
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
        
        # Parse tags/keywords - handle various separators (comma, semicolon, double semicolon)
        tags = []
        if jsonld.get('keywords'):
            keywords = jsonld['keywords']
            if isinstance(keywords, str):
                # Replace common separators with comma, then split
                normalized = keywords.replace(';;', ',').replace(';', ',')
                tags = [k.strip().lower() for k in normalized.split(',') if k.strip()]
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
        
        # Build components - use ingredient groups if available
        components = []
        if ingredient_groups and len(ingredient_groups) > 1:
            # Multiple sections found - create a component for each
            for group in ingredient_groups:
                group_ingredients = []
                for ing_text in group.get("ingredients", []):
                    parsed = cls._parse_ingredient_string(ing_text)
                    group_ingredients.append(parsed)
                
                components.append({
                    "name": group.get("name", ""),
                    "ingredients": group_ingredients,
                    "steps": [],  # Steps usually aren't grouped the same way
                })
            
            # Add all steps to the FIRST component (the main recipe)
            # Secondary components like "For Rolling" or "For Topping" are just extra ingredients
            if components:
                components[0]["steps"] = steps
            
            print(f"📋 Created {len(components)} recipe components from HTML sections")
        else:
            # Single component (default behavior)
            components = [{
                "name": "",
                "ingredients": ingredients,
                "steps": steps,
            }]
        
        # Build recipe object matching the expected schema
        recipe = {
            "title": jsonld.get('name', 'Untitled Recipe'),
            "description": jsonld.get('description', ''),
            "servings": servings,
            "times": times if times else {},
            "ingredients": ingredients,  # Keep flat list for backwards compatibility
            "steps": steps,
            "components": components,
            "tags": tags[:10],  # Limit to 10 tags
            "mealTypes": list(set(meal_types)),
            "nutrition": nutrition if nutrition else {"perServing": {}, "total": {}},  # Schema requires both
            "notes": notes or jsonld.get('description', ''),
            "location": location,
            "sourceUrl": url,  # Required by RecipeExtracted schema
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
        
        # Clean up common artifacts from HTML
        ing_str = ing_str.replace('▢', '').replace('□', '').strip()
        # Remove price info like ($0.20)
        ing_str = re.sub(r'\s*\(\$[\d.]+\)\s*$', '', ing_str)
        
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
        """Extract main text content from HTML, prioritizing recipe content."""
        try:
            soup = BeautifulSoup(html, 'lxml')
            
            # Remove unwanted elements
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript']):
                tag.decompose()
            
            # First, try to find recipe-specific content areas
            content_parts = []
            
            # Look for ingredients - prioritize lists with ingredient-related classes
            ingredients_list = soup.find(['ul', 'ol'], class_=re.compile(r'ingredient', re.I))
            if ingredients_list:
                content_parts.append("INGREDIENTS:\n" + ingredients_list.get_text(separator='\n', strip=True))
            else:
                # Fallback: find any element with ingredient class
                ingredients_section = soup.find(class_=re.compile(r'ingredient', re.I)) or \
                                      soup.find(id=re.compile(r'ingredient', re.I))
                if ingredients_section:
                    content_parts.append("INGREDIENTS:\n" + ingredients_section.get_text(separator='\n', strip=True))
            
            # Look for instructions/steps - prioritize ordered lists with step-related classes
            steps_list = soup.find('ol', class_=re.compile(r'prep|step|instruction|direction|method', re.I))
            if steps_list:
                content_parts.append("INSTRUCTIONS:\n" + steps_list.get_text(separator='\n', strip=True))
            else:
                # Fallback: find any element with instruction/step class
                directions_section = soup.find(class_=re.compile(r'direction|instruction|preparation', re.I)) or \
                                     soup.find(id=re.compile(r'direction|instruction|step', re.I))
                if directions_section:
                    content_parts.append("INSTRUCTIONS:\n" + directions_section.get_text(separator='\n', strip=True))
            
            # Also look for recipe title and description
            title = soup.find('h1')
            if title:
                content_parts.insert(0, f"TITLE: {title.get_text(strip=True)}")
            
            # Look for servings/yield
            servings = soup.find(string=re.compile(r'(serves?|yields?|makes?)\s*:?\s*\d+', re.I))
            if servings:
                content_parts.append(f"SERVINGS: {servings.strip()}")
            
            # If we found specific sections, use them
            if len(content_parts) >= 2:  # At least title + one section
                combined = '\n\n'.join(content_parts)
                if len(combined) > 300:  # Lower threshold since we're being more targeted
                    print(f"📄 Extracted recipe sections: {len(combined)} characters")
                    return combined
            
            # Fallback: Try to find main content area
            main = soup.find('main') or soup.find('article') or \
                   soup.find(class_=re.compile(r'recipe|content|post', re.I))
            if main:
                text = main.get_text(separator='\n', strip=True)
                if len(text) > 500:
                    print(f"📄 Extracted main content: {len(text)} characters")
                    return text
            
            # Try trafilatura as another option
            if trafilatura:
                traf_content = trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=True,
                    no_fallback=False,
                )
                if traf_content and len(traf_content) > 500:
                    print(f"📄 Extracted via trafilatura: {len(traf_content)} characters")
                    return traf_content
            
            # Final fallback: body text
            body = soup.find('body')
            if body:
                text = body.get_text(separator='\n', strip=True)
                print(f"📄 Extracted body text: {len(text)} characters")
                return text
            
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
  "components": [
    {{
      "name": "Component/section name (e.g., 'Chicken Marinade', 'Sauce', 'Main Dish') or empty string if no sections",
      "ingredients": [
        {{"name": "ingredient name", "quantity": "amount", "unit": "unit", "notes": "optional notes", "original": "full original text"}}
      ],
      "steps": ["step 1 text", "step 2 text"]
    }}
  ],
  "tags": ["tag1", "tag2"],
  "mealTypes": ["breakfast", "lunch", "dinner", "snack", "dessert"],
  "nutrition": {{
    "perServing": {{"calories": number, "protein": number, "carbs": number, "fat": number, "fiber": number, "sugar": number, "sodium": number}}
  }},
  "notes": "{notes or 'any recipe notes'}",
  "location": "{location}"
}}

IMPORTANT:
- If the recipe has SECTIONS (like "Chicken Marinade:", "Sauce:", "For the filling:"), create SEPARATE components for each section
- Each component should have its own name, ingredients, and steps
- If there are no clear sections, use a single component with an empty name
- Extract ALL ingredients mentioned, grouped by their section
- Extract ALL steps in order, grouped by their section
- Use reasonable estimates for times if not explicitly stated
- NUTRITION: If the website provides nutrition info, use it. If NOT provided, ESTIMATE the nutrition per serving based on the ingredients and typical values. Always provide nutrition estimates - never leave it empty.
- Only return valid JSON, no explanation"""

        try:
            result = await llm_service.generate_json(prompt)
            if result and isinstance(result, dict) and result.get('title'):
                # Validate that we have REAL recipe content, not placeholder garbage
                title = result.get('title', '').lower().strip()
                placeholder_titles = ['recipe title', 'untitled', 'recipe', 'title', 'no title', 'unknown']
                
                if title in placeholder_titles:
                    print(f"⚠️ AI returned placeholder title: '{result.get('title')}' - rejecting")
                    return None
                
                # Check for actual ingredients or steps
                components = result.get('components', [])
                flat_ingredients = result.get('ingredients', [])
                flat_steps = result.get('steps', [])
                
                total_ingredients = sum(len(c.get('ingredients', [])) for c in components) + len(flat_ingredients)
                total_steps = sum(len(c.get('steps', [])) for c in components) + len(flat_steps)
                
                # Must have at least 1 ingredient OR 1 step to be considered a valid recipe
                if total_ingredients == 0 and total_steps == 0:
                    print(f"⚠️ AI returned recipe with no ingredients and no steps - rejecting")
                    return None
                
                # Add required fields to match schema
                result['sourceUrl'] = url
                result['media'] = {'sourceUrl': url}
                
                # Process components - normalize steps within each component
                components = result.get('components', [])
                all_ingredients = []
                all_steps = []
                
                for comp in components:
                    # Normalize steps within component
                    comp_steps = comp.get('steps', [])
                    normalized_steps = []
                    for step in comp_steps:
                        if isinstance(step, dict) and 'text' in step:
                            normalized_steps.append(step['text'])
                        elif isinstance(step, str):
                            normalized_steps.append(step)
                    comp['steps'] = normalized_steps
                    
                    # Ensure component has name (empty string is ok)
                    if 'name' not in comp:
                        comp['name'] = ''
                    
                    # Collect all ingredients/steps for legacy fields
                    all_ingredients.extend(comp.get('ingredients', []))
                    all_steps.extend(normalized_steps)
                
                # If no components were returned, create one from flat fields
                if not components:
                    ingredients = result.get('ingredients', [])
                    steps = result.get('steps', [])
                    if isinstance(steps, list):
                        steps = [s['text'] if isinstance(s, dict) and 'text' in s else s for s in steps]
                    components = [{
                        "name": "",
                        "ingredients": ingredients,
                        "steps": steps if isinstance(steps, list) else [],
                    }]
                    all_ingredients = ingredients
                    all_steps = steps if isinstance(steps, list) else []
                
                result['components'] = components
                # Also set legacy flat fields for compatibility
                result['ingredients'] = all_ingredients
                result['steps'] = all_steps
                
                # Ensure nutrition has both perServing and total (schema requires both)
                nutrition = result.get('nutrition') or {}
                per_serving = nutrition.get('perServing') or {}
                # Clean None values from perServing
                per_serving = {k: v for k, v in per_serving.items() if v is not None}
                result['nutrition'] = {
                    "perServing": per_serving,
                    "total": {}  # Schema requires this field
                }
                
                # Ensure times is a dict, not None  
                if not result.get('times'):
                    result['times'] = {}
                    
                print(f"✅ AI extracted {len(components)} component(s) with {len(all_ingredients)} ingredients and {len(all_steps)} steps")
                return result
            return None
        except Exception as e:
            print(f"❌ AI extraction error: {e}")
            return None


# Singleton instance
website_service = WebsiteService()
