"""Mealie MCP Server - Main server implementation."""

import os
import json
import asyncio
import base64
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
def get_todays_date() -> str:
    """
    Get today's date in YYYY-MM-DD format.
    Useful for creating meal plans for specific dates.
    Also returns the day of the week.
    """
    today = date.today()
    return json.dumps({
        "date": today.isoformat(),
        "day_of_week": today.strftime("%A"),
        "formatted": today.strftime("%B %d, %Y"),
    })


@mcp.tool()
def get_date_offset(days_from_today: int) -> str:
    """
    Get a date relative to today.

    Args:
        days_from_today: Number of days from today (positive for future, negative for past)

    Returns:
        Date information including ISO format, day of week, and formatted string
    """
    target_date = date.today() + timedelta(days=days_from_today)
    return json.dumps({
        "date": target_date.isoformat(),
        "day_of_week": target_date.strftime("%A"),
        "formatted": target_date.strftime("%B %d, %Y"),
    })


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

        return json.dumps({
            "success": True,
            "slug": updated.get("slug"),
            "name": updated.get("name"),
            "message": "Recipe updated successfully",
        }, indent=2)

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
async def get_todays_meals() -> str:
    """
    Get all meal plan entries for today.

    Returns:
        List of today's planned meals with their details
    """
    client = get_client()
    meals = await client.get_todays_meals()

    result = []
    for meal in meals:
        result.append({
            "id": meal.get("id"),
            "date": meal.get("date"),
            "entry_type": meal.get("entryType"),
            "title": meal.get("title"),
            "recipe": {
                "id": meal.get("recipe", {}).get("id") if meal.get("recipe") else None,
                "name": meal.get("recipe", {}).get("name") if meal.get("recipe") else None,
                "slug": meal.get("recipe", {}).get("slug") if meal.get("recipe") else None,
            } if meal.get("recipe") else None,
        })

    return json.dumps({
        "date": date.today().isoformat(),
        "meals": result,
    }, indent=2)


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
async def assign_food_label(food_id: str, label_id: str) -> str:
    """
    Assign a label/category to a food.

    Args:
        food_id: The ID of the food to update
        label_id: The ID of the label to assign (use list_labels to see available labels)

    Returns:
        Confirmation of the label assignment
    """
    client = get_client()

    food = await client.get_food(food_id)

    update_data = {
        "id": food["id"],
        "name": food["name"],
        "labelId": label_id,
    }

    updated = await client.update_food(food_id, update_data)

    label = updated.get("label")
    return json.dumps({
        "success": True,
        "food_id": food_id,
        "food_name": updated.get("name"),
        "label": label.get("name") if label else None,
        "message": f"Label assigned to '{updated.get('name')}'",
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
        label = food.get("label") if food else None
        entry = {
            "id": item.get("id"),
            "note": item.get("note"),
            "quantity": item.get("quantity"),
            "checked": item.get("checked", False),
            "food": {
                "id": food.get("id"),
                "name": food.get("name"),
                "label": label.get("name") if label else None,
                "label_id": label.get("id") if label else None,
            } if food else None,
            "unit": {
                "id": item.get("unit", {}).get("id"),
                "name": item.get("unit", {}).get("name"),
            } if item.get("unit") else None,
        }
        items.append(entry)
        if food and not label:
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
async def add_shopping_list_items(list_id: str, items: list) -> str:
    """
    Add one or more items to a shopping list, optionally categorised by label.

    Each item should be an object with:
      - note (required): the item text (e.g., "1 lb ground beef", "bananas")
      - quantity (optional): numeric quantity, default 1
      - label (optional): category name like "Produce", "Dairy", "Meat".
        Matched case-insensitively against existing labels via list_labels.
        Unknown labels are silently dropped (the item is still added, just
        uncategorised) and reported in the result.

    Args:
        list_id: The shopping list to add items to (from get_shopping_lists).
        items: List of item objects. Example:
            [
              {"note": "2 lemons", "label": "Produce"},
              {"note": "ground beef", "quantity": 1, "label": "Meat"},
              {"note": "batteries"}
            ]

    Returns:
        Summary of created items, plus any labels that could not be resolved.
    """
    client = get_client()
    item_list = _ensure_list(items)

    labels_result = await client.get_labels()
    label_by_name = {
        (lbl.get("name") or "").strip().lower(): lbl.get("id")
        for lbl in labels_result.get("items", [])
        if lbl.get("id")
    }

    payload = []
    unresolved_labels = []
    for raw in item_list:
        if not isinstance(raw, dict) or not raw.get("note"):
            return json.dumps(
                {"error": "Each item must be an object with a 'note' field", "bad_item": str(raw)},
                indent=2,
            )

        item: dict = {
            "shoppingListId": list_id,
            "note": str(raw["note"]),
            "quantity": float(raw.get("quantity", 1)),
            "isFood": False,
        }

        label_name = raw.get("label")
        if label_name:
            label_id = label_by_name.get(label_name.strip().lower())
            if label_id:
                item["labelId"] = label_id
            elif label_name not in unresolved_labels:
                unresolved_labels.append(label_name)

        payload.append(item)

    created = await client.create_shopping_list_items(payload)
    created_list = created if isinstance(created, list) else created.get("createdItems", [])

    return json.dumps(
        {
            "list_id": list_id,
            "created_count": len(created_list),
            "created": [
                {
                    "id": it.get("id"),
                    "note": it.get("note"),
                    "quantity": it.get("quantity"),
                    "label": (it.get("label") or {}).get("name"),
                }
                for it in created_list
            ],
            "unresolved_labels": unresolved_labels or None,
        },
        indent=2,
    )


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
async def upload_recipe_image(slug: str, image_url: str) -> str:
    """
    Upload an image to a recipe from a URL.

    Args:
        slug: The recipe slug to upload the image to
        image_url: URL of the image to download and upload

    Returns:
        Confirmation of image upload
    """
    client = get_client()

    headers = {"User-Agent": "Mozilla/5.0 (compatible; MealieMCP/1.0; +https://github.com/mhempstock/mealie-mcp)"}
    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        response = await http_client.get(image_url, headers=headers, timeout=30.0)
        response.raise_for_status()
        image_data = response.content

    filename = image_url.split("/")[-1].split("?")[0]
    if not filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
        filename = "recipe_image.png"

    await client.upload_recipe_image(slug, image_data, filename)

    return json.dumps({
        "success": True,
        "slug": slug,
        "message": f"Image uploaded successfully to recipe '{slug}'",
    })


@mcp.tool()
async def upload_recipe_image_base64(
    slug: str, image_base64: str, filename: str = "recipe.png"
) -> str:
    """
    Upload a base64-encoded image to a recipe.

    Args:
        slug: The recipe slug to upload the image to
        image_base64: Base64-encoded image data
        filename: Filename for the image (default: recipe.png)

    Returns:
        Confirmation of image upload
    """
    client = get_client()

    image_data = base64.b64decode(image_base64)
    await client.upload_recipe_image(slug, image_data, filename)

    return json.dumps({
        "success": True,
        "slug": slug,
        "message": f"Image uploaded successfully to recipe '{slug}'",
    })


def main():
    """Run the MCP server."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
