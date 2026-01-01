"""Mealie API client for interacting with the Mealie recipe management system."""

import httpx
from typing import Optional
from dataclasses import dataclass


@dataclass
class Ingredient:
    """Recipe ingredient."""
    note: str
    quantity: Optional[float] = None
    unit: Optional[str] = None


@dataclass
class Instruction:
    """Recipe instruction step."""
    text: str


class MealieClient:
    """HTTP client for Mealie API."""

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Make an HTTP request to the Mealie API."""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                url,
                headers=self.headers,
                json=json,
                params=params,
                timeout=30.0,
            )
            if response.status_code >= 400:
                try:
                    error_body = response.json()
                except Exception:
                    error_body = response.text
                raise httpx.HTTPStatusError(
                    f"{response.status_code} {response.reason_phrase}: {error_body}",
                    request=response.request,
                    response=response,
                )
            if response.status_code == 204:
                return {}
            return response.json()

    async def _request_multipart(
        self,
        method: str,
        path: str,
        files: dict,
    ) -> dict:
        """Make a multipart HTTP request to the Mealie API."""
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.headers['Authorization'].split(' ')[1]}"}
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                files=files,
                timeout=60.0,
            )
            response.raise_for_status()
            return response.json()

    async def get_recipes(
        self,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
        categories: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
    ) -> dict:
        """Get a paginated list of recipes."""
        params = {"page": page, "perPage": per_page}
        if search:
            params["search"] = search
        if categories:
            params["categories"] = categories
        if tags:
            params["tags"] = tags
        return await self._request("GET", "/api/recipes", params=params)

    async def get_recipe(self, slug: str) -> dict:
        """Get a single recipe by slug."""
        return await self._request("GET", f"/api/recipes/{slug}")

    async def create_recipe(self, name: str) -> dict:
        """Create a new recipe with just a name (returns slug for further updates)."""
        return await self._request("POST", "/api/recipes", json={"name": name})

    async def update_recipe(self, slug: str, data: dict) -> dict:
        """Update a recipe."""
        return await self._request("PUT", f"/api/recipes/{slug}", json=data)

    async def delete_recipe(self, slug: str) -> dict:
        """Delete a recipe."""
        return await self._request("DELETE", f"/api/recipes/{slug}")

    async def upload_recipe_image(self, slug: str, image_data: bytes, filename: str) -> dict:
        """Upload an image for a recipe."""
        files = {
            "image": (filename, image_data, "image/png"),
        }
        return await self._request_multipart(
            "PUT",
            f"/api/recipes/{slug}/image",
            files=files,
        )

    async def get_meal_plans(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> dict:
        """Get meal plans with optional date filtering."""
        params = {"page": page, "perPage": per_page}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return await self._request("GET", "/api/households/mealplans", params=params)

    async def get_todays_meals(self) -> list:
        """Get today's meal plan entries."""
        return await self._request("GET", "/api/households/mealplans/today")

    async def create_meal_plan(
        self,
        date: str,
        entry_type: str,
        recipe_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> dict:
        """
        Create a meal plan entry.

        Args:
            date: Date in YYYY-MM-DD format
            entry_type: One of 'breakfast', 'lunch', 'dinner', 'side'
            recipe_id: ID of an existing recipe (optional)
            title: Custom title if not using a recipe (optional)
        """
        data = {
            "date": date,
            "entryType": entry_type,
        }
        if recipe_id:
            data["recipeId"] = recipe_id
        if title:
            data["title"] = title
        return await self._request("POST", "/api/households/mealplans", json=data)

    async def update_meal_plan(self, item_id: str, data: dict) -> dict:
        """Update a meal plan entry."""
        return await self._request("PUT", f"/api/households/mealplans/{item_id}", json=data)

    async def delete_meal_plan(self, item_id: str) -> dict:
        """Delete a meal plan entry."""
        return await self._request("DELETE", f"/api/households/mealplans/{item_id}")

    async def parse_ingredient(self, ingredient_text: str) -> dict:
        """Parse an ingredient string into structured data with unit/food IDs."""
        result = await self._request(
            "POST",
            "/api/parser/ingredient",
            json={"ingredient": ingredient_text},
        )
        return result.get("ingredient", {})

    async def create_unit(self, name: str) -> dict:
        """Create a new unit."""
        return await self._request("POST", "/api/units", json={"name": name})

    async def create_food(self, name: str) -> dict:
        """Create a new food."""
        return await self._request("POST", "/api/foods", json={"name": name})
