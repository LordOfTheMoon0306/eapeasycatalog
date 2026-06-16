from __future__ import annotations

import asyncio
import inspect
import logging

import httpx
import nodriver as uc
from nodriver import Config

from app.core.config import settings

logger = logging.getLogger(__name__)
_RENDER_BROWSER_SEMAPHORE = asyncio.Semaphore(1)


def _supports_parameter(obj: object, name: str) -> bool:
    try:
        parameters = inspect.signature(obj).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


async def _solve_capmonster(page, url: str, proxy_url: str | None = None) -> bool:  # noqa: ANN001
    """Try to solve any captcha on the page using CapMonster Cloud.

    Returns True if a token was successfully injected, False otherwise.
    """
    api_key = settings.capmonster_api_key.strip()
    if not api_key:
        logger.debug(
            "CapMonster API key not configured – skipping captcha solve")
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
            logger.info(
                "CapMonster: no captcha widget detected on page %s", url)
            return False

        logger.info("CapMonster: submitting %s task for %s", task["type"], url)

        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                "https://api.capmonster.cloud/createTask",
                json={"clientKey": api_key, "task": task},
            )
            data = res.json()
            if data.get("errorId", 0) != 0:
                logger.warning("CapMonster createTask error: %s",
                               data.get("errorDescription"))
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
                        logger.warning(
                            "CapMonster: ready but no token in solution: %s", solution)
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
                            logger.warning(
                                "CapMonster: no datadome cookie in solution: %s", solution)
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
                    logger.warning(
                        "CapMonster unexpected poll status: %s", poll)
                    return False

            logger.warning(
                "CapMonster: timed out waiting for solution for %s", url)
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
    logger.warning("Waiting for render browser slot url=%s", url)
    async with _RENDER_BROWSER_SEMAPHORE:
        logger.warning("Acquired render browser slot url=%s", url)
        return await _render_page_with_browser(
            url=url,
            timeout_ms=timeout_ms,
            wait_selectors=wait_selectors,
            proxy_url=proxy_url,
            scroll=scroll,
            device_profile=device_profile,
            user_agent=user_agent,
        )


async def _render_page_with_browser(
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

    config_kwargs = {
        "headless": True,
        "browser_args": browser_args,
    }
    if _supports_parameter(Config, "no_sandbox"):
        config_kwargs["no_sandbox"] = True
    config = Config(**config_kwargs)

    logger.warning(
        "Starting nodriver browser headless=%s no_sandbox=%s url=%s",
        True,
        True,
        url,
    )

    browser = None
    try:
        try:
            browser = await uc.start(
                config=config,
                user_data_dir=False,
                headless=True,
                no_sandbox=True,
                proxy=proxy_url,
            )
        except TypeError as exc:
            if "no_sandbox" not in str(exc):
                raise
            logger.warning(
                "nodriver uc.start no_sandbox parameter unsupported; retrying with Config/browser args url=%s",
                url,
            )
            browser = await uc.start(
                config=config,
                user_data_dir=False,
                headless=True,
                proxy=proxy_url,
            )

        page = await browser.get(url)
        logger.warning("Nodriver opened url=%s", url)

        capmonster_attempted = False
        captcha_detected = False
        await page.sleep(0.3)

        content = await page.get_content()

        try:
            try:
                final_url = await page.evaluate("window.location.href")
            except Exception:  # noqa: BLE001
                final_url = url

            try:
                body_text = await page.evaluate("document.body.innerText || ''")
            except Exception:  # noqa: BLE001
                body_text = ""

            lowered = ((body_text or "") + " " + (content or "")).lower()
            antibot_detected = any(
                phrase in lowered
                for phrase in [
                    "captcha",
                    "robot",
                    "робот",
                    "access denied",
                    "verify",
                    "checking your browser",
                    "доступ ограничен",
                    "подтвердите",
                    "cloudflare",
                    "защит",
                ]
            )

            logger.warning(
                "Render page diagnostics url=%s final_url=%s html_len=%s body_len=%s "
                "antibot_detected=%s body_sample=%r html_sample=%r",
                url,
                final_url,
                len(content or ""),
                len(body_text or ""),
                antibot_detected,
                (body_text or "")[:1000],
                (content or "")[:1000],
            )
        except Exception as diagnostics_exc:  # noqa: BLE001
            logger.warning(
                "Render page diagnostics failed url=%s err=%s",
                url,
                diagnostics_exc,
            )

        return content
    except Exception as exc:
        logger.exception("Nodriver failed to start or connect browser url=%s", url)
        logger.warning("nodriver render failed: url=%s err=%s", url, exc)
        raise
    finally:
        if browser is not None:
            try:
                stop_result = browser.stop()
                if inspect.isawaitable(stop_result):
                    await stop_result
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop nodriver browser url=%s", url)
