"""Unit tests for ConnectivityService lazy VPN manager wrapper."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from dztgbot.services.connectivity_service import ConnectivityService
from dztgbot.vpn import VpnState, VpnStatus


class FakeClock:
    def __init__(self, start_time: datetime | None = None) -> None:
        self._current = start_time or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


class ConnectivityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.vpn_manager = MagicMock()
        self.vpn_manager.status = AsyncMock(
            return_value=VpnStatus(VpnState.UP, "VPN tunnel is up.")
        )
        self.vpn_manager.start = AsyncMock(
            return_value=VpnStatus(VpnState.UP, "VPN tunnel is up.")
        )
        self.service = ConnectivityService(
            vpn_manager=self.vpn_manager,
            clock=self.clock,
            positive_ttl_seconds=10.0,
        )

    async def test_is_connected_positive_cache(self) -> None:
        # Initial check calls vpn_manager.status()
        self.assertTrue(await self.service.is_connected())
        self.assertEqual(self.vpn_manager.status.call_count, 1)

        # Second check within TTL reuses cache
        self.assertTrue(await self.service.is_connected())
        self.assertEqual(self.vpn_manager.status.call_count, 1)

        # Advance past TTL forces new status check
        self.clock.advance(11.0)
        self.assertTrue(await self.service.is_connected())
        self.assertEqual(self.vpn_manager.status.call_count, 2)

    async def test_ensure_connected_when_down_starts_vpn(self) -> None:
        self.vpn_manager.status.return_value = VpnStatus(VpnState.DOWN, "VPN down.")
        self.vpn_manager.start.return_value = VpnStatus(VpnState.UP, "Started.")

        res = await self.service.ensure_connected()
        self.assertTrue(res)
        self.vpn_manager.status.assert_called_once()
        self.vpn_manager.start.assert_called_once()

    async def test_ensure_connected_when_start_fails(self) -> None:
        self.vpn_manager.status.return_value = VpnStatus(VpnState.DOWN, "VPN down.")
        self.vpn_manager.start.return_value = VpnStatus(VpnState.ERROR, "Start failed.")

        res = await self.service.ensure_connected()
        self.assertFalse(res)
        self.assertFalse(await self.service.is_connected())

    async def test_invalidate_cache_forces_recheck(self) -> None:
        self.assertTrue(await self.service.is_connected())
        self.assertEqual(self.vpn_manager.status.call_count, 1)

        self.service.invalidate_cache()
        self.assertTrue(await self.service.is_connected())
        self.assertEqual(self.vpn_manager.status.call_count, 2)

    async def test_disabled_vpn_treated_as_connected(self) -> None:
        self.vpn_manager.status.return_value = VpnStatus(VpnState.DISABLED, "Disabled.")
        self.assertTrue(await self.service.ensure_connected())

    async def test_single_flight_concurrent_calls(self) -> None:
        # Simulate high latency in status()
        async def slow_status() -> VpnStatus:
            await asyncio.sleep(0.05)
            return VpnStatus(VpnState.UP, "Up.")

        self.vpn_manager.status = AsyncMock(side_effect=slow_status)

        # Launch 5 concurrent ensure_connected calls
        results = await asyncio.gather(*[self.service.ensure_connected() for _ in range(5)])
        self.assertTrue(all(results))
        self.assertEqual(self.vpn_manager.status.call_count, 1)


if __name__ == "__main__":
    unittest.main()
