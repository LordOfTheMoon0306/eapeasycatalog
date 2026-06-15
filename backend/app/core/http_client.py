from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import tls_requests
from tls_requests.exceptions import HTTPError, ProxyError, TLSError

from app.core.config import settings
from app.core.proxy_manager import ProxyManager

logger = logging.getLogger(__name__)

DESKTOP_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
]


@dataclass
class FetchResult:
    text: str
    status_code: int
    final_url: str


class RequestClient:
    def __init__(self, proxy_manager: ProxyManager):
        self.proxy_manager = proxy_manager
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._source_last_request_at: dict[str, float] = {}

    @staticmethod
    def _normalize_profile(device_profile: str) -> str:
        profile = (device_profile or "desktop").strip().lower()
        if profile not in {"desktop", "mobile"}:
            return "desktop"
        return profile

    def _pick_user_agent(self, device_profile: str) -> str:
        profile = self._normalize_profile(device_profile)
        if profile == "mobile":
            return random.choice(MOBILE_USER_AGENTS)
        return random.choice(DESKTOP_USER_AGENTS)

    def pick_user_agent(self, device_profile: str) -> str:
        return self._pick_user_agent(device_profile)

    def _get_source_lock(self, source: str | None) -> asyncio.Lock:
        key = source or "global"
        lock = self._source_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._source_locks[key] = lock
        return lock

    def _headers(self, device_profile: str, user_agent: str | None = None) -> dict[str, str]:
        profile = self._normalize_profile(device_profile)
        headers = {
            "User-Agent": user_agent or self._pick_user_agent(profile),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if profile == "mobile":
            headers["Sec-CH-UA-Mobile"] = "?1"
            headers["Viewport-Width"] = "412"
        return headers

    @staticmethod
    def _tls_client_identifier(device_profile: str) -> str:
        profile = (device_profile or "desktop").strip().lower()
        if profile == "mobile":
            return "okhttp4_android_13"
        return "chrome_133"

    @staticmethod
    def _retry_after_header(headers: Any | None) -> str | None:
        if not headers:
            return None

        value = None
        if hasattr(headers, "get"):
            value = headers.get("Retry-After") or headers.get("retry-after")
        if isinstance(value, list):
            if not value:
                return None
            return str(value[0])
        if value is None:
            return None
        return str(value)

    async def _fetch_once(
        self,
        url: str,
        *,
        proxy_url: str | None,
        device_profile: str,
        user_agent: str | None,
    ):
        timeout_seconds = max(1.0, float(settings.request_timeout_seconds))
        return await asyncio.to_thread(
            tls_requests.get,
            url,
            headers=self._headers(device_profile, user_agent=user_agent),
            proxy=proxy_url,
            timeout=timeout_seconds,
            follow_redirects=True,
            client_identifier=self._tls_client_identifier(device_profile),
            verify=True,
        )

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None

        try:
            as_int = int(value)
        except (TypeError, ValueError):
            as_int = None

        if as_int is not None:
            return max(0.0, float(as_int))

        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None

        if dt.tzinfo is None:
            return None
        seconds = (dt - datetime.now(UTC)).total_seconds()
        return max(0.0, seconds)

    @staticmethod
    def _compute_backoff(attempt: int, blocked: bool, retry_after_seconds: float | None = None) -> float:
        base = settings.retry_backoff_seconds * (2 ** (attempt - 1))
        if blocked:
            base *= 1.5
        if retry_after_seconds is not None:
            base = max(base, min(retry_after_seconds, 30.0))
        return base + random.uniform(0.05, 0.25)

    async def _respect_source_interval(self, source: str | None) -> None:
        min_interval = max(0.0, settings.source_min_interval_seconds)
        if min_interval <= 0:
            return

        key = source or "global"
        lock = self._get_source_lock(key)
        async with lock:
            now = asyncio.get_running_loop().time()
            last = self._source_last_request_at.get(key)
            if last is not None:
                elapsed = now - last
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
            self._source_last_request_at[key] = asyncio.get_running_loop().time()

    async def fetch_text(
        self,
        url: str,
        *,
        source: str | None = None,
        device_profile: str = "desktop",
        user_agent: str | None = None,
    ) -> FetchResult:
        last_error: Exception | None = None
        normalized_profile = self._normalize_profile(device_profile)

        for attempt in range(1, settings.max_retries + 1):
            await self._respect_source_interval(source)
            proxy_url = self.proxy_manager.next_proxy()
            try:
                response = await self._fetch_once(
                    url,
                    proxy_url=proxy_url,
                    device_profile=normalized_profile,
                    user_agent=user_agent,
                )
                if response.status_code <= 0:
                    reason = (response.text or "TLS transport error").strip()
                    self.proxy_manager.mark_dead(proxy_url, reason=reason, url=url)
                    last_error = RuntimeError(reason)
                    logger.warning("TLS transport failure for %s via proxy=%s: %s", url, proxy_url, reason)
                    sleep_s = self._compute_backoff(attempt, blocked=False)
                    await asyncio.sleep(sleep_s)
                    continue

                if response.status_code in {429, 403, 498}:
                    reason = f"HTTP {response.status_code}"
                    self.proxy_manager.mark_dead(proxy_url, reason=reason, url=url)
                    last_error = RuntimeError(reason)
                    if settings.enable_block_telemetry:
                        logger.warning(
                            "%s",
                            {
                                "event": "blocked_response",
                                "source": source,
                                "url": url,
                                "status_code": response.status_code,
                                "proxy": proxy_url,
                                "attempt": attempt,
                                "device_profile": normalized_profile,
                            },
                        )
                    retry_after_seconds = self._parse_retry_after(self._retry_after_header(response.headers))
                    sleep_s = self._compute_backoff(attempt, blocked=True, retry_after_seconds=retry_after_seconds)
                    await asyncio.sleep(sleep_s)
                    continue
                elif response.status_code >= 400:
                    reason = f"HTTP {response.status_code}"
                    self.proxy_manager.mark_dead(proxy_url, reason=reason, url=url)
                    last_error = RuntimeError(reason)
                    sleep_s = self._compute_backoff(attempt, blocked=False)
                    await asyncio.sleep(sleep_s)
                    continue

                self.proxy_manager.mark_success(proxy_url)
                return FetchResult(
                    text=response.text,
                    status_code=response.status_code,
                    final_url=str(getattr(response, "url", url)),
                )
            except (ProxyError, TLSError) as exc:
                self.proxy_manager.mark_dead(proxy_url, reason=str(exc), url=url)
                last_error = exc
                logger.warning("Proxy/connect failure for %s via proxy=%s: %s", url, proxy_url, exc)
            except (HTTPError, OSError) as exc:
                last_error = exc
                logger.warning("TLS request failure for %s: %s", url, exc)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("Unexpected HTTP client failure for %s via proxy=%s: %s", url, proxy_url, exc)

            sleep_s = self._compute_backoff(attempt, blocked=False)
            await asyncio.sleep(sleep_s)

        if last_error:
            raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error
        raise RuntimeError(f"Failed to fetch {url}")
