from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup, Tag

from app.adapters.base import MarketplaceAdapter
from app.adapters.common import (
    choose_first_non_empty,
    clean_text,
    detect_antibot_challenge,
    extract_product_jsonld,
    extract_reviews_count,
    extract_total_results_count,
    first_attr,
    first_text,
    format_price,
    format_rating,
    gather_key_value,
    log_block_event,
    looks_like_banner_or_ad,
    looks_like_product_title,
    normalize_link,
)
from app.adapters.fallback_playwright import render_page
from app.core.config import settings
from app.core.http_client import RequestClient
from app.schemas.models import ProductCard, ProductDetail, SourceName

logger = logging.getLogger(__name__)


class WildberriesAdapter(MarketplaceAdapter):
    source = SourceName.wildberries
    base_url = "https://www.wildberries.kz"
    search_wait_selectors = [
        "article.product-card",
        ".product-card__wrapper",
    ]
    detail_wait_selectors = [
        "h1[class*='product-page']",
        "[class*='product-title']",
        "h2[class*='productTitle']",  # New SPA hash class
        "ins[class*='price-block__final-price']",
        "ins[class*='priceBlockFinalPrice']",  # New SPA Hash class
        "div[class*='product-page__slider']",
        "div[class*='productPageSlider']",
        "ul.product-params__list",
        "table.table--CGApj",  # Table from new SPA
    ]

    def __init__(self, client: RequestClient):
        self.client = client
        self.last_block_reason: str | None = None
        self.last_search_total_found: int | None = None
        self.last_search_sellers: list[str] = []

    @staticmethod
    def _resolve_device_profile() -> str:
        configured = (settings.wildberries_device_profile or settings.device_profile_default).strip().lower()
        if configured not in {"desktop", "mobile"}:
            return settings.device_profile_default
        return configured

    async def search(self, query: str, limit: int = 10) -> list[ProductCard]:
        url = f"{self.base_url}/catalog/0/search.aspx?search={quote_plus(query)}"
        profile = self._resolve_device_profile()
        user_agent = self.client.pick_user_agent(profile)
        self.last_search_total_found = None
        self.last_search_sellers = []

        cards = await self._search_with_selenium(
            url=url,
            limit=limit,
            device_profile=profile,
            user_agent=user_agent,
        )
        if cards:
            cards = await self._enrich_cards_with_sellers(
                cards,
                device_profile=profile,
                user_agent=user_agent,
                max_items=5,
            )
            self.last_search_sellers = sorted(
                {
                    item.seller.strip()
                    for item in cards
                    if isinstance(item.seller, str) and item.seller.strip()
                },
                key=str.lower,
            )
            return cards[:limit]

        logger.warning("Wildberries Selenium search returned no cards, trying HTTP fallback: %s", url)
        try:
            html = await self.client.fetch_text(
                url,
                source=self.source.value,
                device_profile=profile,
                user_agent=user_agent,
            )
            cards = self._parse_cards(html.text, limit)
            if cards:
                cards = await self._enrich_cards_with_sellers(
                    cards,
                    device_profile=profile,
                    user_agent=user_agent,
                    max_items=5,
                )
                self.last_search_sellers = sorted(
                    {
                        item.seller.strip()
                        for item in cards
                        if isinstance(item.seller, str) and item.seller.strip()
                    },
                    key=str.lower,
                )
                return cards
        except Exception as exc:  # noqa: BLE001
            logger.warning("Wildberries HTTP fallback failed: %s", exc)

        if self.last_block_reason:
            raise RuntimeError(f"Wildberries blocked by anti-bot challenge: {self.last_block_reason}")
        raise RuntimeError("Wildberries parser returned no products")

    async def get_product_details(self, product_url: str) -> ProductDetail:
        full_url = urljoin(self.base_url, product_url)
        profile = self._resolve_device_profile()
        user_agent = self.client.pick_user_agent(profile)

        apify_detail = None
        article_id = None
        apify_api_key = settings.apify_api_key.strip()
        import re
        match = re.search(r'(?:catalog/|/)(\d+)(?:/detail\.aspx)?', full_url)
        if match and apify_api_key:
            article_id = match.group(1)
            try:
                from apify_client import ApifyClientAsync

                client = ApifyClientAsync(apify_api_key)
                run_input = {
                    "articleId": article_id,
                    "proxyServer": {"useApifyProxy": True}
                }
                logger.info("Starting Apify task for WB: %s", article_id)
                run = await client.actor(settings.apify_wildberries_actor_id).call(run_input=run_input)
                dataset = await client.dataset(run["defaultDatasetId"]).list_items()
                items = dataset.items
                if items and "result" in items[0]:
                    res = items[0]["result"]
                    attributes = {}
                    for opt in res.get("options", []):
                        if "name" in opt and "value" in opt:
                            attributes[opt["name"]] = str(opt["value"])
                            
                    apify_detail = {
                        "title": res.get("imt_name", ""),
                        "description": res.get("description", ""),
                        "characteristics": attributes,
                    }
            except Exception as exc:
                logger.warning("Apify failed for WB, %s", exc)
        elif match:
            article_id = match.group(1)
            logger.info("APIFY_API_KEY is empty, skipping Apify for Wildberries detail")

        detail = await self._detail_with_selenium(
            full_url,
            device_profile=profile,
            user_agent=user_agent,
        )
        
        if not (detail and detail.title):
            logger.warning("Wildberries Selenium detail parse incomplete, trying HTTP fallback: %s", full_url)
            html = await self.client.fetch_text(
                full_url,
                source=self.source.value,
                device_profile=profile,
                user_agent=user_agent,
            )
            detail = self._parse_detail(html.text, full_url)
            
        if detail and apify_detail:
            detail.characteristics = apify_detail.get("characteristics") or detail.characteristics
            detail.description = apify_detail.get("description") or detail.description
            if not detail.title:
                detail.title = apify_detail.get("title") or detail.title

        # Resolving image issue (-BUttZw82auVosRru2) manually since Apify WB card parser doesn't return image URLs
        if detail and article_id and not detail.image_url:
            nm_id = int(article_id)
            vol = nm_id // 100000
            part = nm_id // 1000
            basket = "01"
            if 0 <= vol <= 143: basket = "01"
            elif 144 <= vol <= 287: basket = "02"
            elif 288 <= vol <= 431: basket = "03"
            elif 432 <= vol <= 719: basket = "04"
            elif 720 <= vol <= 1007: basket = "05"
            elif 1008 <= vol <= 1061: basket = "06"
            elif 1062 <= vol <= 1115: basket = "07"
            elif 1116 <= vol <= 1169: basket = "08"
            elif 1170 <= vol <= 1313: basket = "09"
            elif 1314 <= vol <= 1601: basket = "10"
            elif 1602 <= vol <= 1655: basket = "11"
            elif 1656 <= vol <= 1919: basket = "12"
            elif 1920 <= vol <= 2045: basket = "13"
            elif 2046 <= vol <= 2189: basket = "14"
            elif 2190 <= vol <= 2405: basket = "15"
            else: basket = "16"
            detail.image_url = f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/big/1.webp"

        if detail and detail.title:
            return detail

        raise RuntimeError("Failed to parse Wildberries product details")

    async def _search_with_selenium(
        self,
        url: str,
        limit: int,
        device_profile: str,
        user_agent: str,
    ) -> list[ProductCard]:
        timeout_ms = int((settings.request_timeout_seconds + 10) * 1000)
        for attempt in range(1, settings.max_retries + 1):
            proxy_url = self.client.proxy_manager.next_proxy()
            try:
                rendered = await render_page(
                    url=url,
                    timeout_ms=timeout_ms,
                    wait_selectors=self.search_wait_selectors,
                    proxy_url=proxy_url,
                    scroll=True,
                    device_profile=device_profile,
                    user_agent=user_agent,
                )
                blocked_by = detect_antibot_challenge(rendered)
                if blocked_by:
                    self.last_block_reason = blocked_by
                    self.client.proxy_manager.mark_dead(
                        proxy_url,
                        reason=f"anti-bot challenge: {blocked_by}",
                        url=url,
                    )
                    if settings.enable_block_telemetry:
                        log_block_event(
                            logger,
                            source=self.source.value,
                            stage="playwright_search",
                            url=url,
                            marker=blocked_by,
                            attempt=attempt,
                            proxy=proxy_url,
                        )
                    else:
                        logger.warning(
                            "Wildberries anti-bot challenge detected, attempt=%s proxy=%s marker=%s",
                            attempt,
                            proxy_url,
                            blocked_by,
                        )
                    await asyncio.sleep(settings.retry_backoff_seconds * attempt)
                    continue

                cards = self._parse_cards(rendered, limit)
                self.client.proxy_manager.mark_success(proxy_url)
                if cards:
                    self.last_block_reason = None
                    return cards
                logger.warning("Wildberries parse produced 0 cards, attempt=%s url=%s", attempt, url)
            except Exception as exc:  # noqa: BLE001
                if str(exc) == "Marketplace blocked automated access":
                    raise RuntimeError("Marketplace blocked automated access") from exc
                self.client.proxy_manager.mark_dead(proxy_url, reason=f"playwright search failed: {exc}", url=url)
                logger.warning("Wildberries Selenium search failed, attempt=%s proxy=%s err=%s", attempt, proxy_url, exc)

            await asyncio.sleep(settings.retry_backoff_seconds * attempt)

        if self.last_block_reason:
            raise RuntimeError(f"Wildberries blocked by anti-bot challenge: {self.last_block_reason}")
        return []

    async def _detail_with_selenium(
        self,
        full_url: str,
        device_profile: str,
        user_agent: str,
    ) -> ProductDetail | None:
        timeout_ms = int((settings.request_timeout_seconds + 10) * 1000)
        for attempt in range(1, settings.max_retries + 1):
            proxy_url = self.client.proxy_manager.next_proxy()
            try:
                rendered = await render_page(
                    url=full_url,
                    timeout_ms=timeout_ms,
                    wait_selectors=self.detail_wait_selectors,
                    proxy_url=proxy_url,
                    scroll=True,
                    device_profile=device_profile,
                    user_agent=user_agent,
                )
                blocked_by = detect_antibot_challenge(rendered)
                if blocked_by:
                    self.last_block_reason = blocked_by
                    self.client.proxy_manager.mark_dead(
                        proxy_url,
                        reason=f"anti-bot challenge: {blocked_by}",
                        url=full_url,
                    )
                    logger.warning("Wildberries Selenium blocked (%s) on detail %s", blocked_by, full_url)
                    continue

                detail = self._parse_detail(rendered, full_url)
                self.client.proxy_manager.mark_success(proxy_url)
                if detail.title:
                    return detail
                logger.warning("Wildberries detail parsed without title, attempt=%s url=%s", attempt, full_url)
            except Exception as exc:  # noqa: BLE001
                if str(exc) == "Marketplace blocked automated access":
                    raise RuntimeError("Marketplace blocked automated access") from exc
                self.client.proxy_manager.mark_dead(
                    proxy_url,
                    reason=f"playwright detail failed: {exc}",
                    url=full_url,
                )
                logger.warning("Wildberries Selenium detail failed, attempt=%s proxy=%s err=%s", attempt, proxy_url, exc)

            await asyncio.sleep(settings.retry_backoff_seconds * attempt)

        return None

    def _extract_primary_seller_from_detail(self, html: str) -> dict[str, str | None]:
        soup = BeautifulSoup(html, "html.parser")

        seller_node = soup.select_one(
            "a[href*='/seller/'], "
            "a[href*='seller'], "
            "[data-testid*='seller'] a, "
            "[class*='seller'] a, "
            "[class*='supplier'] a"
        )

        seller_container: Tag | None = None
        if seller_node:
            seller_container = seller_node.find_parent(
                attrs={"class": re.compile(r"(seller|supplier)", re.IGNORECASE)}
            )

        if not seller_node:
            seller_node = soup.select_one(
                "[data-testid*='seller'], "
                "[class*='sellerName'], "
                "[class*='seller-name'], "
                "[class*='seller__name'], "
                "[class*='supplierName'], "
                "[class*='supplier-name']"
            )

        seller = clean_text(seller_node.get_text(" ", strip=True)) if seller_node else None
        if seller and (len(seller) > 80 or self._is_bad_seller_candidate(seller)):
            seller = None

        if not seller_container and seller_node:
            seller_container = seller_node.find_parent(
                attrs={"class": re.compile(r"(seller|supplier)", re.IGNORECASE)}
            )

        seller_text = clean_text(
            seller_container.get_text(" ", strip=True)
            if seller_container
            else seller_node.get_text(" ", strip=True)
            if seller_node
            else ""
        )

        return {
            "seller": seller,
            "seller_rating": format_rating(seller_text),
            "seller_reviews_count": extract_reviews_count(seller_text),
        }

    @staticmethod
    def _is_bad_seller_candidate(value: str | None) -> bool:
        text = clean_text(value).lower()
        if not text:
            return True

        bad_substrings = (
            "стать продавцом",
            "продавать на",
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
            "0-0-12",
        )
        if any(token in text for token in bad_substrings):
            return True

        return bool(re.search(r"\d[\d\s\xa0]*(?:₸|тг|₽|руб|тенге)", text))

    async def _enrich_cards_with_sellers(
        self,
        cards: list[ProductCard],
        device_profile: str,
        user_agent: str,
        max_items: int = 5,
    ) -> list[ProductCard]:
        if not cards:
            return cards

        timeout_ms = int((settings.request_timeout_seconds + 10) * 1000)
        enriched_cards: list[ProductCard] = []

        for index, card in enumerate(cards):
            if index >= max_items:
                enriched_cards.append(card)
                continue

            try:
                rendered = await render_page(
                    url=urljoin(self.base_url, card.product_url),
                    timeout_ms=timeout_ms,
                    wait_selectors=[
                        "a[href*='/seller/']",
                        "[data-testid*='seller']",
                        "[class*='seller']",
                        "[class*='supplier']",
                        "main",
                    ],
                    proxy_url=self.client.proxy_manager.next_proxy(),
                    scroll=True,
                    device_profile=device_profile,
                    user_agent=user_agent,
                )
                seller_data = self._extract_primary_seller_from_detail(rendered)
                enriched_cards.append(
                    card.model_copy(
                        update={
                            "seller": seller_data.get("seller"),
                            "seller_rating": seller_data.get("seller_rating"),
                            "seller_reviews_count": seller_data.get("seller_reviews_count"),
                        }
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Wildberries seller enrichment failed url=%s err_type=%s err=%r",
                    card.product_url,
                    type(exc).__name__,
                    exc,
                )
                enriched_cards.append(card)

        return enriched_cards

    def _parse_cards(self, html: str, limit: int) -> list[ProductCard]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[ProductCard] = []
        seen: set[str] = set()
        skipped_ads = 0
        skipped_missing_price = 0
        skipped_title = 0

        for link in soup.select('a[href*="/catalog/"][href*="detail.aspx"]'):
            href = link.get("href") or ""
            if "detail.aspx" not in href or "/catalog/0/" in href:
                continue

            product_url = normalize_link(self.base_url, href)
            if not product_url:
                continue
            if product_url in seen:
                continue

            container = link.find_parent(["article", "li", "div"])
            title = choose_first_non_empty(
                [
                    link.get("aria-label"),
                    link.get("title"),
                    first_text(
                        container,
                        [
                            "[class*='product-card__name']",
                            "[class*='goods-name']",
                            "[class*='name']",
                            "h2",
                            "h3",
                        ],
                    )
                    if container
                    else None,
                    clean_text(link.get_text(" ", strip=True)),
                ]
            )
            if not looks_like_product_title(title):
                skipped_title += 1
                continue

            blob_text = clean_text(container.get_text(" ", strip=True)) if container else clean_text(link.get_text(" ", strip=True))
            if looks_like_banner_or_ad(title, blob_text):
                skipped_ads += 1
                continue

            price = self._extract_price(container, blob_text)
            if not price:
                skipped_missing_price += 1
                continue

            rating = self._extract_rating(container, blob_text)
            reviews_count = self._extract_reviews(container, blob_text)
            image_url = self._extract_image(container or link)
            seen.add(product_url)
            items.append(
                ProductCard(
                    source=self.source,
                    title=title,
                    image_url=image_url,
                    price=price,
                    product_url=product_url,
                    rating=rating,
                    reviews_count=reviews_count,
                )
            )
            if len(items) >= limit:
                break

        logger.info(
            "Wildberries parse stats: parsed=%s skipped_ads=%s skipped_title=%s skipped_missing_price=%s",
            len(items),
            skipped_ads,
            skipped_title,
            skipped_missing_price,
        )
        self.last_search_total_found = extract_total_results_count(html)
        self.last_search_sellers = sorted(
            {
                item.seller.strip()
                for item in items
                if isinstance(item.seller, str) and item.seller.strip()
            },
            key=str.lower,
        )
        if not items:
            logger.warning("Wildberries parser found no relevant product cards")

        return items

    def _parse_detail(self, html: str, full_url: str) -> ProductDetail:
        soup = BeautifulSoup(html, "html.parser")
        jsonld = extract_product_jsonld(soup)

        title = choose_first_non_empty(
            [
                jsonld.get("title"),
                first_text(soup, ["h1", "h2[class*='productTitle']", "[class*='product-page__title']", "[class*='product-title']"]),
            ]
        ) or ""

        image_url = choose_first_non_empty(
            [
                jsonld.get("image_url"),
                first_attr(
                    soup,
                    [
                        "div[class*='productPageSlider'] img[src]",
                        "div.product-page__slider img[src]",
                        "div.zoom-image-container img[src]",
                        "[class*='swiper'] img[src]",
                    ],
                    "src",
                ),
                first_attr(soup, ["img[data-src]"], "data-src"),
                first_attr(soup, ["meta[property='og:image']"], "content"),
            ]
        )
        image_url = normalize_link(self.base_url, image_url) if image_url else None

        price_raw = choose_first_non_empty(
            [
                jsonld.get("price"),
                first_text(
                    soup,
                    [
                        "ins[class*='priceBlockFinalPrice']",
                        "[class*='price-block'] [class*='price']",
                        "[class*='final-price']",
                        "[class*='price']",
                        "meta[property='product:price:amount']",
                    ],
                ),
                first_attr(soup, ["meta[property='product:price:amount']"], "content"),
            ]
        )
        price = format_price(price_raw) or clean_text(price_raw)

        rating_raw = choose_first_non_empty(
            [
                jsonld.get("rating"),
                first_text(soup, ["[class*='rating']", "[class*='valuation']", "[class*='feedbacks']"]),
            ]
        )
        rating = format_rating(rating_raw)

        reviews_raw = choose_first_non_empty(
            [
                jsonld.get("reviews_count"),
                first_text(soup, ["[class*='feedbacks']", "[class*='reviews']", "[class*='comment']"]),
            ]
        )
        reviews_count = extract_reviews_count(reviews_raw)

        description = choose_first_non_empty(
            [
                jsonld.get("description"),
                first_text(soup, ["div[class*='productDescription']", "div[class*='product-page__description']", "section[class*='description']"]),
                first_attr(soup, ["meta[name='description']"], "content"),
            ]
        )

        characteristics = gather_key_value(
            soup,
            row_selector="tr, li, [class*='charc'], [class*='option'], [class*='specification'], [class*='product-params__row']",
            key_selector="th, [class*='name'], [class*='title'], [class*='product-params__cell-title']",
            value_selector="td, [class*='value'], [class*='text'], [class*='product-params__cell']:not([class*='product-params__cell-title'])",
        )

        raw_sections = {
            "headings": [clean_text(h.get_text(" ", strip=True)) for h in soup.select("h2, h3")[:14]],
            "bullet_points": [clean_text(li.get_text(" ", strip=True)) for li in soup.select("ul li")[:20]],
        }

        if not price:
            logger.warning("Wildberries detail: price not found for %s", full_url)
        if not rating:
            logger.info("Wildberries detail: rating not found for %s", full_url)

        return ProductDetail(
            source=self.source,
            title=title,
            product_url=full_url,
            image_url=image_url,
            price=price,
            rating=rating,
            reviews_count=reviews_count,
            description=description,
            characteristics=characteristics,
            raw_sections=raw_sections,
        )

    def _extract_price(self, container: BeautifulSoup | None, blob_text: str) -> str | None:
        candidates: list[str | None] = []
        if container:
            candidates.extend(
                [
                    first_text(
                        container,
                        [
                            "[class*='price__lower-price']",
                            "[class*='price']",
                            "ins",
                            "strong",
                        ],
                    ),
                    first_attr(container, ["meta[itemprop='price']"], "content"),
                ]
            )

        if any(token in blob_text.lower() for token in ("₸", "₽", "тг", "тенге", "руб")):
            candidates.append(blob_text)

        for candidate in candidates:
            price = format_price(candidate)
            if price:
                return price
        return None

    def _extract_rating(self, container: BeautifulSoup | None, blob_text: str) -> str | None:
        candidates: list[str | None] = []
        if container:
            candidates.extend(
                [
                    first_text(container, ["[class*='rating']", "[class*='address-rate']"]),
                    first_attr(container, ["[aria-label*='рейтинг']"], "aria-label"),
                ]
            )
        candidates.append(blob_text)

        for candidate in candidates:
            parsed = format_rating(candidate)
            if parsed:
                return parsed
        return None

    def _extract_reviews(self, container: BeautifulSoup | None, blob_text: str) -> str | None:
        candidates: list[str | None] = []
        if container:
            candidates.extend(
                [
                    first_text(container, ["[class*='feedbacks']", "[class*='reviews']", "[class*='comment']"]),
                    first_attr(container, ["[aria-label*='отзыв']"], "aria-label"),
                    first_text(container, ["[class*='address-rate']"]),
                ]
            )
        candidates.append(blob_text)

        for candidate in candidates:
            parsed = extract_reviews_count(candidate)
            if parsed:
                return parsed
        return None

    def _extract_image(self, container: BeautifulSoup) -> str | None:
        img = container.select_one("img")
        if not img:
            return None

        for attr in ("src", "data-src", "data-original", "data-lazy"):
            value = clean_text(str(img.get(attr) or ""))
            if value and not value.startswith("data:image"):
                return normalize_link(self.base_url, value)

        srcset = clean_text(str(img.get("srcset") or img.get("data-srcset") or ""))
        if srcset:
            first_url = clean_text(srcset.split(",")[0].split(" ")[0])
            if first_url and not first_url.startswith("data:image"):
                return normalize_link(self.base_url, first_url)

        return None
