"""Mealie MCP Server - Main server implementation."""

import os
import json
import asyncio
import base64
import binascii
import secrets
import httpx
import logging
from datetime import date, timedelta
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from .client import MealieClient


INSTRUCTIONS = """
    Mealie MCP Server for recipe and meal planning management.

    Use these tools to:
    - Search and retrieve recipes from Mealie
    - Create new recipes with ingredients and instructions
    - Update existing recipes
    - Manage meal plans (create, view, modify)
    - Get today's date for meal planning
    - Generate and upload recipe images
    - View shopping lists and label their items
    - Search and manage foods and labels

    IMPORTANT - Ingredient and food reuse:
    - The system automatically searches for existing foods/units before creating new ones.
    - Use search_foods to find existing ingredients and their labels.
    - Use list_labels to see available food categories (e.g., Produce, Dairy, Meat).

    IMPORTANT - Labeling foods:
    - After creating a recipe, use get_shopping_lists and get_shopping_list to review items.
    - Use list_labels to get available labels, then bulk_assign_food_labels to assign
      labels to unlabeled foods. Labels control how shopping list items are grouped in Mealie.
    """


class StaticTokenVerifier(TokenVerifier):
    """Bearer-token verifier that accepts a single statically-configured token."""

    def __init__(self, expected: str):
        self._expected = expected

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self._expected):
            return AccessToken(token=token, client_id="static", scopes=["mealie"])
        return None


def _build_mcp() -> FastMCP:
    auth_token = os.environ.get("MCP_AUTH_TOKEN")
    public_url = os.environ.get("MCP_PUBLIC_URL")

    kwargs: dict = {
        "name": "mealie",
        "host": os.environ.get("MCP_HOST", "0.0.0.0"),
        "port": int(os.environ.get("MCP_PORT", "8000")),
        "instructions": INSTRUCTIONS,
    }

    if auth_token:
        if not public_url:
            raise ValueError("MCP_PUBLIC_URL must be set when MCP_AUTH_TOKEN is set")
        kwargs["token_verifier"] = StaticTokenVerifier(auth_token)
        kwargs["auth"] = AuthSettings(
            issuer_url=public_url,
            resource_server_url=public_url,
            required_scopes=["mealie"],
        )
        logger.info("MCP server starting with bearer-token auth enabled")
    else:
        logger.info("MCP server starting without authentication")

    return FastMCP(**kwargs)


mcp = _build_mcp()


def get_client() -> MealieClient:
    """Get a configured Mealie client."""
    base_url = os.environ.get("MEALIE_URL")
    api_token = os.environ.get("MEALIE_API_TOKEN")
    if not base_url or not api_token:
        raise ValueError("MEALIE_URL and MEALIE_API_TOKEN environment variables must be set")
    return MealieClient(base_url, api_token)


