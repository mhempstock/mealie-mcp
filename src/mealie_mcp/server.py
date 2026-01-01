"""Mealie MCP Server - Main server implementation."""

import os
import json
import base64
import httpx
from datetime import date, datetime, timedelta
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .client import MealieClient


mcp = FastMCP(
    name="mealie",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    instructions="""
    Mealie MCP Server for recipe and meal planning management.

    Use these tools to:
    - Search and retrieve recipes from Mealie
    - Create new recipes with ingredients and instructions
    - Update existing recipes
    - Manage meal plans (create, view, modify)
    - Get today's date for meal planning
    - Generate and upload recipe images
    """,
)


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


@mcp.tool()
async def create_recipe(
    name: str,
    description: str,
    ingredients: str,
    instructions: str,
    prep_time: Optional[str] = None,
    cook_time: Optional[str] = None,
    servings: Optional[str] = None,
) -> str:
    """
    Create a new recipe in Mealie.

    Args:
        name: Name of the recipe
        description: Brief description of the recipe
        ingredients: JSON array of ingredients, each with 'note' (required), 'quantity' (optional), 'unit' (optional)
                    Example: [{"note": "2 cups flour"}, {"note": "1 tsp salt"}]
        instructions: JSON array of instruction steps, each with 'text'
                     Example: [{"text": "Preheat oven to 350°F"}, {"text": "Mix dry ingredients"}]
        prep_time: Preparation time (e.g., "15 minutes")
        cook_time: Cooking time (e.g., "30 minutes")
        servings: Number of servings (e.g., "4 servings")

    Returns:
        The created recipe's slug and ID
    """
    client = get_client()

    ingredient_list = json.loads(ingredients)
    instruction_list = json.loads(instructions)

    created = await client.create_recipe(name)
    slug = created

    update_data = {
        "name": name,
        "description": description,
        "recipeIngredient": [
            {"note": ing.get("note", ""), "quantity": ing.get("quantity"), "unit": ing.get("unit")}
            for ing in ingredient_list
        ],
        "recipeInstructions": [
            {"text": inst.get("text", "")}
            for inst in instruction_list
        ],
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


@mcp.tool()
async def update_recipe(
    slug: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    ingredients: Optional[str] = None,
    instructions: Optional[str] = None,
    prep_time: Optional[str] = None,
    cook_time: Optional[str] = None,
    servings: Optional[str] = None,
) -> str:
    """
    Update an existing recipe in Mealie.

    Args:
        slug: The unique slug identifier for the recipe to update
        name: New name for the recipe (optional)
        description: New description (optional)
        ingredients: JSON array of ingredients to replace existing (optional)
        instructions: JSON array of instructions to replace existing (optional)
        prep_time: New preparation time (optional)
        cook_time: New cooking time (optional)
        servings: New servings count (optional)

    Returns:
        Updated recipe information
    """
    client = get_client()

    update_data = {}

    if name:
        update_data["name"] = name
    if description:
        update_data["description"] = description
    if ingredients:
        ingredient_list = json.loads(ingredients)
        update_data["recipeIngredient"] = [
            {"note": ing.get("note", ""), "quantity": ing.get("quantity"), "unit": ing.get("unit")}
            for ing in ingredient_list
        ]
    if instructions:
        instruction_list = json.loads(instructions)
        update_data["recipeInstructions"] = [
            {"text": inst.get("text", "")}
            for inst in instruction_list
        ]
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
        "message": f"Recipe updated successfully",
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

    async with httpx.AsyncClient() as http_client:
        response = await http_client.get(image_url, timeout=30.0)
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
async def upload_recipe_image_base64(slug: str, image_base64: str, filename: str = "recipe.png") -> str:
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
