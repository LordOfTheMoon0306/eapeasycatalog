from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from app.adapters.base import MarketplaceAdapter
from app.adapters.common import (
    choose_first_non_empty,
    clean_text,
    detect_antibot_challenge,
    extract_product_jsonld,
    extract_rating_from_class_tokens,
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


class KaspiAdapter(MarketplaceAdapter):
    source = SourceName.kaspi
    base_url = "https://kaspi.kz"
    search_wait_selectors = [
    ".item-card",
    ".item-card__name",
    ".item-card__prices",
    "a.item-card__image-wrapper[href*='/shop/p/']",
    ]
    detail_wait_selectors = ["h1", "[class*='price']", "main"]

    def __init__(self, client: RequestClient):
        self.client = client
        self.last_block_reason: str | None = None
        self.last_search_total_found: int | None = None
        self.last_search_sellers: list[str] = []

    @staticmethod
    def _resolve_device_profile() -> str:
        configured = (settings.kaspi_device_profile or settings.device_profile_default).strip().lower()
        if configured not in {"desktop", "mobile"}:
            return settings.device_profile_default
        return configured

    @staticmethod
    def _resolve_card_container(link: Tag) -> Tag:
        """
        Kaspi card structure:
        div.item-card
        a.item-card__image-wrapper
        div.item-card__info
            div.item-card__name
            div.item-card__rating
            div.item-card__prices

        The old logic returned item-card__image-wrapper too early,
        so title and price were outside the parsed container.
        """

        # 1. Exact Kaspi product card container
        for parent in link.parents:
            if not isinstance(parent, Tag):
                continue

            class_blob = " ".join(parent.get("class") or []).lower()

            if "item-card" in class_blob and "item-card__" not in class_blob:
                return parent

            if parent.has_attr("data-product-id"):
                return parent

            if parent.name in {"main", "section", "body", "html"}:
                break

        # 2. Generic fallback
        for parent in link.parents:
            if not isinstance(parent, Tag):
                continue

            links_in_parent = len(parent.select('a[href*="/shop/p/"]'))
            if links_in_parent == 0:
                continue

            class_blob = " ".join(parent.get("class") or []).lower()
            testid = clean_text(str(parent.get("data-testid") or "")).lower()

            # Skip inner sub-blocks like item-card__image-wrapper
            if "__" in class_blob:
                continue

            if parent.name in {"article", "li"} and links_in_parent <= 6:
                return parent

            if parent.name == "div":
                if links_in_parent <= 6 and any(token in class_blob for token in ("product", "card", "tile", "goods")):
                    return parent
                if links_in_parent <= 6 and ("product" in testid or "card" in testid or "goods" in testid):
                    return parent

            if parent.name in {"main", "section", "body", "html"}:
                break

        return link

    @staticmethod
    def _looks_noisy_title(value: str) -> bool:
        text = clean_text(value)
        if not text:
            return True

        lowered = text.lower()
        if any(token in lowered for token in ("₸", "₽", " с учетом бонусов", " × ", " x ")):
            return True

        digit_count = len(re.findall(r"\d", text))
        letter_count = len(re.findall(r"[A-Za-zА-Яа-я]", text))
        return digit_count > letter_count

    @staticmethod
    def _sanitize_title_candidate(value: str | None) -> str | None:
        text = clean_text(value)
        if not text:
            return None

        # Drop leading counters/bullets that often appear in aggregated card text.
        leading_alpha = re.search(r"[A-Za-zА-Яа-яЁё].*", text)
        if leading_alpha:
            text = leading_alpha.group(0)

        # Remove common trailing commerce fragments that are not part of title.
        text = re.sub(r"\s+с учетом бонусов.*$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+[1-5](?:[\.,]\d+)?\s*\(\d+\)\s*$", "", text)
        text = re.sub(r"\s+\d[\d\s]{2,}\s*[₸₽]\s*[x×]\s*\d+\s*$", "", text)
        text = re.sub(r"\s+\d[\d\s]{2,}\s*[₸₽]\s*$", "", text)
        text = clean_text(text)
        return text or None

    @staticmethod
    def _has_cyrillic(value: str | None) -> bool:
        if not value:
            return False
        return bool(re.search(r"[А-Яа-яЁё]", value))

    def _extract_card_title(self, link: Tag, container: Tag) -> str | None:
        candidates: list[str | None] = [
            first_text(container, [".item-card__name"]),
            link.get("aria-label"),
            link.get("title"),
            first_text(
                container,
                [
                    "[data-testid*='title']",
                    "[class*='title']",
                    "[class*='name']",
                    "h2",
                    "h3",
                ],
            ),
            first_attr(link, ["img[alt]"], "alt"),
            first_attr(container, ["img[alt]"], "alt"),
            clean_text(link.get_text(" ", strip=True)),
        ]

        for raw_candidate in candidates:
            candidate = self._sanitize_title_candidate(raw_candidate)
            if not candidate:
                continue
            if self._looks_noisy_title(candidate):
                continue
            if not looks_like_product_title(candidate):
                continue
            return candidate

        return None

    @staticmethod
    def _parse_characteristic_line(raw_line: str) -> tuple[str, str] | None:
        line = raw_line.strip()
        if not line or len(line) > 220:
            return None

        lowered = clean_text(line).lower()
        if not lowered:
            return None
        if any(token in lowered for token in ("характеристик", "продавцы", "доставка", "смотреть все")):
            return None

        left: str | None = None
        right: str | None = None

        if ":" in line:
            maybe_left, maybe_right = line.split(":", 1)
            if maybe_right.strip():
                left, right = maybe_left, maybe_right
        elif re.search(r"\.{2,}", line):
            maybe_left, maybe_right = re.split(r"\.{2,}", line, maxsplit=1)
            if maybe_right.strip():
                left, right = maybe_left, maybe_right
        elif re.search(r"\s{3,}", line):
            maybe_left, maybe_right = re.split(r"\s{3,}", line, maxsplit=1)
            if maybe_right.strip():
                left, right = maybe_left, maybe_right

        if not left or not right:
            return None

        key = clean_text(left)
        value = clean_text(right)
        if not key or not value:
            return None
        if len(key) > 90 or len(value) > 140:
            return None
        return key, value

    def _extract_primary_seller_from_detail(self, html: str) -> dict[str, str | None]:
        soup = BeautifulSoup(html, "html.parser")

        row = soup.select_one(".sellers-table tbody tr")

        if not row:
            return {
                "seller": None,
                "seller_rating": None,
                "seller_reviews_count": None,
            }

        seller_node = row.select_one("a[href*='/shop/info/']")

        seller = clean_text(seller_node.get_text(" ", strip=True)) if seller_node else None
        if self._is_bad_seller_candidate(seller):
            seller = None

        row_text = clean_text(row.get_text(" ", strip=True))

        seller_rating = format_rating(row_text)
        seller_reviews_count = extract_reviews_count(row_text)

        return {
            "seller": seller,
            "seller_rating": seller_rating,
            "seller_reviews_count": seller_reviews_count,
        }

    @staticmethod
    def _is_bad_seller_candidate(value: str | None) -> bool:
        seller = clean_text(value)
        if not seller or len(seller) > 80:
            return True

        lowered = seller.lower()
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
            return True
        if re.search(r"\d[\d\s\xa0]*(?:₸|тг|₽|руб|тенге)", lowered):
            return True
        if re.search(r"(?:₸|тг|₽|руб|тенге)\s*\d", lowered):
            return True
        if re.search(r"\b\d+(?:[.,]\d+)?\s*(?:отзыв|отзыва|отзывов)\b", lowered):
            return True
        if re.search(r"(?:x|×|х)\s*\d+\s*(?:мес|месяц)", lowered):
            return True
        return False
    
    def _extract_characteristics_fallback(self, soup: BeautifulSoup) -> dict[str, str]:
        output: dict[str, str] = {}
        selectors = [
            "[class*='character']",
            "[class*='spec']",
            "[class*='attribute']",
            "[class*='property']",
            "table",
        ]

        for node in soup.select(", ".join(selectors))[:80]:
            if not isinstance(node, Tag):
                continue

            text_block = node.get_text("\n", strip=True)
            if not text_block:
                continue

            for raw_line in text_block.splitlines():
                parsed = self._parse_characteristic_line(raw_line)
                if not parsed:
                    continue
                key, value = parsed
                if key not in output:
                    output[key] = value

        return output

    async def search(self, query: str, limit: int = 10) -> list[ProductCard]:
        url = f"{self.base_url}/shop/search/?text={quote_plus(query)}"
        profile = self._resolve_device_profile()
        user_agent = self.client.pick_user_agent(profile)
        self.last_search_total_found = None
        self.last_search_sellers = []
        
        timeout_ms = int((settings.request_timeout_seconds + 12) * 1000)
        proxy_url = self.client.proxy_manager.next_proxy()
        
        try:
            rendered = await render_page(
                url=url,
                timeout_ms=timeout_ms,
                wait_selectors=self.search_wait_selectors,
                proxy_url=proxy_url,
                scroll=True,
                device_profile=profile,
                user_agent=user_agent,
            )
            cards = self._parse_cards(rendered, limit)
            cards = await self._enrich_cards_with_sellers(cards, max_items=5)
            self.last_search_sellers = sorted(
                {
                    item.seller.strip()
                    for item in cards
                    if isinstance(item.seller, str) and item.seller.strip()
                },
                key=str.lower,
            )

            # If the rendered DOM does not expose global count, try raw HTML once.
            if self.last_search_total_found is None:
                try:
                    html = await self.client.fetch_text(
                        url,
                        source=self.source.value,
                        device_profile=profile,
                        user_agent=user_agent,
                    )
                    parsed_total = extract_total_results_count(html.text)
                    if parsed_total is not None:
                        self.last_search_total_found = parsed_total
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Kaspi total count fallback fetch failed: %s", exc)

            return cards
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kaspi render/search failed: err_type=%s err=%r", type(exc).__name__, exc, exc_info=True,)
            if str(exc) == "Marketplace blocked automated access":
                raise RuntimeError("Marketplace blocked automated access") from exc
            if self.last_block_reason:
                raise RuntimeError(f"Kaspi blocked by anti-bot challenge: {self.last_block_reason}") from exc
            raise RuntimeError("Kaspi parser returned no products") from exc

    async def _enrich_cards_with_sellers(
        self,
        cards: list[ProductCard],
        max_items: int = 5,
    ) -> list[ProductCard]:
        if not cards:
            return cards

        profile = self._resolve_device_profile()
        user_agent = self.client.pick_user_agent(profile)
        timeout_ms = int((settings.request_timeout_seconds + 12) * 1000)

        enriched_cards: list[ProductCard] = []

        for index, card in enumerate(cards):
            if index >= max_items:
                enriched_cards.append(card)
                continue

            try:
                rendered = await render_page(
                    url=card.product_url,
                    timeout_ms=timeout_ms,
                    wait_selectors=[
                        ".sellers-table",
                        "table[class*='seller']",
                        "[class*='seller']",
                        "main",
                    ],
                    proxy_url=self.client.proxy_manager.next_proxy(),
                    scroll=True,
                    device_profile=profile,
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

            except Exception as exc:
                logger.warning(
                    "Kaspi seller enrichment failed url=%s err_type=%s err=%r",
                    card.product_url,
                    type(exc).__name__,
                    exc,
                )
                enriched_cards.append(card)

        return enriched_cards
    
    async def get_product_details(self, product_url: str) -> ProductDetail:
        full_url = urljoin(self.base_url, product_url)
        profile = self._resolve_device_profile()
        user_agent = self.client.pick_user_agent(profile)
        
        timeout_ms = int((settings.request_timeout_seconds + 12) * 1000)
        proxy_url = self.client.proxy_manager.next_proxy()

        try:
            rendered = await render_page(
                url=full_url,
                timeout_ms=timeout_ms,
                wait_selectors=self.detail_wait_selectors,
                proxy_url=proxy_url,
                scroll=True,
                device_profile=profile,
                user_agent=user_agent,
            )
            detail = self._parse_detail(rendered, full_url)
            if detail and detail.title:
                return detail
        except Exception as exc:  # noqa: BLE001
            if str(exc) == "Marketplace blocked automated access":
                raise RuntimeError("Marketplace blocked automated access") from exc
            logger.warning("Kaspi HTTP request failed: %s", exc)
        raise RuntimeError("Failed to parse Kaspi product details")

    def _parse_cards(self, html: str, limit: int) -> list[ProductCard]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[ProductCard] = []
        aggregated: dict[str, dict[str, object]] = {}
        skipped_ads = 0
        skipped_missing_price = 0
        skipped_title = 0

        for link in soup.select('a[href*="/shop/p/"]'):
            if not isinstance(link, Tag):
                continue

            product_url = normalize_link(self.base_url, link.get("href"))
            if not product_url:
                continue
            canonical_url = product_url.replace("&tab=reviews", "").replace("?tab=reviews", "")

            container = self._resolve_card_container(link)
            entry = aggregated.setdefault(
                canonical_url,
                {
                    "title": None,
                    "price": None,
                    "rating": None,
                    "reviews_count": None,
                    "image_url": None,
                    "blob_texts": [],
                },
            )

            title_candidate = self._extract_card_title(link, container)
            if title_candidate:
                existing_title = entry["title"] if isinstance(entry["title"], str) else None
                if not existing_title:
                    entry["title"] = title_candidate
                elif not self._has_cyrillic(existing_title) and self._has_cyrillic(title_candidate):
                    entry["title"] = title_candidate
                elif len(title_candidate) > len(existing_title):
                    entry["title"] = title_candidate

            blob_text = clean_text(container.get_text(" ", strip=True))
            if blob_text:
                casted_blobs: list[str] = entry["blob_texts"]  # type: ignore[assignment]
                casted_blobs.append(blob_text)

            if not entry["price"]:
                entry["price"] = self._extract_card_price(container)
            if not entry["rating"]:
                entry["rating"] = self._extract_card_rating(container)
            if not entry["reviews_count"]:
                entry["reviews_count"] = self._extract_card_reviews_count(container)
            if not entry["image_url"]:
                entry["image_url"] = self._extract_image(container, link=link)

        for product_url, entry in aggregated.items():
            blob_texts: list[str] = entry["blob_texts"]  # type: ignore[assignment]
            merged_blob = " ".join(blob_texts)

            title = choose_first_non_empty(
                [
                    entry["title"] if isinstance(entry["title"], str) else None,
                    self._title_from_product_url(product_url),
                ]
            )
            if not looks_like_product_title(title):
                skipped_title += 1
                continue

            if looks_like_banner_or_ad(title, merged_blob):
                skipped_ads += 1
                continue

            price = entry["price"] if isinstance(entry["price"], str) else None
            if not price:
                skipped_missing_price += 1
                continue

            rating = entry["rating"] if isinstance(entry["rating"], str) else None
            reviews_count = entry["reviews_count"] if isinstance(entry["reviews_count"], str) else None
            image_url = entry["image_url"] if isinstance(entry["image_url"], str) else None

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
            "Kaspi parse stats: parsed=%s skipped_ads=%s skipped_title=%s skipped_missing_price=%s",
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
            logger.warning("Kaspi parser found no relevant product cards")

        return items

    def _extract_card_price(self, container: BeautifulSoup | Tag | None) -> str | None:
        if not container:
            return None

        for node in container.select(
            ".item-card__prices-price, "
            ".item-card__price, "
            "[class*='prices-price'], "
            ".product-card-info__product-price, "
            ".product-price__final-price, "
            ".product-price__final-price-discounted, "
            ".product-price"
        ):
            class_blob = " ".join(node.get("class") or []).lower()
            parent_class_blob = " ".join(node.parent.get("class") or []).lower() if isinstance(node.parent, Tag) else ""
            if "monthly-payment" in class_blob or "monthly-payment" in parent_class_blob:
                continue

            text = clean_text(node.get_text(" ", strip=True))
            match = re.search(r"(\d[\d\s\xa0]{2,})", text)
            if not match:
                continue

            digits = re.sub(r"\D", "", match.group(1))
            if len(digits) < 3:
                continue

            return f"{int(digits):,}".replace(",", " ") + " ₸"

        return None

    def _extract_card_reviews_count(self, container: BeautifulSoup | Tag | None) -> str | None:
        if not container:
            return None

        for node in container.select(
            ".item-card__rating a[href*='tab=review'], "
            "a[href*='tab=review']"
        ):
            text = clean_text(node.get_text(" ", strip=True))
            match = re.search(r"(\d[\d\s\xa0]*)\s*отзыв(?:а|ов)?\b", text, flags=re.IGNORECASE)
            if match:
                return re.sub(r"\D", "", match.group(1))

        return None

    def _extract_card_rating(self, container: BeautifulSoup | Tag | None) -> str | None:
        if not container:
            return None

        for node in container.select(".item-card__rating span.rating, span.rating"):
            for class_name in node.get("class") or []:
                match = re.fullmatch(r"_(\d{2})", str(class_name))
                if not match:
                    continue

                raw_value = int(match.group(1))
                if raw_value < 10 or raw_value > 50:
                    continue

                value = raw_value / 10
                return str(int(value)) if value.is_integer() else str(value)

        return None

    def _parse_detail(self, html: str, full_url: str) -> ProductDetail:
        soup = BeautifulSoup(html, "html.parser")
        jsonld = extract_product_jsonld(soup)

        title = choose_first_non_empty(
            [
                jsonld.get("title"),
                first_text(soup, ["h1", "[data-testid*='title']", "[class*='title']"]),
            ]
        ) or ""

        image_url = choose_first_non_empty(
            [
                jsonld.get("image_url"),
                first_attr(soup, ["meta[property='og:image']"], "content"),
                first_attr(soup, ["img[src]", "img[data-src]", "[class*='gallery'] img"], "src"),
                first_attr(soup, ["img[data-src]"], "data-src"),
            ]
        )
        image_url = normalize_link(self.base_url, image_url) if image_url else None

        price = self._extract_detail_price(soup)
        rating = self._extract_detail_rating(soup)
        reviews_count = self._extract_detail_reviews_count(soup)

        description = choose_first_non_empty(
            [
                jsonld.get("description"),
                first_text(
                    soup,
                    [
                        "[class*='description']",
                        "[data-testid*='description']",
                        "meta[name='description']",
                    ],
                ),
                first_attr(soup, ["meta[name='description']"], "content"),
            ]
        )

        characteristics = gather_key_value(
            soup,
            row_selector="tr, li, [class*='spec'], [class*='character'], [class*='attribute']",
            key_selector="th, dt, [class*='name'], [class*='label'], [class*='title']",
            value_selector="td, dd, [class*='value'], [class*='description']",
        )
        if len(characteristics) < 3:
            fallback_characteristics = self._extract_characteristics_fallback(soup)
            if fallback_characteristics:
                for key, value in fallback_characteristics.items():
                    characteristics.setdefault(key, value)

        raw_sections = {
            "headings": [clean_text(h.get_text(" ", strip=True)) for h in soup.select("h2, h3")[:14]],
            "bullet_points": [clean_text(li.get_text(" ", strip=True)) for li in soup.select("ul li")[:20]],
        }

        if not price:
            logger.warning("Kaspi detail: price not found for %s", full_url)
        if not rating:
            logger.info("Kaspi detail: rating not found for %s", full_url)
        logger.info(
            "Kaspi detail parsed title=%r price=%r rating=%r reviews_count=%r url=%s",
            title,
            price,
            rating,
            reviews_count,
            full_url,
        )

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

    def _extract_detail_price(self, soup: BeautifulSoup) -> str | None:
        for node in soup.select(
            ".item__price-once, "
            ".item__price, "
            ".item__price-main, "
            "[class*='item__price']"
        ):
            text = clean_text(node.get_text(" ", strip=True))
            lowered = text.lower()
            if not any(token in lowered for token in ("₸", "тг")):
                continue
            if any(token in lowered for token in ("рассроч", "мес", "x", "×")):
                continue

            match = re.search(r"(\d[\d\s\xa0]{2,})", text)
            if not match:
                continue

            value = int(re.sub(r"\D", "", match.group(1)))
            if value < 1_000 or value > 100_000_000:
                continue

            return f"{value:,}".replace(",", " ") + " ₸"

        return None

    def _extract_detail_reviews_count(self, soup: BeautifulSoup) -> str | None:
        text = self._detail_text_before_sections(soup)
        for match in re.finditer(r"(\d[\d\s\xa0]*)\s*отзыв(?:а|ов)?\b", text, flags=re.IGNORECASE):
            value = int(re.sub(r"\D", "", match.group(1)))
            if value <= 100_000:
                return str(value)

        return None

    def _extract_detail_rating(self, soup: BeautifulSoup) -> str | None:
        for node in soup.select("span.rating"):
            if not self._is_before_detail_sections(soup, node):
                continue
            if node.find_parent("table", class_=re.compile(r"sellers-table", re.IGNORECASE)):
                continue

            parsed = self._rating_from_class_names(node.get("class"))
            if parsed:
                return parsed

        return None

    def _detail_text_before_sections(self, soup: BeautifulSoup) -> str:
        markers = ("продавцы", "характеристики", "описание")
        chunks: list[str] = []

        for raw_text in soup.find_all(string=True):
            if raw_text.parent and raw_text.parent.name in {"script", "style", "noscript"}:
                continue
            text = clean_text(str(raw_text))
            if not text:
                continue
            lowered = text.lower()
            marker_positions = [lowered.find(marker) for marker in markers if lowered.find(marker) >= 0]

            if marker_positions:
                cut_at = min(marker_positions)
                if cut_at > 0:
                    chunks.append(text[:cut_at])
                break

            chunks.append(text)

        return clean_text(" ".join(chunks))

    def _is_before_detail_sections(self, soup: BeautifulSoup, node: Tag) -> bool:
        markers = ("продавцы", "характеристики", "описание")

        for descendant in soup.descendants:
            if descendant is node:
                return True
            if isinstance(descendant, NavigableString):
                if descendant.parent and descendant.parent.name in {"script", "style", "noscript"}:
                    continue
                text = clean_text(str(descendant)).lower()
                if any(marker in text for marker in markers):
                    return False

        return False

    @staticmethod
    def _rating_from_class_names(class_names: object) -> str | None:
        for class_name in class_names or []:
            match = re.fullmatch(r"_(\d{2})", str(class_name))
            if not match:
                continue

            raw_value = int(match.group(1))
            if raw_value < 0 or raw_value > 50:
                continue

            value = raw_value / 10
            return str(int(value)) if value.is_integer() else str(value)

        return None

    def _extract_price(self, container: BeautifulSoup | None, blob_text: str) -> str | None:
        candidates: list[str | None] = []

        if container:
            candidates.extend(
                [
                    first_text(container, [".item-card__prices"]),
                    first_text(container, [".item-card__price"]),
                    first_text(container, ["[data-testid*='price']", "[class*='price']", "strong"]),
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
        class_based = self._extract_rating_from_classes(container)
        if class_based:
            return class_based

        candidates: list[str | None] = []
        if container:
            candidates.extend(
                [
                    first_text(container, ["[class*='rating']", "[data-testid*='rating']"]),
                    first_attr(container, ["[aria-label*='рейтинг']", "[title*='рейтинг']"], "aria-label"),
                ]
            )
        candidates.append(blob_text)

        for candidate in candidates:
            parsed = format_rating(candidate)
            if parsed:
                return parsed
        return None

    def _extract_rating_from_classes(self, container: BeautifulSoup | None) -> str | None:
        if not container:
            return None

        selectors = [
            "[class*='rating']",
            "[class*='star']",
            "[class*='score']",
            "[class*='grade']",
        ]

        for selector in selectors:
            for node in container.select(selector):
                parsed = extract_rating_from_class_tokens(node.get("class"))
                if parsed:
                    return parsed

        return None

    def _extract_reviews(self, container: BeautifulSoup | None, blob_text: str) -> str | None:
        candidates: list[str | None] = []
        if container:
            candidates.extend(
                [
                    first_text(container, ["[class*='review']", "[class*='feedback']", "[data-testid*='review']"]),
                    first_attr(container, ["[aria-label*='отзыв']"], "aria-label"),
                ]
            )
        candidates.append(blob_text)

        for candidate in candidates:
            parsed = extract_reviews_count(candidate)
            if parsed:
                return parsed
        return None

    def _extract_image(self, container: Tag, link: Tag | None = None) -> str | None:
        scopes: list[Tag] = []

        if link is not None:
            scopes.append(link)

            local_scope = link
            while isinstance(local_scope.parent, Tag) and local_scope.parent is not container:
                local_scope = local_scope.parent
            if local_scope is not link:
                scopes.append(local_scope)

            if isinstance(link.parent, Tag):
                scopes.append(link.parent)

        scopes.append(container)

        seen_scope_ids: set[int] = set()
        for scope in scopes:
            scope_id = id(scope)
            if scope_id in seen_scope_ids:
                continue
            seen_scope_ids.add(scope_id)

            img = scope.select_one("img")
            image_url = self._image_url_from_img(img)
            if image_url:
                return image_url

        return None

    def _image_url_from_img(self, img: Tag | None) -> str | None:
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

    def _title_from_product_url(self, product_url: str) -> str | None:
        match = re.search(r"/shop/p/([^/?#]+)/?", product_url)
        if not match:
            return None

        slug = match.group(1)
        slug = re.sub(r"-\d+$", "", slug)
        normalized = clean_text(slug.replace("-", " "))
        if not looks_like_product_title(normalized):
            return None
        return normalized
