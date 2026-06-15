from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup


PRICE_NUMBER_RE = re.compile(r"(\d[\d\s\xa0]{2,})")
RATING_CONTEXT_RE = re.compile(
    r"(?:рейтинг|rating|оценк[аи]?)\s*[:\-]?\s*([1-5](?:[\.,]\d{1,2})?)",
    re.IGNORECASE,
)
RATING_WITH_SCALE_RE = re.compile(r"(?<!\d)([1-5](?:[\.,]\d{1,2})?)\s*(?:/\s*5|из\s*5|★)", re.IGNORECASE)
RATING_BOUNDED_RE = re.compile(r"(?<![\d\.,])([1-5](?:[\.,]\d{1,2})?)(?![\d\.,])")
REVIEWS_RE = re.compile(
    r"(?<![\.,\d])(\d+[\d\s\xa0]*)\s*(?:отзыв(?:а|ов)?|оцен(?:ка|ки|ок)?|review(?:s)?)",
    re.IGNORECASE,
)
REVIEWS_PREFIX_RE = re.compile(
    r"(?:отзыв(?:ы|ов|а)?|review(?:s)?|оцен(?:ка|ки|ок)?)\s*[\(\[\s:]*\s*(?<![\.,\d])(\d+[\d\s\xa0]*)",
    re.IGNORECASE,
)
SELLER_CONTEXT_RE = re.compile(
    r"(?:продавец|магазин|seller|shop)\s*[:\-]?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\s\.\-\"']{1,80})",
    re.IGNORECASE,
)
AD_KEYWORDS = (
    "sale",
    "скидк",
    "реклам",
    "баннер",
    "ad",
    "promo",
    "подборк",
)
EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002700-\U000027BF"  # dingbats
    "\U00002600-\U000026FF"  # misc symbols
    "]+",
    flags=re.UNICODE,
)
INVISIBLE_CHARS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
TRAILING_GARBAGE_RE = re.compile(r"(?:\s*(?:[-–—•·▪▫◦●|/\\]+)\s*)+$")
GENERIC_TITLE_WORDS = {
    "цена",
    "купить",
    "подробнее",
    "в корзину",
    "перейти",
    "открыть",
    "sale",
    "promo",
}
ANTIBOT_MARKERS = (
    "подозрительная активность",
    "почти готово",
    "доступ ограничен",
    "нам нужно убедиться, что вы не робот",
    "please, enable javascript to continue",
    "ой... кажется, такой страницы не существует",
    "captcha",
    "antibot",
    "access denied",
    "forbidden",
    "robot",
    "сопоставьте пазл",
    "двигая ползунок",
    "переместите ползунок",
    "нажмите и перетащите",
    "slide to verify",
    "drag to verify",
    "move the slider",
)

TOTAL_RESULTS_PATTERNS = (
    re.compile(
        r"все\s*категори(?:и|я)\s*\(\s*(\d[\d\s\xa0]{0,12})\s*\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:найден[оы]?|нашл[оаи]?сь|всего|результат(?:ов|а)?)\s*[:\-]?\s*(\d[\d\s\xa0]{0,12})\s*(?:товар(?:ов|а)?|результат(?:ов|а)?|предложени(?:й|я))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d[\d\s\xa0]{0,12})\s*(?:товар(?:ов|а)?|результат(?:ов|а)?|предложени(?:й|я))",
        re.IGNORECASE,
    ),
    # Generic JSON keys: totalCount, total, found, totalResults, totalItems
    re.compile(r'"(?:total|totalCount|totalResults|totalItems|found|itemsTotal|productsCount)"\s*:\s*(\d{1,12})', re.IGNORECASE),
    # Ozon-specific: pagination total / catalogTotal
    re.compile(r'"(?:catalogTotal|totalCount|paginationTotal|total)"\s*:\s*(\d{1,12})', re.IGNORECASE),
    # Number followed by «товаров» without space (mobile rendering)
    re.compile(r"(\d{2,12})товар", re.IGNORECASE),
)


@dataclass
class BlockEvent:
    source: str
    stage: str
    url: str
    marker: str
    attempt: int | None = None
    proxy: str | None = None


