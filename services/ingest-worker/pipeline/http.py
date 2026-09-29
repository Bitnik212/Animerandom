"""Shared outgoing HTTP: User-Agent, a Redis token bucket per source, error classes.

Every request to an external source goes through `SourceClient.request`, which
takes a token from `ratelimit:source:{name}` first. The bucket lives in Redis so
the limit holds across all workers, unlike Celery's per-process `rate_limit`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import cache
from typing import Any

import httpx
import redis
from django.conf import settings

# Wait up to this long in-process for a token; beyond it the task reschedules itself.
MAX_INLINE_WAIT = 2.0
STATS_TTL = 25 * 3600


class SourceError(Exception):
    """Base for classified request failures."""


class RateLimited(SourceError):
    """No token available soon, or the source answered 429. Retry after `wait` seconds.

    Never counted against a task's attempt limit."""

    def __init__(self, source: str, wait: float, *, from_server: bool = False) -> None:
        super().__init__(f"{source}: rate limited, retry in {wait:.1f}s")
        self.source = source
        self.wait = wait
        self.from_server = from_server


class TransientError(SourceError):
    """Network error or 5xx: retry with backoff."""


class ItemFailed(SourceError):
    """4xx other than 429: this item fails, the run continues."""

    def __init__(self, source: str, status: int, detail: str) -> None:
        super().__init__(f"{source}: HTTP {status}: {detail[:300]}")
        self.status = status


@cache
def redis_client() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


# KEYS[1] bucket hash; ARGV: capacity, refill per second, now (seconds, float).
# Returns 0 when a token was taken, otherwise seconds until one is available.
_TAKE_TOKEN = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now, 'capacity', capacity, 'rate', rate)
return tostring(wait)
"""


@cache
def _take_token_script(client: redis.Redis) -> Any:
    return client.register_script(_TAKE_TOKEN)


@dataclass
class TokenBucket:
    source: str
    per_minute: int

    @property
    def key(self) -> str:
        return f"ratelimit:source:{self.source}"

    @property
    def capacity(self) -> float:
        # Small bursts only: about two seconds' worth of requests, at least one.
        return max(1.0, self.per_minute / 30)

    def try_take(self) -> float:
        script = _take_token_script(redis_client())
        return float(
            script(keys=[self.key], args=[self.capacity, self.per_minute / 60, time.time()])
        )

    def acquire(self, max_wait: float = MAX_INLINE_WAIT) -> None:
        deadline = time.monotonic() + max_wait
        while True:
            wait = self.try_take()
            if wait <= 0:
                return
            if time.monotonic() + wait > deadline:
                raise RateLimited(self.source, wait)
            time.sleep(wait)

    def level(self) -> float | None:
        raw = redis_client().hget(self.key, "tokens")
        return float(raw) if raw is not None else None


def bucket_for(source: str) -> TokenBucket:
    return TokenBucket(source, settings.SOURCE_RATES_PER_MIN[source])


class SourceStats:
    """Hourly counters behind GET /sources: requests, errors, 429s, last success."""

    def __init__(self, source: str) -> None:
        self.source = source

    def _hour_key(self, when: datetime) -> str:
        return f"ratelimit:source:{self.source}:stats:{when:%Y%m%d%H}"

    def record(self, outcome: str) -> None:
        now = datetime.now(UTC)
        r = redis_client()
        key = self._hour_key(now)
        pipe = r.pipeline()
        pipe.hincrby(key, "requests", 1)
        if outcome != "ok":
            pipe.hincrby(key, outcome, 1)
        pipe.expire(key, STATS_TTL)
        if outcome == "ok":
            pipe.set(f"ratelimit:source:{self.source}:last_success", now.isoformat())
        pipe.execute()

    def last_24h(self) -> dict[str, Any]:
        r = redis_client()
        now = datetime.now(UTC)
        totals = {"requests": 0, "errors": 0, "throttled": 0}
        for hours_ago in range(24):
            ts = datetime.fromtimestamp(now.timestamp() - hours_ago * 3600, UTC)
            for field, value in r.hgetall(self._hour_key(ts)).items():
                if field in totals:
                    totals[field] += int(value)
        return {
            **totals,
            "last_success": r.get(f"ratelimit:source:{self.source}:last_success"),
        }


def _retry_after(response: httpx.Response, default: float = 60.0) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return default
    try:
        return max(1.0, float(value))
    except ValueError:
        try:
            return max(1.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return default


class SourceClient:
    """Sync httpx client for one source. Used inside Celery tasks."""

    def __init__(
        self,
        source: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        bucket: TokenBucket | None = None,
    ) -> None:
        self.source = source
        self.base_url = base_url
        self.bucket = bucket or bucket_for(source)
        self.stats = SourceStats(source)
        self.http = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": settings.HTTP_USER_AGENT, **(headers or {})},
            timeout=timeout,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> SourceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """`url` is relative to the base URL; "" means the base URL exactly (GraphQL
        endpoints), since httpx would otherwise append a trailing slash."""
        url = url or self.base_url
        self.bucket.acquire()
        try:
            response = self.http.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            self.stats.record("errors")
            raise TransientError(f"{self.source}: {exc!r}") from exc
        if response.status_code == 429:
            self.stats.record("throttled")
            raise RateLimited(self.source, _retry_after(response), from_server=True)
        if response.status_code >= 500:
            self.stats.record("errors")
            raise TransientError(f"{self.source}: HTTP {response.status_code}")
        if response.status_code >= 400:
            self.stats.record("errors")
            raise ItemFailed(self.source, response.status_code, response.text)
        self.stats.record("ok")
        return response
