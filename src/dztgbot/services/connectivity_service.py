"""Logical lazy VPN connectivity service with positive status caching and single-flight guards."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

from dztgbot.domain.ports import ClockPort, VpnManagerPort

LOGGER = logging.getLogger(__name__)


class ConnectivityService(VpnManagerPort):
    """Lazy VPN connectivity service providing caching and single-flight connection checks."""

    def __init__(
        self,
        vpn_manager: Any,
        clock: ClockPort | None = None,
        positive_ttl_seconds: float = 10.0,
    ) -> None:
        self._vpn_manager = vpn_manager
        self._clock = clock
        self._positive_ttl_seconds = positive_ttl_seconds
        self._lock = asyncio.Lock()
        self._last_positive_utc: datetime | None = None

    def invalidate_cache(self) -> None:
        """Invalidate the positive status cache on network or connection errors."""
        self._last_positive_utc = None

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    def _is_cache_valid(self) -> bool:
        if self._last_positive_utc is None:
            return False
        elapsed = (self._now() - self._last_positive_utc).total_seconds()
        return 0 <= elapsed < self._positive_ttl_seconds

    async def is_connected(self) -> bool:
        """Return whether VPN is logically connected (cached or checked)."""
        if self._is_cache_valid():
            return True

        async with self._lock:
            if self._is_cache_valid():
                return True

            status = await self._vpn_manager.status()
            state_val = getattr(status, "state", None)
            is_up = getattr(status, "is_up", False) or state_val == "up"
            is_disabled = state_val == "disabled"

            if is_up or is_disabled:
                self._last_positive_utc = self._now()
                return True

            self.invalidate_cache()
            return False

    async def ensure_connected(self) -> bool:
        """Lazily ensure VPN is connected single-flight before Jira operations."""
        if self._is_cache_valid():
            return True

        async with self._lock:
            if self._is_cache_valid():
                return True

            status = await self._vpn_manager.status()
            state_val = getattr(status, "state", None)
            is_up = getattr(status, "is_up", False) or state_val == "up"
            is_disabled = state_val == "disabled"

            if is_up or is_disabled:
                self._last_positive_utc = self._now()
                return True

            # Attempt startup if VPN is down and enabled
            start_status = await self._vpn_manager.start()
            start_state = getattr(start_status, "state", None)
            start_up = getattr(start_status, "is_up", False) or start_state == "up"
            start_disabled = start_state == "disabled"

            if start_up or start_disabled:
                self._last_positive_utc = self._now()
                return True

            self.invalidate_cache()
            return False