@mcp.tool()
async def search_recipes(
    query: Optional[str] = None,
    categories: Optional[str] = None,
    tags: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> str:
    """
    Search for recipes in Mealie.

    Args:
        query: Search term to filter recipes by name or description
        categories: Comma-separated list of category names to filter by
        tags: Comma-separated list of tag names to filter by
        page: Page number for pagination (default: 1)
        per_page: Number of results per page (default: 20)

    Returns:
        List of matching recipes with their basic information
    """
    client = get_client()
    category_list = [c.strip() for c in categories.split(",")] if categories else None
    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    result = await client.get_recipes(
        search=query,
        page=page,
        per_page=per_page,
        categories=category_list,
        tags=tag_list,
    )

    recipes = []
    for item in result.get("items", []):
        recipes.append({
            "id": item.get("id"),
            "slug": item.get("slug"),
            "name": item.get("name"),
            "description": item.get("description"),
            "rating": item.get("rating"),
            "total_time": item.get("totalTime"),
        })

    return json.dumps({
        "recipes": recipes,
        "total": result.get("total", 0),
        "page": page,
        "per_page": per_page,
    }, indent=2)


@mcp.tool()
async def get_recipe(slug: str) -> str:
    """
    Get detailed information about a specific recipe.

    Args:
        slug: The unique slug identifier for the recipe

    Returns:
        Complete recipe details including ingredients, instructions, and metadata
    """
    client = get_client()
    recipe = await client.get_recipe(slug)

    ingredients = []
    for ing in recipe.get("recipeIngredient", []):
        ingredients.append({
            "note": ing.get("note"),
            "quantity": ing.get("quantity"),
            "unit": ing.get("unit", {}).get("name") if ing.get("unit") else None,
            "food": ing.get("food", {}).get("name") if ing.get("food") else None,
        })

    instructions = []
    for inst in recipe.get("recipeInstructions", []):
        instructions.append({
            "text": inst.get("text"),
        })

    return json.dumps({
        "id": recipe.get("id"),
        "slug": recipe.get("slug"),
        "name": recipe.get("name"),
        "description": recipe.get("description"),
        "ingredients": ingredients,
        "instructions": instructions,
        "prep_time": recipe.get("prepTime"),
        "cook_time": recipe.get("performTime"),
        "total_time": recipe.get("totalTime"),
        "servings": recipe.get("recipeYield"),
        "rating": recipe.get("rating"),
        "categories": [c.get("name") for c in recipe.get("recipeCategory", [])],
        "tags": [t.get("name") for t in recipe.get("tags", [])],
        "notes": [n.get("text") for n in recipe.get("notes", [])],
    }, indent=2)


def _ensure_list(value) -> list:
    """Parse a value that should be a list but might be a JSON string."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError(f"Expected array, got: {type(parsed).__name__}")
        return parsed
    raise ValueError(f"Expected array, got: {type(value).__name__}")


def _parse_instruction(inst) -> dict:
    """Parse an instruction into Mealie format. Accepts string or {"text": "..."}."""
    if isinstance(inst, str):
        return {"text": inst, "ingredientReferences": []}
    if isinstance(inst, dict) and "text" in inst:
        return {"text": inst["text"], "ingredientReferences": []}
    raise ValueError(f"Instruction must be a string or object with 'text' field, got: {inst}")


async def _search_existing_unit(client: MealieClient, unit_name: str) -> Optional[dict]:
    """Search for an existing unit by name (case-insensitive exact match)."""
    result = await client.get_units(search=unit_name, page=1, per_page=50)
    items = result.get("items", [])
    unit_name_lower = unit_name.lower().strip()
    for item in items:
        if item.get("name", "").lower().strip() == unit_name_lower:
            return item
        if item.get("abbreviation", "").lower().strip() == unit_name_lower:
            return item
    return None


async def _ensure_unit(client: MealieClient, unit_data: Optional[dict]) -> Optional[dict]:
    """Ensure a unit exists in the database, reusing existing or creating if necessary."""
    if unit_data is None:
        return None
    if unit_data.get("id"):
        return {"id": unit_data["id"], "name": unit_data["name"]}

    unit_name = unit_data["name"]
    existing = await _search_existing_unit(client, unit_name)
    if existing:
        return {"id": existing["id"], "name": existing["name"]}

    result = await client.create_unit(unit_name)
    return {"id": result["id"], "name": result["name"]}


def _normalize_label_name(name: str) -> str:
    """Lowercase, strip whitespace, drop leading non-alphanumeric chars (emoji + punctuation)."""
    if not name:
        return ""
    lowered = name.strip().lower()
    i = 0
    while i < len(lowered) and not lowered[i].isalnum():
        i += 1
    return lowered[i:].strip()


def _build_label_index(labels: list) -> dict:
    """Map normalized label name -> label dict, for both raw and emoji-stripped variants."""
    index: dict = {}
    for lbl in labels:
        name = lbl.get("name") or ""
        for variant in {name.strip().lower(), _normalize_label_name(name)}:
            if variant:
                index.setdefault(variant, lbl)
    return index


def _resolve_label(label_name: str, index: dict) -> Optional[dict]:
    """Find a label by name: try exact, normalized, then prefix match against normalized keys."""
    if not label_name:
        return None
    raw = label_name.strip().lower()
    if raw in index:
        return index[raw]
    norm = _normalize_label_name(label_name)
    if norm and norm in index:
        return index[norm]
    if norm:
        for key, lbl in index.items():
            if norm in key or key in norm:
                return lbl
    return None


async def _search_existing_food(client: MealieClient, food_name: str) -> Optional[dict]:
    """Search for an existing food by name (case-insensitive exact match)."""
    result = await client.get_foods(search=food_name, page=1, per_page=50)
    items = result.get("items", [])
    food_name_lower = food_name.lower().strip()
    for item in items:
        if item.get("name", "").lower().strip() == food_name_lower:
            return item
        for alias in item.get("aliases", []):
            if isinstance(alias, dict):
                alias_name = alias.get("name", "")
            else:
                alias_name = str(alias)
            if alias_name.lower().strip() == food_name_lower:
                return item
    return None


async def _ensure_food(client: MealieClient, food_data: Optional[dict], label_id: Optional[str] = None) -> Optional[dict]:
    """Ensure a food exists in the database, reusing existing or creating if necessary."""
    if food_data is None:
        return None
    if food_data.get("id"):
        return {"id": food_data["id"], "name": food_data["name"]}

    food_name = food_data["name"]
    existing = await _search_existing_food(client, food_name)

    if existing:
        if label_id and not existing.get("label"):
            await client.update_food(existing["id"], {"id": existing["id"], "name": existing["name"], "labelId": label_id})
        return {"id": existing["id"], "name": existing["name"]}

    result = await client.create_food(food_name, label_id=label_id)
    return {"id": result["id"], "name": result["name"]}


PARSE_SEMAPHORE = asyncio.Semaphore(5)


async def _parse_and_prepare_ingredient(client: MealieClient, ingredient_text: str) -> dict:
    """Parse an ingredient string and ensure its unit/food exist."""
    async with PARSE_SEMAPHORE:
        logger.info(f"Parsing ingredient: {ingredient_text}")
        try:
            parsed = await client.parse_ingredient(ingredient_text)
            logger.info(f"Parsed '{ingredient_text}' -> food={parsed.get('food')}, unit={parsed.get('unit')}, qty={parsed.get('quantity')}")
            unit = await _ensure_unit(client, parsed.get("unit"))
            food = await _ensure_food(client, parsed.get("food"))
            return {
                "quantity": parsed.get("quantity"),
                "unit": unit,
                "food": food,
                "note": parsed.get("note", ""),
                "display": ingredient_text,
            }
        except Exception as e:
            logger.error(f"Error parsing ingredient '{ingredient_text}': {e}", exc_info=True)
            raise


@mcp.tool()
async def create_recipe(
    name: str,
    description: str,
    ingredients: list,
    instructions: list,
    prep_time: Optional[str] = None,
    cook_time: Optional[str] = None,
    servings: Optional[str] = None,
) -> str:
    """
    Create a new recipe in Mealie.

    Args:
        name: Recipe name (required)
        description: Brief description of the recipe
        ingredients: List of ingredient strings. Each string is parsed automatically.
                     Example: ["500g spaghetti", "2 tbsp olive oil", "1 onion, diced"]
        instructions: List of instruction strings (steps to make the recipe).
                      Example: ["Preheat oven to 350°F", "Mix dry ingredients", "Bake for 30 minutes"]
        prep_time: Preparation time (e.g., "15 minutes")
        cook_time: Cooking time (e.g., "30 minutes")
        servings: Number of servings (e.g., "4 servings")

    Returns:
        JSON with success status, recipe slug, and ID
    """
    try:
        client = get_client()

        created = await client.create_recipe(name)
        slug = created

        recipe = await client.get_recipe(slug)

        ingredient_list = _ensure_list(ingredients)
        instruction_list = _ensure_list(instructions)

        ingredient_strings = []
        for ing in ingredient_list:
            if isinstance(ing, dict):
                ing = ing.get("note") or ing.get("text") or str(ing)
            if not isinstance(ing, str):
                return json.dumps({
                    "error": f"Ingredient must be a string, got: {type(ing).__name__}",
                    "hint": "Pass ingredients as a list of strings like [\"500g flour\", \"2 eggs\"]",
                }, indent=2)
            ingredient_strings.append(ing)

        logger.info(f"Parsing {len(ingredient_strings)} ingredients in parallel...")
        try:
            parsed_ingredients = await asyncio.gather(
                *[_parse_and_prepare_ingredient(client, ing) for ing in ingredient_strings],
                return_exceptions=True
            )
            errors = [(i, r) for i, r in enumerate(parsed_ingredients) if isinstance(r, Exception)]
            if errors:
                for idx, err in errors:
                    logger.error(f"Ingredient {idx} '{ingredient_strings[idx]}' failed: {err}")
                return json.dumps({
                    "error": "Failed to parse some ingredients",
                    "details": [{"ingredient": ingredient_strings[i], "error": str(e)} for i, e in errors]
                }, indent=2)
            logger.info(f"Successfully parsed all {len(ingredient_strings)} ingredients")
        except Exception as e:
            logger.error(f"Parallel parsing failed: {e}", exc_info=True)
            return json.dumps({"error": f"Failed to parse ingredients: {str(e)}"}, indent=2)

        update_data = {
            "id": recipe["id"],
            "userId": recipe.get("userId"),
            "householdId": recipe.get("householdId"),
            "groupId": recipe.get("groupId"),
            "name": name,
            "slug": slug,
            "description": description,
            "recipeIngredient": parsed_ingredients,
            "recipeInstructions": [_parse_instruction(inst) for inst in instruction_list],
        }

        if prep_time:
            update_data["prepTime"] = prep_time
        if cook_time:
            update_data["performTime"] = cook_time
        if servings:
            update_data["recipeYield"] = servings

        updated = await client.update_recipe(slug, update_data)

        return json.dumps({
            "success": True,
            "slug": updated.get("slug", slug),
            "id": updated.get("id"),
            "name": name,
            "message": f"Recipe '{name}' created successfully",
        }, indent=2)

    except httpx.HTTPStatusError as e:
        return json.dumps({
            "error": f"API error: {e.response.status_code}",
            "details": str(e),
            "hint": "Check that all required fields are provided correctly",
        }, indent=2)
    except ValueError as e:
        return json.dumps({
            "error": str(e),
            "hint": "Check the format of ingredients and instructions",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"Unexpected error: {str(e)}",
        }, indent=2)


@mcp.tool()
async def update_recipe(
    slug: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    ingredients: Optional[list] = None,
    instructions: Optional[list] = None,
    prep_time: Optional[str] = None,
    cook_time: Optional[str] = None,
    servings: Optional[str] = None,
) -> str:
    """
    Update an existing recipe in Mealie.

    Args:
        slug: The recipe's URL slug identifier (from create_recipe or search_recipes)
        name: New name for the recipe
        description: New description
        ingredients: List of ingredient strings. Example: ["500g flour", "2 eggs"]
        instructions: List of instruction strings. Example: ["Preheat oven", "Mix ingredients"]
        prep_time: Preparation time (e.g., "15 minutes")
        cook_time: Cooking time (e.g., "30 minutes")
        servings: Number of servings (e.g., "4 servings")

    Returns:
        JSON with success status and updated recipe info
    """
    try:
        client = get_client()

        recipe = await client.get_recipe(slug)

        update_data = {
            "id": recipe["id"],
            "userId": recipe.get("userId"),
            "householdId": recipe.get("householdId"),
            "groupId": recipe.get("groupId"),
            "name": name or recipe.get("name"),
            "slug": slug,
        }

        if description:
            update_data["description"] = description
        if ingredients:
            ingredient_list = _ensure_list(ingredients)
            ingredient_strings = []
            for ing in ingredient_list:
                if isinstance(ing, dict):
                    ing = ing.get("note") or ing.get("text") or str(ing)
                if not isinstance(ing, str):
                    return json.dumps({
                        "error": f"Ingredient must be a string, got: {type(ing).__name__}",
                        "hint": "Pass ingredients as a list of strings",
                    }, indent=2)
                ingredient_strings.append(ing)

            logger.info(f"Updating: parsing {len(ingredient_strings)} ingredients in parallel...")
            try:
                parsed_ingredients = await asyncio.gather(
                    *[_parse_and_prepare_ingredient(client, ing) for ing in ingredient_strings],
                    return_exceptions=True
                )
                errors = [(i, r) for i, r in enumerate(parsed_ingredients) if isinstance(r, Exception)]
                if errors:
                    for idx, err in errors:
                        logger.error(f"Ingredient {idx} '{ingredient_strings[idx]}' failed: {err}")
                    return json.dumps({
                        "error": "Failed to parse some ingredients",
                        "details": [{"ingredient": ingredient_strings[i], "error": str(e)} for i, e in errors]
                    }, indent=2)
                logger.info(f"Successfully parsed all {len(ingredient_strings)} ingredients")
            except Exception as e:
                logger.error(f"Parallel parsing failed: {e}", exc_info=True)
                return json.dumps({"error": f"Failed to parse ingredients: {str(e)}"}, indent=2)
            update_data["recipeIngredient"] = parsed_ingredients
        if instructions:
            instruction_list = _ensure_list(instructions)
            update_data["recipeInstructions"] = [_parse_instruction(inst) for inst in instruction_list]
        if prep_time:
            update_data["prepTime"] = prep_time
        if cook_time:
            update_data["performTime"] = cook_time
        if servings:
            update_data["recipeYield"] = servings

        updated = await client.update_recipe(slug, update_data)

        new_slug = updated.get("slug")
        response: dict = {
            "success": True,
            "slug": new_slug,
            "name": updated.get("name"),
            "message": "Recipe updated successfully",
        }
        if new_slug and new_slug != slug:
            response["slug_changed"] = {
                "old": slug,
                "new": new_slug,
                "warning": (
                    f"The recipe slug changed from '{slug}' to '{new_slug}' because "
                    "Mealie regenerates slugs from the name. Existing meal plan "
                    "entries link to the recipe by ID (not slug), so they remain "
                    "valid; but any external references using the old slug "
                    "(bookmarks, links shared elsewhere) will 404. Use the new "
                    "slug in subsequent calls."
                ),
            }
        return json.dumps(response, indent=2)

    except httpx.HTTPStatusError as e:
        return json.dumps({
            "error": f"API error: {e.response.status_code}",
            "details": str(e),
            "hint": "Check that the recipe slug exists and fields are correct",
        }, indent=2)
    except ValueError as e:
        return json.dumps({
            "error": str(e),
            "hint": "Check the format of ingredients and instructions",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"Unexpected error: {str(e)}",
        }, indent=2)


@mcp.tool()
async def delete_recipe(slug: str) -> str:
    """
    Delete a recipe from Mealie.

    Args:
        slug: The unique slug identifier for the recipe to delete

    Returns:
        Confirmation of deletion
    """
    client = get_client()
    await client.delete_recipe(slug)
    return json.dumps({
        "success": True,
        "message": f"Recipe '{slug}' deleted successfully",
    })


@mcp.tool()
async def get_meal_plans(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> str:
    """
    Get meal plans within a date range.

    Args:
        start_date: Start date in YYYY-MM-DD format (optional, defaults to today)
        end_date: End date in YYYY-MM-DD format (optional, defaults to 7 days from start)
        page: Page number for pagination
        per_page: Results per page

    Returns:
        List of meal plan entries within the date range
    """
    client = get_client()

    if not start_date:
        start_date = date.today().isoformat()
    if not end_date:
        end_date = (date.today() + timedelta(days=7)).isoformat()

    result = await client.get_meal_plans(
        start_date=start_date,
        end_date=end_date,
        page=page,
        per_page=per_page,
    )

    meals = []
    for meal in result.get("items", []):
        meals.append({
            "id": meal.get("id"),
            "date": meal.get("date"),
            "entry_type": meal.get("entryType"),
            "title": meal.get("title"),
            "recipe_name": meal.get("recipe", {}).get("name") if meal.get("recipe") else None,
            "recipe_slug": meal.get("recipe", {}).get("slug") if meal.get("recipe") else None,
        })

    return json.dumps({
        "start_date": start_date,
        "end_date": end_date,
        "meals": meals,
        "total": result.get("total", 0),
    }, indent=2)


@mcp.tool()
async def create_meal_plan(
    meal_date: str,
    entry_type: str,
    recipe_slug: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """
    Create a meal plan entry.

    Args:
        meal_date: Date for the meal in YYYY-MM-DD format
        entry_type: Type of meal - one of 'breakfast', 'lunch', 'dinner', 'side'
        recipe_slug: Slug of an existing recipe to link (optional)
        title: Custom title if not using a recipe (optional)

    Returns:
        The created meal plan entry
    """
    client = get_client()

    recipe_id = None
    if recipe_slug:
        recipe = await client.get_recipe(recipe_slug)
        recipe_id = recipe.get("id")

    result = await client.create_meal_plan(
        date=meal_date,
        entry_type=entry_type,
        recipe_id=recipe_id,
        title=title,
    )

    return json.dumps({
        "success": True,
        "id": result.get("id"),
        "date": result.get("date"),
        "entry_type": result.get("entryType"),
        "message": f"Meal plan entry created for {meal_date}",
    }, indent=2)


@mcp.tool()
async def delete_meal_plan(item_id: str) -> str:
    """
    Delete a meal plan entry.

    Args:
        item_id: The ID of the meal plan entry to delete

    Returns:
        Confirmation of deletion
    """
    client = get_client()
    await client.delete_meal_plan(item_id)
    return json.dumps({
        "success": True,
        "message": f"Meal plan entry '{item_id}' deleted successfully",
    })


@mcp.tool()
async def search_foods(query: Optional[str] = None, page: int = 1, per_page: int = 20) -> str:
    """
    Search for foods in the database.

    Args:
        query: Search term to filter foods by name (optional)
        page: Page number for pagination (default: 1)
        per_page: Number of results per page (default: 20)

    Returns:
        List of foods with their id, name, and label
    """
    client = get_client()
    result = await client.get_foods(search=query, page=page, per_page=per_page)

    foods = []
    for item in result.get("items", []):
        label = item.get("label")
        foods.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "label": label.get("name") if label else None,
            "label_id": label.get("id") if label else None,
        })

    return json.dumps({
        "foods": foods,
        "total": result.get("total", 0),
        "page": page,
        "per_page": per_page,
    }, indent=2)


@mcp.tool()
async def get_food_details(food_id: str) -> str:
    """
    Get detailed information about a specific food.

    Args:
        food_id: The unique ID of the food

    Returns:
        Food details including name, label, and aliases
    """
    client = get_client()
    food = await client.get_food(food_id)

    label = food.get("label")
    aliases = []
    for alias in food.get("aliases", []):
        if isinstance(alias, dict):
            aliases.append(alias.get("name", ""))
        else:
            aliases.append(str(alias))

    return json.dumps({
        "id": food.get("id"),
        "name": food.get("name"),
        "label": label.get("name") if label else None,
        "label_id": label.get("id") if label else None,
        "aliases": aliases,
    }, indent=2)


@mcp.tool()
async def list_labels() -> str:
    """
    Get all available food labels/categories (e.g., Produce, Dairy, Meat).

    Returns:
        List of all labels with their id and name
    """
    client = get_client()
    result = await client.get_labels()

    labels = []
    for item in result.get("items", []):
        labels.append({
            "id": item.get("id"),
            "name": item.get("name"),
        })

    return json.dumps({
        "labels": labels,
        "total": len(labels),
    }, indent=2)


@mcp.tool()
async def get_shopping_lists() -> str:
    """
    Get all shopping lists.

    Returns:
        List of shopping lists with their id and name
    """
    client = get_client()
    result = await client.get_shopping_lists()

    lists = []
    for item in result.get("items", []):
        lists.append({
            "id": item.get("id"),
            "name": item.get("name"),
        })

    return json.dumps({
        "shopping_lists": lists,
        "total": result.get("total", 0),
    }, indent=2)


@mcp.tool()
async def get_shopping_list(list_id: str) -> str:
    """
    Get a shopping list with all its items, showing which foods have labels and which don't.

    Args:
        list_id: The ID of the shopping list

    Returns:
        Shopping list details with items, their foods, and label status
    """
    client = get_client()
    result = await client.get_shopping_list(list_id)

    items = []
    unlabeled_foods = []
    for item in result.get("listItems", []):
        food = item.get("food")
        food_label = food.get("label") if food else None
        item_label = item.get("label")
        effective_label = item_label or food_label
        entry = {
            "id": item.get("id"),
            "note": item.get("note"),
            "quantity": item.get("quantity"),
            "checked": item.get("checked", False),
            "label": effective_label.get("name") if effective_label else None,
            "label_id": effective_label.get("id") if effective_label else None,
            "food": {
                "id": food.get("id"),
                "name": food.get("name"),
                "label": food_label.get("name") if food_label else None,
                "label_id": food_label.get("id") if food_label else None,
            } if food else None,
            "unit": {
                "id": item.get("unit", {}).get("id"),
                "name": item.get("unit", {}).get("name"),
            } if item.get("unit") else None,
        }
        items.append(entry)
        if food and not food_label:
            unlabeled_foods.append({
                "food_id": food.get("id"),
                "food_name": food.get("name"),
            })

    seen = set()
    unique_unlabeled = []
    for f in unlabeled_foods:
        if f["food_id"] not in seen:
            seen.add(f["food_id"])
            unique_unlabeled.append(f)

    return json.dumps({
        "id": result.get("id"),
        "name": result.get("name"),
        "items": items,
        "total_items": len(items),
        "unlabeled_foods": unique_unlabeled,
        "unlabeled_count": len(unique_unlabeled),
    }, indent=2)


@mcp.tool()
async def add_shopping_list_items(list_id: str, items: list, auto_link_foods: bool = True) -> str:
    """
    Add one or more items to a shopping list.

    Each item should be an object with:
      - note (required): the item text (e.g., "1 lb ground beef", "bananas").
        If auto_link_foods is true (default), the note is searched against the
        existing food database. If a food is matched by name or alias, the item
        is linked to it and inherits that food's label/category automatically.
      - quantity (optional): numeric quantity, default 1
      - label (optional): override category name like "Produce" or
        "🥩 Meat & Poultry". Matched case-insensitively and ignoring any
        leading emoji/punctuation, with a substring fallback. An explicit label
        overrides whatever the matched food carries.

    Args:
        list_id: The shopping list to add items to (from get_shopping_lists).
        items: List of item objects. Example:
            [
              {"note": "Garlic"},                          # auto-links to Garlic food
              {"note": "ground beef", "quantity": 2,
               "label": "Meat & Poultry"},                 # explicit category
              {"note": "batteries"}                        # plain text, no food, no label
            ]
        auto_link_foods: When true, fuzzy-match notes against existing foods.
                         Disable for free-text-only items.

    Returns:
        Per-item result: linked food name (if any), effective label, and any
        labels that could not be resolved.
    """
    client = get_client()
    item_list = _ensure_list(items)

    labels_result = await client.get_labels()
    label_index = _build_label_index(labels_result.get("items", []))

    payload = []
    item_meta = []
    unresolved_labels: list[str] = []

    for raw in item_list:
        if not isinstance(raw, dict) or not raw.get("note"):
            return json.dumps(
                {"error": "Each item must be an object with a 'note' field", "bad_item": str(raw)},
                indent=2,
            )

        note = str(raw["note"]).strip()
        item: dict = {
            "shoppingListId": list_id,
            "note": note,
            "quantity": float(raw.get("quantity", 1)),
            "isFood": False,
        }

        linked_food = None
        if auto_link_foods:
            linked_food = await _search_existing_food(client, note)
            if linked_food:
                item["isFood"] = True
                item["foodId"] = linked_food["id"]
                item["note"] = ""

        explicit_label = raw.get("label")
        if explicit_label:
            resolved = _resolve_label(explicit_label, label_index)
            if resolved:
                item["labelId"] = resolved["id"]
            elif explicit_label not in unresolved_labels:
                unresolved_labels.append(explicit_label)

        payload.append(item)
        item_meta.append({"original_note": note, "linked_food": linked_food})

    created = await client.create_shopping_list_items(payload)
    created_list = created if isinstance(created, list) else created.get("createdItems", [])

    rows = []
    for created_item, meta in zip(created_list, item_meta):
        label_obj = created_item.get("label")
        food_obj = created_item.get("food")
        food_label = (food_obj or {}).get("label")
        rows.append({
            "id": created_item.get("id"),
            "note": meta["original_note"],
            "quantity": created_item.get("quantity"),
            "linked_food": (meta["linked_food"] or {}).get("name") if meta["linked_food"] else None,
            "label": (label_obj or food_label or {}).get("name"),
        })

    return json.dumps(
        {
            "list_id": list_id,
            "created_count": len(rows),
            "items": rows,
            "unresolved_labels": unresolved_labels or None,
        },
        indent=2,
    )


@mcp.tool()
async def update_shopping_list_item(
    item_id: str,
    note: Optional[str] = None,
    quantity: Optional[float] = None,
    label: Optional[str] = None,
    food_id: Optional[str] = None,
    checked: Optional[bool] = None,
) -> str:
    """
    Update a single shopping list item.

    Only the fields you pass are changed; everything else is preserved. To
    clear a label, pass label="". To unlink a food, pass food_id="".

    Args:
        item_id: The shopping list item ID (from get_shopping_list).
        note: New free-text note.
        quantity: New numeric quantity.
        label: New category name (matched like in add_shopping_list_items).
        food_id: Link/unlink to a specific food. Empty string clears the link.
        checked: Mark item checked/unchecked.

    Returns:
        The updated item with its effective label.
    """
    client = get_client()
    current = await client.get_shopping_list_item(item_id)

    update: dict = {
        "id": current["id"],
        "shoppingListId": current["shoppingListId"],
        "note": current.get("note", "") if note is None else note,
        "quantity": current.get("quantity", 1) if quantity is None else float(quantity),
        "isFood": current.get("isFood", False),
        "foodId": current.get("foodId"),
        "labelId": current.get("labelId"),
        "unitId": current.get("unitId"),
        "checked": current.get("checked", False) if checked is None else bool(checked),
        "position": current.get("position", 0),
        "extras": current.get("extras") or {},
    }

    if food_id is not None:
        if food_id == "":
            update["foodId"] = None
            update["isFood"] = False
        else:
            update["foodId"] = food_id
            update["isFood"] = True

    if label is not None:
        if label == "":
            update["labelId"] = None
        else:
            labels_result = await client.get_labels()
            resolved = _resolve_label(label, _build_label_index(labels_result.get("items", [])))
            if not resolved:
                return json.dumps({"error": f"Label not found: {label!r}"}, indent=2)
            update["labelId"] = resolved["id"]

    response = await client.update_shopping_list_item(item_id, update)
    updated_items = response.get("updatedItems") if isinstance(response, dict) else None
    updated = updated_items[0] if updated_items else (response if isinstance(response, dict) else {})
    label_obj = updated.get("label") or (updated.get("food") or {}).get("label")
    return json.dumps({
        "id": updated.get("id"),
        "note": updated.get("note"),
        "quantity": updated.get("quantity"),
        "checked": updated.get("checked"),
        "label": (label_obj or {}).get("name"),
        "linked_food": (updated.get("food") or {}).get("name"),
    }, indent=2)


@mcp.tool()
async def delete_shopping_list_items(item_ids: list) -> str:
    """
    Delete one or more shopping list items in a single call.

    Args:
        item_ids: List of shopping list item IDs to delete.

    Returns:
        Number of items deleted.
    """
    client = get_client()
    ids = _ensure_list(item_ids)
    await client.delete_shopping_list_items(ids)
    return json.dumps({"deleted_count": len(ids), "ids": ids}, indent=2)


@mcp.tool()
async def add_recipe_to_shopping_list(
    list_id: str,
    recipe_slug: str,
    scale: float = 1.0,
) -> str:
    """
    Add all ingredients from a recipe to a shopping list, scaled if desired.

    Mirrors Mealie's "Add to shopping list" button. Items come in pre-linked to
    their foods (so labels apply automatically) and tagged with the source
    recipe so they can be removed together later.

    Args:
        list_id: Target shopping list ID (from get_shopping_lists).
        recipe_slug: The recipe to add (slug or ID, e.g., "homemade-pizza").
        scale: Multiplier for the recipe quantities (default 1.0). Use 2.0 to
               double, 0.5 to halve.

    Returns:
        Number of items added and their notes/foods.
    """
    client = get_client()
    recipe = await client.get_recipe(recipe_slug)

    before = await client.get_shopping_list(list_id)
    before_ids = {it.get("id") for it in before.get("listItems", [])}

    await client.add_recipe_to_shopping_list(list_id, recipe["id"], scale=scale)

    after = await client.get_shopping_list(list_id)
    added = [it for it in after.get("listItems", []) if it.get("id") not in before_ids]

    return json.dumps({
        "list_id": list_id,
        "recipe": {"id": recipe["id"], "name": recipe.get("name"), "slug": recipe.get("slug")},
        "scale": scale,
        "added_count": len(added),
        "added": [
            {
                "id": it.get("id"),
                "food": (it.get("food") or {}).get("name"),
                "quantity": it.get("quantity"),
                "note": it.get("note"),
                "label": ((it.get("label") or (it.get("food") or {}).get("label")) or {}).get("name"),
            }
            for it in added
        ],
    }, indent=2)


@mcp.tool()
async def bulk_assign_food_labels(assignments: list) -> str:
    """
    Assign labels to multiple foods at once. Useful for labeling shopping list items.

    Use list_labels first to get available label IDs, then get_shopping_list to find
    unlabeled foods.

    Args:
        assignments: List of objects with "food_id" and "label_id" fields.
                     Example: [{"food_id": "abc-123", "label_id": "def-456"}, ...]

    Returns:
        Summary of successful and failed assignments
    """
    client = get_client()
    assignment_list = _ensure_list(assignments)

    successes = []
    failures = []

    for item in assignment_list:
        if not isinstance(item, dict) or "food_id" not in item or "label_id" not in item:
            failures.append({"item": str(item), "error": "Must have 'food_id' and 'label_id' fields"})
            continue

        food_id = item["food_id"]
        label_id = item["label_id"]

        try:
            food = await client.get_food(food_id)
            update_data = {
                "id": food["id"],
                "name": food["name"],
                "labelId": label_id,
            }
            updated = await client.update_food(food_id, update_data)
            label = updated.get("label")
            successes.append({
                "food_id": food_id,
                "food_name": updated.get("name"),
                "label": label.get("name") if label else None,
            })
        except Exception as e:
            failures.append({"food_id": food_id, "error": str(e)})

    return json.dumps({
        "success_count": len(successes),
        "failure_count": len(failures),
        "successes": successes,
        "failures": failures if failures else None,
    }, indent=2)


@mcp.tool()
async def upload_recipe_image(
    slug: str,
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
    filename: Optional[str] = None,
) -> str:
    """
    Upload an image to a recipe from either a URL or base64-encoded data.

    Exactly one of image_url or image_base64 must be provided.

    Args:
        slug: The recipe slug to upload the image to.
        image_url: URL of an image to download and upload.
        image_base64: Base64-encoded image bytes. A data URI prefix such as
            "data:image/jpeg;base64," is tolerated and stripped.
        filename: Optional filename hint. If omitted, derived from image_url
            or defaults to "recipe.png".

    Returns:
        Confirmation of image upload.
    """
    if (image_url is None) == (image_base64 is None):
        return json.dumps(
            {"error": "Provide exactly one of image_url or image_base64."},
            indent=2,
        )

    client = get_client()

    if image_url is not None:
        ua = "Mozilla/5.0 (compatible; MealieMCP/1.0; +https://github.com/mhempstock/mealie-mcp)"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http_client:
            response = await http_client.get(image_url, headers={"User-Agent": ua})
            response.raise_for_status()
            image_data = response.content
        if not filename:
            filename = image_url.split("/")[-1].split("?")[0]
    else:
        b64 = image_base64.strip()
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        b64 = "".join(b64.split())
        try:
            image_data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            return json.dumps({"error": f"Invalid base64 image data: {e}"}, indent=2)

    if not filename or not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        filename = "recipe_image.png"

    await client.upload_recipe_image(slug, image_data, filename)
    return json.dumps({
        "success": True,
        "slug": slug,
        "filename": filename,
        "bytes": len(image_data),
        "message": f"Image uploaded successfully to recipe '{slug}'",
    })


def main():
    """Run the MCP server."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
