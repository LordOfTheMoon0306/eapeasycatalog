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
    first_attr,
    first_text,
    format_price,
    format_rating,
    gather_key_value,
    extract_total_results_count,
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

OZON_DETAIL_CHALLENGE_MESSAGE = "Ozon requires browser verification. Product details are temporarily unavailable."


class OzonAdapter(MarketplaceAdapter):
    source = SourceName.ozon
    base_url = "https://www.ozon.kz"
    search_wait_selectors = [
        "a[href*='/product/']",
        "[data-widget*='searchResultsV2']",
        "main",
    ]
    detail_wait_selectors = [
        "h1",
        "[data-widget='webProductHeading']",
        "[class*='price']",
    ]

    def __init__(self, client: RequestClient):
        self.client = client
        self.last_block_reason: str | None = None
        self.last_search_total_found: int | None = None
        self.last_search_sellers: list[str] = []

    @staticmethod
    def _resolve_device_profile() -> str:
        configured = (settings.ozon_device_profile or settings.device_profile_default).strip().lower()
        if configured not in {"desktop", "mobile"}:
            return settings.device_profile_default
        return configured

    async def search(self, query: str, limit: int = 10) -> list[ProductCard]:
        url = f"{self.base_url}/search/?text={quote_plus(query)}"
        profile = self._resolve_device_profile()
        user_agent = self.client.pick_user_agent(profile)
        self.last_block_reason = None
        self.last_search_total_found = None
        self.last_search_sellers = []

        apify_api_key = settings.apify_api_key.strip()
        if apify_api_key:
            logger.info("Using Apify directly for Ozon search: %s", url)
            try:
                from apify_client import ApifyClientAsync
                client = ApifyClientAsync(apify_api_key)
                run_input = {
                    "startUrls": [{"url": url}],
                    "maxItems": limit,  # many actors respect this
                }
                run = await client.actor(settings.apify_ozon_actor_id).call(run_input=run_input)
                dataset = await client.dataset(run["defaultDatasetId"]).list_items()
                
                apify_cards = []
                for item in dataset.items:
                    title = item.get("title") or item.get("name") or ""
                    if not title:
                        continue
                    
                    price_raw = str(item.get("cardPrice") or item.get("price") or item.get("originalPrice") or "")
                    if price_raw and "₽" not in price_raw:
                        price_raw = str(price_raw).replace("\u2009", "").replace(" ", "").strip() + " ₽"

                    image_url = item.get("images", [None])[0] if item.get("images") else item.get("image")
                    product_url = item.get("url") or item.get("productUrl") or ""
                    seller = self._clean_seller_candidate(
                        item.get("seller") or item.get("sellerName") or item.get("shop") or item.get("merchant")
                    )
                    rating = str(item.get("rating") or "")
                    reviews_count = str(item.get("reviewCount") or item.get("reviewsCount") or "")
                    
                    apify_cards.append(ProductCard(
                        source=self.source,
                        title=title,
                        image_url=image_url or "",
                        price=price_raw,
                        seller=seller,
                        product_url=product_url,
                        rating=rating,
                        reviews_count=reviews_count,
                    ))
                
                if apify_cards:
                    # Fake total found based on what we got, Apify doesn't always provide stats
                    self.last_search_total_found = len(apify_cards) 
                    self.last_search_sellers = sorted(list({c.seller.strip() for c in apify_cards if c.seller and c.seller.strip()}), key=str.lower)
                    return apify_cards[:limit]
            except Exception as exc:
                logger.warning("Ozon Apify search failed, falling back: %s", exc)

        logger.info("Falling back to Selenium for Ozon search")
        try:
            cards = await self._search_with_selenium(
                url=url,
                limit=limit,
                device_profile=profile,
                user_agent=user_agent,
            )
        except RuntimeError as exc:
            if str(exc) == "Marketplace blocked automated access":
                raise
            logger.warning("Ozon Selenium search exhausted, trying HTTP fallback: %s", exc)
            cards = []

        if cards:
            return cards[:limit]

        logger.warning("Ozon Selenium search returned no cards, trying HTTP fallback: %s", url)
        try:
            html = await self.client.fetch_text(
                url,
                source=self.source.value,
                device_profile=profile,
                user_agent=user_agent,
            )
            cards = self._parse_cards(html.text, limit)
            if cards:
                return cards
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ozon HTTP fallback failed: %s", exc)

        if self.last_block_reason:
            raise RuntimeError(f"Ozon blocked by anti-bot challenge: {self.last_block_reason}")
        raise RuntimeError("Ozon parser returned no products")

    async def get_product_details(self, product_url: str) -> ProductDetail:
        full_url = urljoin(self.base_url, product_url)
        apify_api_key = settings.apify_api_key.strip()
        apify_error: Exception | None = None
        apify_attempted = False
        if apify_api_key:
            apify_attempted = True
            try:
                from apify_client import ApifyClientAsync

                client = ApifyClientAsync(apify_api_key)
                run_input = {
                    "startUrls": [{"url": full_url}],
                }
                logger.info("Starting Apify task for Ozon: %s", full_url)
                run = await client.actor(settings.apify_ozon_actor_id).call(run_input=run_input)
                dataset = await client.dataset(run["defaultDatasetId"]).list_items()
                items = dataset.items

                if items:
                    item = items[0]
                    title = item.get("title", "")

                    # Check multiple price fields and avoid zero-balance outputs
                    price = str(item.get("cardPrice") or item.get("price") or item.get("originalPrice") or "")

                    # Format price
                    if price and "₽" not in price:
                        price = price.replace("\u2009", "").replace(" ", "").strip() + " ₽"
                    elif price:
                        price = price.replace("\u2009", " ")

                    rating = str(item.get("rating") or "")
                    reviews_count = str(item.get("reviewCount") or "").replace("\xa0", " ")

                    # Fetch first image
                    image_url = item.get("images", [None])[0] if item.get("images") else ""
                    if not image_url and item.get("descriptionImages"):
                        image_url = item.get("descriptionImages")[0]

                    description = item.get("richDescription") or item.get("description") or ""
                    brand = item.get("brand") or ""

                    # Fetch variants
                    variants_list = []
                    for var_group in item.get("variants", []):
                        group_name = var_group.get("aspectName", "Вариант")
                        options = var_group.get("options", [])
                        var_options = [opt.get("name", "") for opt in options if opt.get("name")]
                        if var_options:
                            variants_list.append(
                                {
                                    "name": group_name,
                                    "options": var_options,
                                }
                            )

                    characteristics = item.get("characteristics", [])
                    attributes = {char.get("name", ""): char.get("value", "") for char in characteristics}
                    if brand and "Бренд" not in attributes.keys():
                        attributes["Бренд"] = brand

                    # Keep variants visible in the existing characteristics table.
                    if variants_list:
                        attributes["Доступные варианты"] = "; ".join(
                            f"{v['name']}: {', '.join(v['options'])}" for v in variants_list
                        )

                    if title:
                        return ProductDetail(
                            source=self.source,
                            title=title,
                            product_url=full_url,
                            price=price,
                            rating=rating,
                            reviews_count=reviews_count,
                            image_url=image_url,
                            description=description,
                            characteristics=attributes,
                        )
                apify_error = RuntimeError("Apify returned no usable Ozon detail item")
            except Exception as exc:
                apify_error = exc
                logger.warning("Apify failed, falling back to original code: %s", exc)
        else:
            logger.info("APIFY_API_KEY is empty, skipping Apify for Ozon detail")

        if not settings.enable_playwright_fallback:
            if apify_attempted:
                reason = str(apify_error) if apify_error else "Apify did not return product details"
                raise RuntimeError(f"Ozon detail fallback disabled; Apify failed: {reason}")
            raise RuntimeError("Ozon detail fallback disabled and APIFY_API_KEY is empty")

        profile = self._resolve_device_profile()
        user_agent = self.client.pick_user_agent(profile)

        detail = await self._detail_with_selenium(
            full_url,
            device_profile=profile,
            user_agent=user_agent,
        )
        if detail and detail.title:
            return detail

        logger.warning("Ozon Selenium detail parse incomplete, trying HTTP fallback: %s", full_url)
        html = await self.client.fetch_text(
            full_url,
            source=self.source.value,
            device_profile=profile,
            user_agent=user_agent,
        )
        if self._is_detail_challenge(html.text):
            raise RuntimeError(OZON_DETAIL_CHALLENGE_MESSAGE)
        fallback_detail = self._parse_detail(html.text, full_url)
        if fallback_detail and fallback_detail.title:
            return fallback_detail
        raise RuntimeError("Failed to parse Ozon product details")

    async def _search_with_selenium(
        self,
        url: str,
        limit: int,
        device_profile: str,
        user_agent: str,
    ) -> list[ProductCard]:
        timeout_ms = int((settings.request_timeout_seconds + 12) * 1000)
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
                    user_agent=None,
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
                            "Ozon anti-bot challenge detected, attempt=%s proxy=%s marker=%s",
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
                logger.warning("Ozon parse produced 0 cards, attempt=%s url=%s", attempt, url)
            except Exception as exc:  # noqa: BLE001
                if str(exc) == "Marketplace blocked automated access":
                    raise RuntimeError("Marketplace blocked automated access") from exc
                self.client.proxy_manager.mark_dead(proxy_url, reason=f"playwright search failed: {exc}", url=url)
                logger.warning("Ozon Selenium search failed, attempt=%s proxy=%s err=%s", attempt, proxy_url, exc)

            await asyncio.sleep(settings.retry_backoff_seconds * attempt)

        return []

    async def _detail_with_selenium(
        self,
        full_url: str,
        device_profile: str,
        user_agent: str,
    ) -> ProductDetail | None:
        timeout_ms = int((settings.request_timeout_seconds + 12) * 1000)
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
                    user_agent=None,
                )
                if self._is_detail_challenge(rendered):
                    raise RuntimeError(OZON_DETAIL_CHALLENGE_MESSAGE)
                detail = self._parse_detail(rendered, full_url)
                self.client.proxy_manager.mark_success(proxy_url)
                if detail.title:
                    return detail
                logger.warning("Ozon detail parsed without title, attempt=%s url=%s", attempt, full_url)
            except RuntimeError as exc:
                if str(exc) == "Marketplace blocked automated access":
                    raise
                if str(exc) == OZON_DETAIL_CHALLENGE_MESSAGE:
                    self.client.proxy_manager.mark_dead(
                        proxy_url,
                        reason="ozon browser verification required",
                        url=full_url,
                    )
                    raise
                self.client.proxy_manager.mark_dead(
                    proxy_url,
                    reason=f"playwright detail failed: {exc}",
                    url=full_url,
                )
                logger.warning("Ozon Selenium detail failed, attempt=%s proxy=%s err=%s", attempt, proxy_url, exc)
            except Exception as exc:  # noqa: BLE001
                if str(exc) == "Marketplace blocked automated access":
                    raise RuntimeError("Marketplace blocked automated access") from exc
                self.client.proxy_manager.mark_dead(
                    proxy_url,
                    reason=f"playwright detail failed: {exc}",
                    url=full_url,
                )
                logger.warning("Ozon Selenium detail failed, attempt=%s proxy=%s err=%s", attempt, proxy_url, exc)

            await asyncio.sleep(settings.retry_backoff_seconds * attempt)

        return None

    @staticmethod
    def _is_detail_challenge(html: str | None) -> bool:
        text = clean_text(html).lower()
        if not text:
            return False

        challenge_markers = (
            "сопоставьте пазл",
            "двигая ползунок",
            "проверяем браузер",
            "captcha",
            "challenge",
        )
        return any(marker in text for marker in challenge_markers)

    def _parse_cards(self, html: str, limit: int) -> list[ProductCard]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[ProductCard] = []
        seen: set[str] = set()
        skipped_ads = 0
        skipped_missing_price = 0
        skipped_title = 0

        for link in soup.select('a[href*="/product/"]'):
            href = link.get("href") or ""
            if "/product/" not in href:
                continue

            product_url = normalize_link(self.base_url, href)
            if not product_url:
                continue
            if product_url in seen:
                continue

            container = link.find_parent(["article", "li", "div"])
            title = choose_first_non_empty(
                [
                    link.get("title"),
                    link.get("aria-label"),
                    first_text(
                        container,
                        [
                            "[class*='tile-hover-target']",
                            "[class*='tsBody500Medium']",
                            "[class*='title']",
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
            seller = self._extract_seller_from_card(container)

            seen.add(product_url)
            items.append(
                ProductCard(
                    source=self.source,
                    title=title,
                    image_url=image_url,
                    price=price,
                    seller=seller,
                    product_url=product_url,
                    rating=rating,
                    reviews_count=reviews_count,
                )
            )
            if len(items) >= limit:
                break

        logger.info(
            "Ozon parse stats: parsed=%s skipped_ads=%s skipped_title=%s skipped_missing_price=%s",
            len(items),
            skipped_ads,
            skipped_title,
            skipped_missing_price,
        )
        self.last_search_total_found = self._extract_ozon_total(html)
        self.last_search_sellers = sorted(
            {
                item.seller.strip()
                for item in items
                if isinstance(item.seller, str) and item.seller.strip()
            },
            key=str.lower,
        )
        if not items:
            logger.warning("Ozon parser found no relevant product cards")

        logger.info("Ozon total_found=%s for query", self.last_search_total_found)
        return items

    @staticmethod
    def _clean_seller_candidate(value: str | None) -> str | None:
        seller = clean_text(value)
        if not seller:
            return None

        seller = re.sub(
            r"^(?:продавец|магазин|seller|shop)\s*[:\-]?\s*",
            "",
            seller,
            flags=re.IGNORECASE,
        )
        seller = clean_text(seller)
        if not seller or len(seller) > 80:
            return None

        lowered = seller.lower()
        blocked_tokens = (
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
            "доставка",
            "товар",
            "цена",
            "корзин",
            "рассроч",
            "в месяц",
            "0-0-12",
            "₸",
            "₽",
            "тг",
            "руб",
            "тенге",
        )
        if any(token in lowered for token in blocked_tokens):
            return None
        if re.search(r"\d[\d\s\xa0]*(?:₸|тг|₽|руб|тенге)", lowered):
            return None
        if re.search(r"(?:₸|тг|₽|руб|тенге)\s*\d", lowered):
            return None
        if re.search(r"\b\d+(?:[.,]\d+)?\s*(?:отзыв|отзыва|отзывов)\b", lowered):
            return None
        if re.search(r"(?:x|×|х)\s*\d+\s*(?:мес|месяц)", lowered):
            return None

        return seller

    def _extract_seller_from_card(self, container: Tag | None) -> str | None:
        if not container:
            return None

        for selector in (
            "[data-widget*='seller']",
            "[data-testid*='seller']",
            "[class*='seller']",
            "a[href*='/seller/']",
            "a[href*='/shop/']",
            "a[href*='seller']",
        ):
            for node in container.select(selector)[:5]:
                seller = self._clean_seller_candidate(node.get_text(" ", strip=True))
                if seller:
                    return seller

        label_pattern = re.compile(
            r"(?:продавец|магазин|seller|shop)\s*[:\-]?\s*(.{2,80})",
            flags=re.IGNORECASE,
        )
        direct_texts: list[str] = []
        direct_texts.extend(
            clean_text(str(text_node))
            for text_node in container.find_all(string=True, recursive=False)
        )
        for child in container.find_all(recursive=False):
            if isinstance(child, Tag):
                direct_texts.extend(
                    clean_text(str(text_node))
                    for text_node in child.find_all(string=True, recursive=False)
                )

        for text in direct_texts:
            match = label_pattern.search(text)
            if not match:
                continue
            seller = self._clean_seller_candidate(match.group(1))
            if seller:
                return seller

        return None

    @staticmethod
    def _extract_ozon_total(html: str) -> int | None:
        """Extract total product count from Ozon's rendered HTML.

        Ozon embeds its state in several forms:
        - JSON script tags with keys like totalFound / total / catalogTotal
        - Text like "12 345 товаров"
        """
        import re as _re

        # Priority 1: Ozon-specific JSON keys in script tags
        ozon_patterns = [
            _re.compile(r'"totalFound"\s*:\s*(\d{1,12})'),
            _re.compile(r'"foundCount"\s*:\s*(\d{1,12})'),
            _re.compile(r'"total"\s*:\s*(\d{1,12})'),
            _re.compile(r'"catalogTotal"\s*:\s*(\d{1,12})'),
            _re.compile(r'"paginationTotal"\s*:\s*(\d{1,12})'),
            _re.compile(r'"totalCount"\s*:\s*(\d{1,12})'),
            _re.compile(r'"itemsTotal"\s*:\s*(\d{1,12})'),
        ]

        candidates: list[int] = []

        for pattern in ozon_patterns:
            for m in pattern.finditer(html):
                value = int(m.group(1))
                if 1 <= value <= 50_000_000:
                    candidates.append(value)

        if candidates:
            # Use the max plausible value
            result = max(candidates)
            logger.debug("Ozon total from JSON patterns: %s (candidates: %s)", result, candidates[:5])
            return result

        # Priority 2: Fallback to generic extractor
        from app.adapters.common import extract_total_results_count
        return extract_total_results_count(html)


    def _parse_detail(self, html: str, full_url: str) -> ProductDetail:
        if self._is_detail_challenge(html):
            raise RuntimeError(OZON_DETAIL_CHALLENGE_MESSAGE)

        soup = BeautifulSoup(html, "html.parser")
        jsonld = extract_product_jsonld(soup)

        title = choose_first_non_empty(
            [
                jsonld.get("title"),
                first_text(soup, ["h1", "[data-widget='webProductHeading']", "[class*='title']"]),
            ]
        ) or ""

        image_url = choose_first_non_empty(
            [
                jsonld.get("image_url"),
                first_attr(soup, ["meta[property='og:image']"], "content"),
                first_attr(soup, ["[data-widget*='gallery'] img[src]", "img[src]"], "src"),
                first_attr(soup, ["img[data-src]"], "data-src"),
            ]
        )
        image_url = normalize_link(self.base_url, image_url) if image_url else None

        price_raw = choose_first_non_empty(
            [
                jsonld.get("price"),
                first_text(
                    soup,
                    [
                        "[data-widget*='webPrice']",
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
                first_text(soup, ["[class*='rating']", "[data-widget*='reviews']"]),
            ]
        )
        rating = format_rating(rating_raw)

        reviews_raw = choose_first_non_empty(
            [
                jsonld.get("reviews_count"),
                first_text(soup, ["[data-widget*='reviews']", "[class*='review']", "[class*='feedback']"]),
            ]
        )
        reviews_count = extract_reviews_count(reviews_raw)

        description = choose_first_non_empty(
            [
                jsonld.get("description"),
                first_text(soup, ["[data-widget*='description']", "[class*='description']"]),
                first_attr(soup, ["meta[name='description']"], "content"),
            ]
        )

        characteristics = gather_key_value(
            soup,
            row_selector="tr, li, [class*='attribute'], [class*='spec'], [class*='character']",
            key_selector="th, [class*='name'], [class*='key'], [class*='title']",
            value_selector="td, [class*='value'], [class*='text']",
        )

        if not price:
            logger.warning("Ozon detail: price not found for %s", full_url)
        if not rating:
            logger.info("Ozon detail: rating not found for %s", full_url)

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
            raw_sections={},
        )

    def _extract_price(self, container: BeautifulSoup | None, blob_text: str) -> str | None:
        def clean_installments(t: str | None) -> str | None:
            if not t: return t
            t = re.sub(
                r"\d[\d\s\xa0]*(?:₸|тг|₽|руб|тенге)\s*(?:[xх×]\s*\d+\s*мес|в\s*месяц|в\s*рассрочку|рассрочк|за\s*мес)",
                "", t, flags=re.IGNORECASE
            )
            t = re.sub(
                r"(?:[xх×]\s*\d+\s*мес|в\s*месяц|в\s*рассрочку|рассрочк|за\s*мес)\s*\d[\d\s\xa0]*(?:₸|тг|₽|руб|тенге)",
                "", t, flags=re.IGNORECASE
            )
            return t

        clean_blob = clean_installments(blob_text) or ""

        candidates: list[str | None] = []
        if container:
            candidates.extend(
                [
                    clean_installments(first_text(
                        container,
                        [
                            "[class*='price']",
                            "[data-widget*='price']",
                            "strong",
                            "ins",
                        ],
                    )),
                    first_attr(container, ["meta[itemprop='price']"], "content"),
                ]
            )

        if any(token in clean_blob.lower() for token in ("₸", "₽", "тг", "тенге", "руб")):
            candidates.append(clean_blob)

        for candidate in candidates:
            price = format_price(candidate)
            if price:
                return price
                
        logger.debug(f"NO PRICE FOUND. base: '{blob_text}', cleaned: '{clean_blob}', cands: {candidates}")
        return None

    def _extract_rating(self, container: BeautifulSoup | None, blob_text: str) -> str | None:
        candidates: list[str | None] = []
        if container:
            candidates.extend(
                [
                    first_text(container, ["[class*='rating']", "[data-widget*='reviews']"]),
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
                    first_text(container, ["[class*='review']", "[class*='feedback']", "[data-widget*='reviews']"]),
                    first_attr(container, ["[aria-label*='отзыв']"], "aria-label"),
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
