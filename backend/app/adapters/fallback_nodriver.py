from __future__ import annotations

import asyncio
import logging

import httpx
import nodriver as uc
from nodriver import Config

from app.core.config import settings

logger = logging.getLogger(__name__)


async def _solve_capmonster(page, url: str, proxy_url: str | None = None) -> bool:  # noqa: ANN001
    """Try to solve any captcha on the page using CapMonster Cloud.

    Returns True if a token was successfully injected, False otherwise.
    """
    api_key = settings.capmonster_api_key.strip()
    if not api_key:
        logger.debug("CapMonster API key not configured – skipping captcha solve")
        return False

    try:
        # --- Turnstile (Cloudflare) ---
        site_key: str | None = await page.evaluate(
            "(function(){"
            "  var el = document.querySelector('[data-sitekey],[data-site-key]');"
            "  return el ? (el.getAttribute('data-sitekey') || el.getAttribute('data-site-key')) : null;"
            "})()"
        )

        # --- reCAPTCHA v2 / v3 ---
        recaptcha_key: str | None = await page.evaluate(
            "(function(){"
            "  var el = document.querySelector('.g-recaptcha,[data-callback]');"
            "  if(el) return el.getAttribute('data-sitekey') || null;"
            "  var m = document.documentElement.innerHTML.match(/['\"]sitekey['\"]:s*['\"]([^'\"]+)['\"]/i);"
            "  return m ? m[1] : null;"
            "})()"
        )

        # --- hCaptcha ---
        hcaptcha_key: str | None = await page.evaluate(
            "(function(){"
            "  var el = document.querySelector('.h-captcha,[data-hcaptcha-sitekey]');"
            "  return el ? el.getAttribute('data-sitekey') : null;"
            "})()"
        )

        # --- DataDome ---
        datadome_captcha_url: str | None = await page.evaluate(
            "(function(){"
            "  var iframe = document.querySelector('iframe[src*=\"geo.captcha-delivery.com\"]');"
            "  if (iframe) return iframe.src;"
            "  var m = document.documentElement.innerHTML.match(/(https:\\/\\/geo\\.captcha-delivery\\.com\\/captcha\\/[^\"]+)/);"
            "  return m ? m[1] : null;"
            "})()"
        )

        task: dict | None = None
        inject_mode: str = "turnstile"

        if site_key and "turnstile" not in url.lower():
            # Generic sitekey – try Turnstile first
            task = {
                "type": "TurnstileTaskProxyless",
                "websiteURL": url,
                "websiteKey": site_key,
            }
            inject_mode = "turnstile"
        elif recaptcha_key:
            task = {
                "type": "RecaptchaV2TaskProxyless",
                "websiteURL": url,
                "websiteKey": recaptcha_key,
            }
            inject_mode = "recaptcha"
        elif hcaptcha_key:
            task = {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": url,
                "websiteKey": hcaptcha_key,
            }
            inject_mode = "hcaptcha"
        elif datadome_captcha_url:
            user_agent = str(await page.evaluate("navigator.userAgent"))
            datadome_cookie = str(await page.evaluate(
                "(function(){"
                "  var match = document.cookie.match(/(?:^|;\\s*)datadome=([^;]*)/);"
                "  return match ? 'datadome=' + match[1] + ';' : '';"
                "})()"
            ))
            
            task = {
                "type": "CustomTask",
                "class": "DataDome",
                "websiteURL": url,
                "metadata": {
                    "captchaUrl": datadome_captcha_url,
                    "datadomeCookie": datadome_cookie or "datadome=;",
                    "userAgent": user_agent,
                }
            }
            if proxy_url:
                task["metadata"]["proxy"] = proxy_url
            inject_mode = "datadome"
        elif site_key:
            task = {
                "type": "TurnstileTaskProxyless",
                "websiteURL": url,
                "websiteKey": site_key,
            }
            inject_mode = "turnstile"

        if not task:
            logger.info("CapMonster: no captcha widget detected on page %s", url)
            return False

        logger.info("CapMonster: submitting %s task for %s", task["type"], url)

        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                "https://api.capmonster.cloud/createTask",
                json={"clientKey": api_key, "task": task},
            )
            data = res.json()
            if data.get("errorId", 0) != 0:
                logger.warning("CapMonster createTask error: %s", data.get("errorDescription"))
                return False

            task_id = data.get("taskId")
            if not task_id:
                logger.warning("CapMonster: no taskId returned")
                return False

            # Poll for result (max 60 s)
            for attempt in range(12):
                await asyncio.sleep(5)
                ans = await client.post(
                    "https://api.capmonster.cloud/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                )
                poll = ans.json()
                if poll.get("status") == "ready":
                    solution = poll.get("solution", {})
                    token = (
                        solution.get("token")
                        or solution.get("gRecaptchaResponse")
                        or solution.get("answer")
                    )
                    if not token and inject_mode != "datadome":
                        logger.warning("CapMonster: ready but no token in solution: %s", solution)
                        return False

                    if inject_mode == "recaptcha":
                        await page.evaluate(
                            f"(function(){{"
                            f"  var ta = document.getElementById('g-recaptcha-response');"
                            f"  if(ta){{ ta.value = '{token}'; }}"
                            f"  if(typeof ___grecaptcha_cfg !== 'undefined'){{"
                            f"    var keys = Object.keys(___grecaptcha_cfg.clients||{{}});"
                            f"    if(keys.length) grecaptcha.enterprise ? "
                            f"      grecaptcha.enterprise.execute() : grecaptcha.execute();"
                            f"  }}"
                            f"}})()"
                        )
                    elif inject_mode == "hcaptcha":
                        await page.evaluate(
                            f"(function(){{"
                            f"  var ta = document.querySelector('[name=h-captcha-response]');"
                            f"  if(ta) ta.value = '{token}';"
                            f"}})()"
                        )
                    elif inject_mode == "datadome":
                        # data.solution string contains `domains` structure, find the cookie or direct token
                        # token may be missing if the response had `domains` nested structure. CapMonster doesn't use `token` field for DataDome.
                        # Wait, we need to extract from `solution` directly for DataDome:
                        datadome_resp_cookie = ""
                        domains = solution.get("domains", {})
                        for domain, d_info in domains.items():
                            cookies = d_info.get("cookies", {})
                            if "datadome" in cookies:
                                datadome_resp_cookie = cookies["datadome"]
                                break
                        
                        if not datadome_resp_cookie and token:
                             datadome_resp_cookie = token  # fallback 

                        if not datadome_resp_cookie:
                            logger.warning("CapMonster: no datadome cookie in solution: %s", solution)
                            return False

                        await page.evaluate(
                            f"(function(){{"
                            f"  document.cookie = 'datadome={datadome_resp_cookie}; path=/; max-age=3600';"
                            f"}})()"
                        )
                    else:  # turnstile
                        await page.evaluate(
                            f"(function(){{"
                            f"  document.cookie = 'cf_clearance={token}; path=/; max-age=3600';"
                            f"  document.cookie = 'wlbc={token}; path=/; max-age=3600';"
                            f"  var cb = window.__TURNSTILE_CB__ || window.__cfChallengeCallback;"
                            f"  if(typeof cb === 'function') cb('{token}');"
                            f"}})()"
                        )

                    logger.info(
                        "CapMonster: %s token injected (attempt %d) for %s",
                        inject_mode,
                        attempt + 1,
                        url,
                    )
                    await asyncio.sleep(2)
                    return True

                if poll.get("status") != "processing":
                    logger.warning("CapMonster unexpected poll status: %s", poll)
                    return False

            logger.warning("CapMonster: timed out waiting for solution for %s", url)
            return False

    except Exception as exc:  # noqa: BLE001
        logger.error("CapMonster solve error: %s", exc)
        return False


ANTIBOT_PHRASES = [
    "такой страницы не существует",
    "почти готово",
    "доступ ограничен",
    "нам нужно убедиться",
    "captcha",
    "robot",
    "access denied",
    "forbidden",
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
    browser_args = [
        "--headless=new",
        "--disable-blink-features=AutomationControlled",
        "--disable-blink-features",
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--window-size=1440,2200",
        "--window-position=0,0",
    ]
    if user_agent:
        browser_args.append(f"--user-agent={user_agent}")

    config = Config(
        headless=True,
        browser_args=browser_args,
    )

    browser = await uc.start(config=config, user_data_dir=False, headless=True, proxy=proxy_url)
    try:
        page = await browser.get(url)

        capmonster_attempted = False
        captcha_detected = False
        if wait_selectors:
            interval = 0.5
            max_iter = int(timeout_ms / (interval * 1000))
            for _ in range(max_iter):
                # Check for anti-bot / captcha page
                try:
                    page_text: str = await page.evaluate("document.body.innerText || ''")
                    lowered = page_text.lower() if page_text else ""
                    is_blocked = any(phrase in lowered for phrase in ANTIBOT_PHRASES)
                    if is_blocked and not capmonster_attempted:
                        capmonster_attempted = True
                        logger.info("Anti-bot page detected on %s – attempting CapMonster solve", url)
                        solved = await _solve_capmonster(page, url, proxy_url=proxy_url)
                        if solved:
                            # Reload or wait for redirect after token injection
                            await asyncio.sleep(3)
                            await page.reload()
                            await asyncio.sleep(2)
                        else:
                            # Slider / unknown captcha — CapMonster can't handle it
                            captcha_detected = True
                            logger.warning(
                                "Anti-bot challenge not solvable by CapMonster (likely slider captcha) on %s", url
                            )
                            raise RuntimeError(f"Unsolvable anti-bot challenge detected on {url}")
                except RuntimeError:
                    raise
                except Exception:  # noqa: BLE001
                    pass

                found = False
                for selector in wait_selectors:
                    try:
                        node = await page.evaluate(f'document.querySelector("{selector}")')
                        if node:
                            found = True
                            break
                    except Exception:  # noqa: BLE001
                        pass
                if found:
                    break
                await page.sleep(interval)
        else:
            await page.sleep(2)

        if scroll:
            for _ in range(5):
                await page.evaluate("window.scrollBy(0, 1600);")
                await page.sleep(0.3)

        content = await page.get_content()
        return content
    except Exception as exc:
        logger.warning("nodriver render failed: url=%s err=%s", url, exc)
        raise
    finally:
        browser.stop()
