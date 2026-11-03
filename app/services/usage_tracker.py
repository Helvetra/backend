"""
Anonymous usage tracking service.
Tracks character usage by IP address: weekly limits for anonymous users, and a
monthly budget for unverified iOS app requests. Also tracks how long an IP has
been using Helvetra without an account.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis

from app.config import get_settings
from app.core.tiers import Tier, get_tier_config

settings = get_settings()


@dataclass
class UsageResult:
    """Result of usage check."""

    allowed: bool
    characters_used: int
    characters_limit: int
    characters_remaining: int
    reset_at: int  # Unix timestamp


def get_week_key() -> str:
    """Get the current ISO week identifier (YYYY-WW)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%G-W%V")


# How long a visitor may translate without an account, counted from their
# first anonymous translation. After that an account is required.
ANONYMOUS_TRIAL_DAYS = 28
# How long the first-use date is remembered. An IP seen again after this
# starts a new trial, which keeps the stored data bounded.
_FIRST_SEEN_TTL_SECONDS = 365 * 24 * 60 * 60


def get_month_key() -> str:
    """Get the current month identifier (YYYY-MM)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def next_month_start() -> int:
    """Unix timestamp of the first day of next month, 00:00 UTC."""
    now = datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


# Atomic check-and-increment in Redis. Previously this was GET → check →
# INCRBY, which lets two concurrent requests both pass the check at
# current=limit-ε and both increment, overshooting the limit. Lua scripts
# run as a single Redis command so the whole gate is atomic. See
# helvetra/backend#99.
_CHECK_AND_INCR_LUA = """
local current = tonumber(redis.call("GET", KEYS[1]) or "0")
local cost = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if current + cost > limit then
    return {0, current}
end
local new = redis.call("INCRBY", KEYS[1], cost)
redis.call("EXPIRE", KEYS[1], ttl)
return {1, new}
"""


class AnonymousUsageTracker:
    """Redis-based tracker for anonymous user character usage."""

    def __init__(self):
        """Initialize tracker with Redis connection."""
        self.client: redis.Redis | None = None
        self._check_and_incr = None

    async def connect(self):
        """Establish Redis connection + register Lua script."""
        if self.client is None:
            self.client = redis.from_url(settings.redis_url)
            self._check_and_incr = self.client.register_script(_CHECK_AND_INCR_LUA)

    async def close(self):
        """Close Redis connection."""
        if self.client:
            await self.client.aclose()
            self.client = None
            self._check_and_incr = None

    async def _check_and_record(
        self, redis_key: str, characters: int, limit: int, ttl_seconds: int, reset_at: int
    ) -> UsageResult:
        """Atomically check `characters` against `limit` for one key and record it."""
        await self.connect()

        allowed, value = await self._check_and_incr(
            keys=[redis_key],
            args=[characters, limit, ttl_seconds],
        )
        usage = int(value)

        return UsageResult(
            allowed=bool(int(allowed)),
            characters_used=usage,
            characters_limit=limit,
            characters_remaining=max(0, limit - usage),
            reset_at=reset_at,
        )

    async def check_and_record_usage(
        self, ip_address: str, characters: int
    ) -> UsageResult:
        """
        Atomically check whether `characters` fits under the weekly limit
        for this IP and record the usage in the same Redis round-trip.
        """
        now = datetime.now(timezone.utc)
        days_until_monday = (7 - now.weekday()) % 7 or 7
        next_monday = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_until_monday)

        return await self._check_and_record(
            f"usage:anon:{ip_address}:{get_week_key()}",
            characters,
            get_tier_config(Tier.ANONYMOUS).period_limit,
            8 * 24 * 60 * 60,  # week + buffer
            int(next_monday.timestamp()),
        )

    async def check_and_record_ios_usage(
        self, ip_address: str, characters: int
    ) -> UsageResult:
        """
        Atomically check and record usage for an unverified iOS app request
        against the free plan's monthly budget, counted per IP.
        """
        return await self._check_and_record(
            f"usage:ios:{ip_address}:{get_month_key()}",
            characters,
            get_tier_config(Tier.FREE).period_limit,
            32 * 24 * 60 * 60,  # month + buffer
            next_month_start(),
        )

    async def anonymous_trial_ended(self, ip_address: str) -> bool:
        """
        Record the first anonymous use from this IP and report whether its
        free period without an account is over.
        """
        await self.connect()
        key = f"usage:anon-first-seen:{ip_address}"
        now = int(datetime.now(timezone.utc).timestamp())
        await self.client.set(key, now, nx=True, ex=_FIRST_SEEN_TTL_SECONDS)
        first_seen = int(await self.client.get(key) or now)
        return now - first_seen > ANONYMOUS_TRIAL_DAYS * 24 * 60 * 60

    async def get_usage(self, ip_address: str) -> UsageResult:
        """Get current usage without recording anything."""
        await self.connect()

        config = get_tier_config(Tier.ANONYMOUS)
        limit = config.period_limit
        week_key = get_week_key()
        redis_key = f"usage:anon:{ip_address}:{week_key}"

        current = await self.client.get(redis_key)
        current_usage = int(current or 0)

        # Calculate reset time (next Monday 00:00 UTC)
        now = datetime.now(timezone.utc)
        days_until_monday = (7 - now.weekday()) % 7 or 7
        next_monday = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_until_monday)
        reset_at = int(next_monday.timestamp())

        return UsageResult(
            allowed=current_usage < limit,
            characters_used=current_usage,
            characters_limit=limit,
            characters_remaining=max(0, limit - current_usage),
            reset_at=reset_at,
        )


# Global tracker instance
anonymous_usage_tracker = AnonymousUsageTracker()
