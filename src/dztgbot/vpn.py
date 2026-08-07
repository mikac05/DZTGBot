"""Safe async NetworkManager L2TP/IPsec status and startup helper."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class VpnState(StrEnum):
    UP = "up"
    DOWN = "down"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VpnStatus:
    state: VpnState
    message: str

    @property
    def is_up(self) -> bool:
        return self.state is VpnState.UP


class NetworkManagerL2tpManager:
    """Control one L2TP/IPsec connection without exposing secrets or command output."""

    def __init__(
        self,
        *,
        enabled: bool,
        connection_name: str,
        profile_path: Path,
        allow_start: bool,
        nmcli_bin: Path,
        sudo_bin: Path,
        command_timeout_seconds: float,
    ) -> None:
        self._enabled = enabled
        self._connection_name = connection_name
        self._profile_path = profile_path
        self._allow_start = allow_start
        self._nmcli_bin = nmcli_bin
        self._sudo_bin = sudo_bin
        self._command_timeout_seconds = command_timeout_seconds
        self._lock = asyncio.Lock()

    async def status(self) -> VpnStatus:
        if not self._enabled:
            return VpnStatus(VpnState.DISABLED, "L2TP/IPsec VPN support is disabled.")

        result = await self._run_for_state(
            str(self._nmcli_bin),
            "--terse",
            "--fields",
            "GENERAL.STATE",
            "connection",
            "show",
            self._connection_name,
        )
        if result is None:
            return VpnStatus(VpnState.ERROR, "L2TP/IPsec VPN status could not be checked.")

        return_code, output = result
        if return_code != 0:
            return VpnStatus(VpnState.DOWN, "L2TP/IPsec VPN tunnel is down.")

        # With LC_ALL=C, NetworkManager reports values such as
        # GENERAL.STATE:activated or GENERAL.STATE:deactivated.
        state_value = output.rsplit(":", maxsplit=1)[-1].strip().lower()
        if state_value == "activated":
            return VpnStatus(VpnState.UP, "L2TP/IPsec VPN tunnel is up.")
        return VpnStatus(VpnState.DOWN, "L2TP/IPsec VPN tunnel is down.")

    async def start(self) -> VpnStatus:
        async with self._lock:
            current = await self.status()
            if current.is_up or not self._enabled:
                return current
            if not self._allow_start:
                return VpnStatus(
                    VpnState.DOWN,
                    "L2TP/IPsec VPN tunnel is down and remote start is disabled.",
                )
            loaded = await self._run_quiet(
                str(self._sudo_bin),
                "-n",
                str(self._nmcli_bin),
                "connection",
                "load",
                str(self._profile_path),
            )
            if loaded != 0:
                return VpnStatus(VpnState.ERROR, "The private VPN profile could not be loaded.")

            started = await self._run_quiet(
                str(self._sudo_bin),
                "-n",
                str(self._nmcli_bin),
                "connection",
                "up",
                self._connection_name,
            )
            if started != 0:
                return VpnStatus(VpnState.ERROR, "L2TP/IPsec VPN tunnel could not be started.")
            return await self.status()

    async def _run_for_state(self, *command: str) -> tuple[int, str] | None:
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
        except OSError as error:
            LOGGER.error("VPN status command could not start (%s)", type(error).__name__)
            return None

        try:
            async with asyncio.timeout(self._command_timeout_seconds):
                stdout, _ = await process.communicate()
        except TimeoutError:
            await self._terminate(process)
            LOGGER.error("VPN status command timed out")
            return None

        # GENERAL.STATE cannot contain secrets. Limit decoding defensively.
        return process.returncode, stdout[:256].decode("utf-8", errors="replace")

    async def _run_quiet(self, *command: str) -> int | None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            LOGGER.error("VPN helper command could not start (%s)", type(error).__name__)
            return None

        try:
            async with asyncio.timeout(self._command_timeout_seconds):
                return await process.wait()
        except TimeoutError:
            await self._terminate(process)
            LOGGER.error("VPN helper command timed out")
            return None

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
