from __future__ import annotations

import asyncio
import logging

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_RENDER_BROWSER_SEMAPHORE = asyncio.Semaphore(1)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ANTIBOT_PHRASES = [
    "captcha",
    "challenge",
    "robot",
    "робот",
    "access denied",
    "verify",
    "checking your browser",
    "доступ ограничен",
    "подтвердите",
    "cloudflare",
    "защит",
    "сопоставьте пазл",
    "двигая ползунок",
    "проверяем браузер",
]


async def render_page(
    url: str,
    timeout_ms: int = 20000,
    wait_selectors: list[str] | None = None,
    proxy_url: str | None = None,
    scroll: bool = True,
    device_profile: str = "desktop",
    user_agent: str | None = None,
) -> str:
    logger.warning("Waiting for Playwright browser slot url=%s", url)
    async with _RENDER_BROWSER_SEMAPHORE:
        logger.warning("Acquired Playwright browser slot url=%s", url)
        return await _render_page_with_playwright(
            url=url,
            timeout_ms=timeout_ms,
            wait_selectors=wait_selectors,
            proxy_url=proxy_url,
            scroll=scroll,
            device_profile=device_profile,
            user_agent=user_agent,
        )


async def _render_page_with_playwright(
    url: str,
    timeout_ms: int = 20000,
    wait_selectors: list[str] | None = None,
    proxy_url: str | None = None,
    scroll: bool = True,
    device_profile: str = "desktop",
    user_agent: str | None = None,
) -> str:
    browser = None
    context = None
    launch_kwargs = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1440,2200",
        ],
    }
    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}

    logger.warning("Starting Playwright browser headless=%s url=%s", True, url)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 2200},
                user_agent=user_agent or DESKTOP_USER_AGENT,
                locale="ru-RU",
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            if wait_selectors:
                await _wait_for_any_selector(page, wait_selectors, timeout_ms)
            else:
                await page.wait_for_timeout(3000)

            if scroll:
                for _ in range(5):
                    await page.evaluate("window.scrollBy(0, 1600);")
                    await page.wait_for_timeout(300)

            html = await page.content()
            final_url = page.url
            body_text = await _safe_body_text(page)
            antibot_detected = _is_antibot_page(body_text, html)

            logger.warning(
                "Playwright diagnostics url=%s final_url=%s html_len=%s body_len=%s "
                "antibot_detected=%s body_sample=%r html_sample=%r",
                url,
                final_url,
                len(html or ""),
                len(body_text or ""),
                antibot_detected,
                (body_text or "")[:1000],
                (html or "")[:1000],
            )

            if antibot_detected:
                raise RuntimeError("Marketplace blocked automated access")

            return html
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Playwright render failed url=%s err=%s", url, exc)
        raise
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close Playwright context url=%s", url)
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close Playwright browser url=%s", url)


async def _wait_for_any_selector(page, selectors: list[str], timeout_ms: int) -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + max(timeout_ms, 1000) / 1000
    while asyncio.get_running_loop().time() < deadline:
        for selector in selectors:
            try:
                if await page.query_selector(selector):
                    return
            except Exception:  # noqa: BLE001
                continue
        await page.wait_for_timeout(500)


async def _safe_body_text(page) -> str:  # noqa: ANN001
    try:
        return await page.locator("body").inner_text(timeout=1000)
    except Exception:  # noqa: BLE001
        return ""


def _is_antibot_page(body_text: str | None, html: str | None) -> bool:
    lowered = ((body_text or "") + " " + (html or "")).lower()
    return any(phrase in lowered for phrase in ANTIBOT_PHRASES)
