from __future__ import annotations

import asyncio
import logging
import re
import time
from statistics import mean, median
from uuid import uuid4

from app.adapters.base import MarketplaceAdapter
from app.adapters.kaspi import KaspiAdapter
from app.adapters.ozon import OzonAdapter
from app.adapters.wildberries import WildberriesAdapter
from app.adapters.satu import SatuAdapter
from app.core.config import settings
from app.core.http_client import RequestClient
from app.core.proxy_manager import ProxyManager
from app.schemas.models import (
    ProductCard,
    ProductDetail,
    PriceStats,
    SearchResponse,
    SourceName,
    SourceResult,
    SupplierInfo,
)

logger = logging.getLogger(__name__)

def parse_price_value(raw_price: str | None) -> int | None:
    if not raw_price:
        return None

    digits = re.sub(r"\D", "", raw_price)

    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def detect_currency(raw_price: str | None) -> str | None:
    if not raw_price:
        return None

    if "₸" in raw_price or "тг" in raw_price.lower():
        return "₸"

    if "₽" in raw_price or "руб" in raw_price.lower():
        return "₽"

    return None


def analyze_prices(items: list[ProductCard]) -> PriceStats:
    prices: list[int] = []
    currencies: list[str] = []

    for item in items:
        price = parse_price_value(item.price)
        if price is None:
            continue

        prices.append(price)

        currency = detect_currency(item.price)
        if currency:
            currencies.append(currency)

    if not prices:
        return PriceStats(
            min_price=None,
            max_price=None,
            average_price=None,
            median_price=None,
            products_with_price=0,
            currency=None,
        )

    currency = currencies[0] if currencies else None

    return PriceStats(
        min_price=min(prices),
        max_price=max(prices),
        average_price=round(mean(prices), 2),
        median_price=round(median(prices), 2),
        products_with_price=len(prices),
        currency=currency,
    )


def normalize_supplier_name(raw_name: str | None) -> str | None:
    if not raw_name:
        return None

    name = re.sub(r"\s+", " ", raw_name).strip()
    if not name:
        return None

    lowered = name.lower()
    bad_exact_values = {
        "нет данных",
        "unknown",
        "none",
        "null",
        "-",
    }
    if lowered in bad_exact_values:
        return None

    bad_substrings = (
        "стать продавцом",
        "продавать на",
        "kaspi гид",
        "клиентам",
        "бизнесу",
        "магазин",
        "каталог",
        "поиск",
        "купить",
        "в корзину",
        "отзыв",
        "рейтинг",
        "рассроч",
        "в месяц",
        "0-0-12",
    )
    if any(token in lowered for token in bad_substrings):
        return None

    if re.search(r"\d[\d\s\xa0]*(?:₸|тг|₽|руб|тенге)", lowered):
        return None
    if re.search(r"(?:₸|тг|₽|руб|тенге)\s*\d", lowered):
        return None
    if re.search(r"\b\d+(?:[.,]\d+)?\s*(?:отзыв|отзыва|отзывов)\b", lowered):
        return None
    if re.search(r"(?:x|×|х)\s*\d+\s*(?:мес|месяц)", lowered):
        return None

    return name


def analyze_suppliers(
    items: list[ProductCard],
    include_source: bool = False,
) -> list[SupplierInfo]:
    grouped: dict[tuple[str | None, str], dict] = {}

    for item in items:
        supplier_name = normalize_supplier_name(getattr(item, "seller", None))

        if not supplier_name:
            continue

        source_value = item.source.value if include_source else None
        key = (source_value, supplier_name)

        if key not in grouped:
            grouped[key] = {
                "source": item.source if include_source else None,
                "name": supplier_name,
                "prices": [],
                "products_count": 0,
                "rating": None,
                "reviews_count": None,
            }

        grouped[key]["products_count"] += 1

        if not grouped[key]["rating"]:
            grouped[key]["rating"] = item.seller_rating
        if not grouped[key]["reviews_count"]:
            grouped[key]["reviews_count"] = item.seller_reviews_count

        price = parse_price_value(item.price)
        if price is not None:
            grouped[key]["prices"].append(price)

    suppliers: list[SupplierInfo] = []

    for data in grouped.values():
        prices = data["prices"]

        if prices:
            min_price = min(prices)
            max_price = max(prices)
            average_price = round(mean(prices), 2)
        else:
            min_price = None
            max_price = None
            average_price = None

        suppliers.append(
            SupplierInfo(
                source=data["source"],
                name=data["name"],
                products_count=data["products_count"],
                min_price=min_price,
                max_price=max_price,
                average_price=average_price,
                rating=data["rating"],
                reviews_count=data["reviews_count"],
            )
        )

    suppliers.sort(
        key=lambda supplier: (
            -supplier.products_count,
            supplier.min_price if supplier.min_price is not None else 10**18,
            supplier.name.lower(),
        )
    )

    return suppliers


class MarketplaceService:
    def __init__(self, proxy_manager: ProxyManager):
        client = RequestClient(proxy_manager)
        self.adapters: dict[SourceName, MarketplaceAdapter] = {
            SourceName.kaspi: KaspiAdapter(client),
            SourceName.wildberries: WildberriesAdapter(client),
            SourceName.ozon: OzonAdapter(client),
            SourceName.satu: SatuAdapter(),
        }
        per_source_limit = max(1, settings.source_concurrency_limit)
        self._source_semaphores: dict[SourceName, asyncio.Semaphore] = {
            source: asyncio.Semaphore(per_source_limit) for source in self.adapters
        }

    async def _run_with_source_limit(self, source: SourceName, operation):
        semaphore = self._source_semaphores[source]
        async with semaphore:
            return await operation

    async def search(self, query: str, request_id: str | None = None) -> SearchResponse:
        request_id = request_id or str(uuid4())

        async def run_source(source: SourceName, adapter: MarketplaceAdapter) -> SourceResult:
            started_at = time.perf_counter()
            try:
                items = await self._run_with_source_limit(
                    source,
                    adapter.search(query, limit=settings.max_items_per_source),
                )
                logger.info(
                    "search_source_done request_id=%s source=%s items=%s latency_ms=%s",
                    request_id,
                    source.value,
                    len(items),
                    int((time.perf_counter() - started_at) * 1000),
                )
                return SourceResult(
                    source=source,
                    items=items,
                    price_stats=analyze_prices(items),
                    suppliers=analyze_suppliers(items),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "search_source_failed request_id=%s source=%s latency_ms=%s err=%s",
                    request_id,
                    source.value,
                    int((time.perf_counter() - started_at) * 1000),
                    exc,
                )
                return SourceResult(source=source, items=[], error=str(exc))

        tasks = [run_source(source, adapter) for source, adapter in self.adapters.items()]
        results = await asyncio.gather(*tasks)
        logger.info(
            "search_request_done request_id=%s query_len=%s sources=%s",
            request_id,
            len(query),
            len(results),
        )
        all_items = []
        for result in results:
            all_items.extend(result.items)

        return SearchResponse(
            query=query,
            results=results,
            price_stats=analyze_prices(all_items),
            suppliers=analyze_suppliers(all_items, include_source=True),
        )

    async def get_product_details(self, source: SourceName, product_url: str, request_id: str | None = None) -> ProductDetail:
        request_id = request_id or str(uuid4())
        started_at = time.perf_counter()
        adapter = self.adapters[source]
        detail = await self._run_with_source_limit(source, adapter.get_product_details(product_url))
        logger.info(
            "detail_request_done request_id=%s source=%s latency_ms=%s",
            request_id,
            source.value,
            int((time.perf_counter() - started_at) * 1000),
        )
        return detail