def log_block_event(
    logger: logging.Logger,
    *,
    source: str,
    stage: str,
    url: str,
    marker: str,
    attempt: int | None = None,
    proxy: str | None = None,
) -> BlockEvent:
    event = BlockEvent(
        source=source,
        stage=stage,
        url=url,
        marker=marker,
        attempt=attempt,
        proxy=proxy,
    )
    payload = {
        "event": "block_detected",
        "source": event.source,
        "stage": event.stage,
        "url": event.url,
        "marker": event.marker,
        "attempt": event.attempt,
        "proxy": event.proxy,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    logger.warning("%s", payload)
    return event


def clean_text(value: str | None) -> str:
    """Normalize text from BeautifulSoup and Playwright extraction.

    The cleaner intentionally performs a strict pass because marketplace content often
    contains HTML entities, emoji icons, and trailing decorative symbols.
    """
    if value is None:
        return ""

    text = html.unescape(str(value))
    if not text:
        return ""

    text = text.replace("\xa0", " ").replace("\u202f", " ")
    text = INVISIBLE_CHARS_RE.sub("", text)
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = TRAILING_GARBAGE_RE.sub("", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)

    return text


def normalize_link(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(base_url, href)


def format_price(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None

    lowered = text.lower()
    has_currency = any(token in lowered for token in ("₸", "₽", "тг", "тенге", "руб"))
    match = PRICE_NUMBER_RE.search(text)
    if not match:
        return None

    digits = re.sub(r"\D", "", match.group(1))
    if len(digits) < 3 and not has_currency:
        return None

    price_value = f"{int(digits):,}".replace(",", " ")
    currency = ""
    if "₸" in text or "тг" in lowered or "тенге" in lowered:
        currency = "₸"
    elif "₽" in text or "руб" in lowered:
        currency = "₽"

    return f"{price_value} {currency}".strip()


def format_rating(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None

    match = RATING_CONTEXT_RE.search(text)
    if not match:
        match = RATING_WITH_SCALE_RE.search(text)
    if not match and ("★" in text or "рейтинг" in text.lower() or "rating" in text.lower() or "оцен" in text.lower() or "отзыв" in text.lower()):
        match = RATING_BOUNDED_RE.search(text.replace("4K", "").replace("5G", ""))
    if not match:
        return None

    raw = match.group(1).replace(",", ".")
    try:
        score = float(raw)
    except ValueError:
        return None

    if score < 1 or score > 5:
        return None
    return f"{score:.1f}".rstrip("0").rstrip(".")


def extract_reviews_count(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None

    prefixed = REVIEWS_PREFIX_RE.search(text)
    if prefixed:
        return re.sub(r"\D", "", prefixed.group(1))

    match = REVIEWS_RE.search(text)
    if match:
        return re.sub(r"\D", "", match.group(1))

    lowered = text.lower()
    if any(token in lowered for token in ("отзыв", "оцен", "review")):
        tokens = text.split()
        for token in reversed(tokens):
            numeric = re.sub(r"\D", "", token)
            if 1 <= len(numeric) <= 6:
                return numeric

    return None


def extract_seller(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None

    match = SELLER_CONTEXT_RE.search(text)
    if not match:
        return None

    seller = clean_text(match.group(1))
    seller = re.sub(r"[.,:;\-]+$", "", seller).strip()
    if len(seller) < 2:
        return None

    blocked_tokens = {"и", "или", "в", "на", "по", "без", "новый", "sale", "promo"}
    if seller.lower() in blocked_tokens:
        return None

    return seller


def extract_rating_from_class_tokens(class_tokens: Iterable[str] | str | None) -> str | None:
    if not class_tokens:
        return None

    tokens: Iterable[str]
    if isinstance(class_tokens, str):
        tokens = class_tokens.split()
    else:
        tokens = class_tokens

    for token in tokens:
        match = re.search(r"_(\d{2})\b", token)
        if not match:
            continue
        raw_value = int(match.group(1))
        if raw_value < 10 or raw_value > 50:
            continue
        score = raw_value / 10
        return f"{score:.1f}".rstrip("0").rstrip(".")

    return None


def choose_first_non_empty(values: Iterable[str | None]) -> str | None:
    for value in values:
        candidate = clean_text(value)
        if candidate:
            return candidate
    return None


def looks_like_banner_or_ad(title: str | None, blob_text: str | None = None) -> bool:
    haystack = f"{clean_text(title)} {clean_text(blob_text)}".lower()
    if not haystack:
        return True
    return any(keyword in haystack for keyword in AD_KEYWORDS)


def looks_like_product_title(title: str | None) -> bool:
    normalized = clean_text(title)
    if len(normalized) < 4:
        return False
    if normalized.lower() in GENERIC_TITLE_WORDS:
        return False
    if not re.search(r"[A-Za-zА-Яа-я0-9]", normalized):
        return False
    return True


def detect_antibot_challenge(html: str) -> str | None:
    text = clean_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)).lower()
    if not text:
        return None

    for marker in ANTIBOT_MARKERS:
        if marker in text:
            return marker
    return None


def extract_total_results_count(raw_html: str) -> int | None:
    if not raw_html:
        return None

    text = clean_text(BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True))
    combined = f"{text}\n{raw_html}"
    candidates: list[int] = []

    for pattern in TOTAL_RESULTS_PATTERNS:
        for match in pattern.finditer(combined):
            digits = re.sub(r"\D", "", match.group(1))
            if not digits:
                continue
            value = int(digits)
            if 0 <= value <= 50_000_000:
                candidates.append(value)

    if not candidates:
        return None

    return max(candidates)


def first_attr(soup: BeautifulSoup, selectors: Iterable[str], attr: str) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node and node.get(attr):
            return str(node.get(attr)).strip()
    return None


def first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if text:
                return text
    return None


def gather_key_value(
    soup: BeautifulSoup,
    row_selector: str,
    key_selector: str,
    value_selector: str,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in soup.select(row_selector):
        key_node = row.select_one(key_selector)
        value_node = row.select_one(value_selector)
        if not key_node or not value_node:
            continue
        key = clean_text(key_node.get_text(" ", strip=True))
        value = clean_text(value_node.get_text(" ", strip=True))
        if key and value:
            out[key] = value
    return out


def extract_product_jsonld(soup: BeautifulSoup) -> dict[str, str | None]:
    result: dict[str, str | None] = {
        "title": None,
        "image_url": None,
        "price": None,
        "rating": None,
        "reviews_count": None,
        "description": None,
    }

    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for node in _iter_json_nodes(data):
            node_type = node.get("@type")
            type_str = " ".join(node_type) if isinstance(node_type, list) else str(node_type or "")
            if "product" not in type_str.lower():
                continue

            title = clean_text(str(node.get("name") or "")) or None
            image_raw = node.get("image")
            image_url = None
            if isinstance(image_raw, list) and image_raw:
                image_url = clean_text(str(image_raw[0])) or None
            elif isinstance(image_raw, str):
                image_url = clean_text(image_raw) or None

            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            price = None
            if isinstance(offers, dict):
                price_raw = choose_first_non_empty([str(offers.get("price") or ""), str(offers.get("lowPrice") or "")])
                price_currency = clean_text(str(offers.get("priceCurrency") or ""))
                if price_raw:
                    parsed = format_price(f"{price_raw} {price_currency}")
                    price = parsed or clean_text(price_raw)

            agg = node.get("aggregateRating")
            rating = None
            reviews_count = None
            if isinstance(agg, dict):
                rating = format_rating(str(agg.get("ratingValue") or ""))
                reviews_count = extract_reviews_count(str(agg.get("reviewCount") or agg.get("ratingCount") or ""))

            description = clean_text(str(node.get("description") or "")) or None

            result["title"] = result["title"] or title
            result["image_url"] = result["image_url"] or image_url
            result["price"] = result["price"] or price
            result["rating"] = result["rating"] or rating
            result["reviews_count"] = result["reviews_count"] or reviews_count
            result["description"] = result["description"] or description

            if result["title"] and result["price"]:
                return result

    return result


def _iter_json_nodes(payload: object) -> Iterable[dict[str, object]]:
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_json_nodes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_json_nodes(item)
