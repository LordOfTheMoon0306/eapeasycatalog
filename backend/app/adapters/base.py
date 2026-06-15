from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas.models import ProductCard, ProductDetail, SourceName


class MarketplaceAdapter(ABC):
    source: SourceName

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[ProductCard]:
        raise NotImplementedError

    @abstractmethod
    async def get_product_details(self, product_url: str) -> ProductDetail:
        raise NotImplementedError
