from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from app.schemas.models import ProxyErrorEvent, ProxyRecord, ProxyStatus


@dataclass
class ProxyEntry:
    raw: str
    url: str | None
    dead_until: datetime | None = None
    failure_streak: int = 0


class ProxyManager:
    def __init__(self) -> None:
        self._entries: list[ProxyEntry] = []
        self._enabled = False
        self._index = 0
        self._lock = Lock()
        self._errors: deque[ProxyErrorEvent] = deque(maxlen=300)

    @staticmethod
    def _normalize_proxy(line: str) -> ProxyEntry | None:
        val = line.strip()
        if not val:
            return None

        if "@" in val:
            credentials, host = val.split("@", 1)
            if ":" not in credentials:
                return None
            login, password = credentials.split(":", 1)
            url = f"http://{login}:{password}@{host}"
            return ProxyEntry(raw=val, url=url)

        # ip:port
        if val.count(":") == 1:
            url = f"http://{val}"
            return ProxyEntry(raw=val, url=url)

        return None

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled

    def get_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def load_from_lines(self, lines: list[str]) -> list[ProxyRecord]:
        normalized: list[ProxyEntry] = []
        for line in lines:
            entry = self._normalize_proxy(line)
            if entry:
                normalized.append(entry)

        with self._lock:
            self._entries = normalized
            self._index = 0

        return [ProxyRecord(raw=e.raw, url=e.url) for e in normalized]

    def next_proxy(self) -> str | None:
        with self._lock:
            if not self._enabled or not self._entries:
                return None

            now = datetime.now(UTC)
            total = len(self._entries)

            for _ in range(total):
                entry = self._entries[self._index % total]
                self._index = (self._index + 1) % total
                if entry.dead_until and now < entry.dead_until:
                    continue
                return entry.url

            return None

    def mark_dead(self, proxy_url: str | None, reason: str, url: str | None = None) -> None:
        with self._lock:
            if proxy_url:
                for entry in self._entries:
                    if entry.url == proxy_url:
                        entry.failure_streak += 1
                        cooldown_minutes = min(60, 5 * (2 ** (entry.failure_streak - 1)))
                        entry.dead_until = datetime.now(UTC) + timedelta(minutes=cooldown_minutes)
                        break

            self._errors.appendleft(
                ProxyErrorEvent(
                    proxy=proxy_url,
                    reason=reason,
                    url=url,
                    occurred_at=datetime.now(UTC),
                )
            )

    def mark_success(self, proxy_url: str | None) -> None:
        if not proxy_url:
            return
        with self._lock:
            for entry in self._entries:
                if entry.url == proxy_url:
                    entry.dead_until = None
                    entry.failure_streak = 0
                    break

    def status(self) -> ProxyStatus:
        with self._lock:
            now = datetime.now(UTC)
            dead = sum(
                1
                for entry in self._entries
                if entry.dead_until is not None and entry.dead_until > now
            )
            total = len(self._entries)
            active = total - dead
            return ProxyStatus(
                enabled=self._enabled,
                total=total,
                active=active,
                dead=dead,
                current_index=self._index,
            )

    def recent_errors(self, limit: int = 50) -> list[ProxyErrorEvent]:
        with self._lock:
            return list(self._errors)[:limit]
