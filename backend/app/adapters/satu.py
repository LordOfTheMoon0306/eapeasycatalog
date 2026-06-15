from __future__ import annotations

import httpx

from app.adapters.base import MarketplaceAdapter
from app.schemas.models import ProductCard, ProductDetail, SourceName


SATU_ANALYZER_URL = "https://satu-analyzer.onrender.com/analyze"


def format_price(value) -> str | None:
    if value is None:
        return None

    try:
        return f"{float(value):,.0f} ₸".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


class SatuAdapter(MarketplaceAdapter):
    source = SourceName.satu

    async def search(self, query: str, limit: int = 10) -> list[ProductCard]:
        params = {
            "query": query,
            "selected_source": "satu",
            "selected_category": "auto",
            "start_page": 1,
            "end_page": 1,
            "remove_outliers": "true",
            "strict_title_match": "false",
            "best_limit": limit,
        }

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(SATU_ANALYZER_URL, params=params)
            response.raise_for_status()
            payload = response.json()

        offers = payload.get("best_offers") or []

        items: list[ProductCard] = []

        for offer in offers[:limit]:
            title = offer.get("title")
            url = offer.get("url")

            if not title or not url:
                continue

            rating = offer.get("rating")
            reviews_count = offer.get("reviews_count")

            seller = (
                offer.get("supplier")
                or offer.get("seller")
                or offer.get("company")
                or offer.get("company_name")
            )

            items.append(
                ProductCard(
                    source=SourceName.satu,
                    title=title,
                    image_url=offer.get("image_url"),
                    price=format_price(offer.get("price")),
                    product_url=url,
                    rating=str(rating) if rating is not None else None,
                    reviews_count=str(reviews_count) if reviews_count is not None else None,
                    seller=seller,
                )
            )

        return items

    async def get_product_details(self, product_url: str) -> ProductDetail:
        return ProductDetail(
            source=SourceName.satu,
            title="Satu.kz product",
            product_url=product_url,
            image_url=None,
            price=None,
            rating=None,
            reviews_count=None,
            description="Open the product page on Satu.kz for full details.",
            characteristics={},
            raw_sections={},
        )