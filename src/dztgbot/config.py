"""Environment-based application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded only from the process environment or a local .env file."""

    telegram_bot_token: str = field(repr=False)
    gemini_api_key: str = field(repr=False)
    gemini_model: str
    gemini_timeout_seconds: float
    telegram_admin_user_ids: frozenset[int]
    telegram_concurrent_updates: int
    jira_rules_path: Path
    vpn_enabled: bool
    vpn_connection_name: str
    vpn_profile_path: Path
    vpn_allow_start: bool
    vpn_nmcli_bin: Path
    vpn_sudo_bin: Path
    vpn_command_timeout_seconds: float
    log_level: str

    @classmethod
    def from_environment(cls) -> "Settings":
        # TODO: On the target Linux server, prefer a protected systemd EnvironmentFile.
        # Local .env loading is provided only for development and never overrides real env vars.
        load_dotenv(override=False)

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token.startswith("TODO_"):
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN must be supplied through the environment or a local .env file."
            )

        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not gemini_api_key or gemini_api_key.startswith("TODO_"):
            raise RuntimeError(
                "GEMINI_API_KEY must be supplied through the environment or a local .env file."
            )

        gemini_model = os.getenv("GEMINI_MODEL", "").strip()
        if not gemini_model or gemini_model.startswith("TODO_"):
            raise RuntimeError("GEMINI_MODEL must name a supported model in the environment.")

        raw_timeout = os.getenv("GEMINI_TIMEOUT_SECONDS", "30").strip()
        try:
            gemini_timeout_seconds = float(raw_timeout)
        except ValueError as error:
            raise RuntimeError("GEMINI_TIMEOUT_SECONDS must be a number.") from error
        if not 1 <= gemini_timeout_seconds <= 120:
            raise RuntimeError("GEMINI_TIMEOUT_SECONDS must be between 1 and 120.")

        raw_admin_ids = os.getenv("TELEGRAM_ADMIN_USER_IDS", "").strip()
        if not raw_admin_ids or raw_admin_ids.startswith("TODO_"):
            raise RuntimeError(
                "TELEGRAM_ADMIN_USER_IDS must contain authorised numeric Telegram user IDs."
            )
        try:
            telegram_admin_user_ids = frozenset(
                int(value.strip()) for value in raw_admin_ids.split(",") if value.strip()
            )
        except ValueError as error:
            raise RuntimeError(
                "TELEGRAM_ADMIN_USER_IDS must be a comma-separated list of integers."
            ) from error
        if not telegram_admin_user_ids or any(user_id <= 0 for user_id in telegram_admin_user_ids):
            raise RuntimeError("TELEGRAM_ADMIN_USER_IDS must contain positive integers.")

        raw_concurrent_updates = os.getenv("TELEGRAM_CONCURRENT_UPDATES", "4").strip()
        try:
            telegram_concurrent_updates = int(raw_concurrent_updates)
        except ValueError as error:
            raise RuntimeError("TELEGRAM_CONCURRENT_UPDATES must be an integer.") from error
        if not 1 <= telegram_concurrent_updates <= 32:
            raise RuntimeError("TELEGRAM_CONCURRENT_UPDATES must be between 1 and 32.")

        raw_rules_path = os.getenv("JIRA_RULES_PATH", "").strip()
        if not raw_rules_path or raw_rules_path.startswith("TODO_"):
            raise RuntimeError("JIRA_RULES_PATH must point to the runtime rules file.")
        jira_rules_path = Path(raw_rules_path).expanduser()

        vpn_enabled = cls._environment_bool("VPN_ENABLED", default=False)
        vpn_allow_start = cls._environment_bool("VPN_ALLOW_START", default=False)
        if vpn_allow_start and not vpn_enabled:
            raise RuntimeError("VPN_ALLOW_START cannot be true while VPN_ENABLED is false.")
        vpn_connection_name = os.getenv("VPN_CONNECTION_NAME", "").strip()
        raw_vpn_profile_path = os.getenv("VPN_PROFILE_PATH", "").strip()
        if vpn_enabled:
            if not vpn_connection_name or vpn_connection_name.startswith("TODO_"):
                raise RuntimeError(
                    "VPN_CONNECTION_NAME must be configured when VPN_ENABLED=true."
                )
            if len(vpn_connection_name) > 128 or any(
                character in vpn_connection_name for character in ("\0", "\n", "\r")
            ):
                raise RuntimeError("VPN_CONNECTION_NAME is invalid.")
            if not raw_vpn_profile_path or raw_vpn_profile_path.startswith("TODO_"):
                raise RuntimeError("VPN_PROFILE_PATH must be configured when VPN_ENABLED=true.")
        vpn_profile_path = Path(raw_vpn_profile_path).expanduser()

        vpn_nmcli_bin = Path(os.getenv("VPN_NMCLI_BIN", "/usr/bin/nmcli").strip())
        vpn_sudo_bin = Path(os.getenv("VPN_SUDO_BIN", "/usr/bin/sudo").strip())
        if vpn_enabled:
            if not vpn_profile_path.is_absolute():
                raise RuntimeError("VPN_PROFILE_PATH must be an absolute path.")
            vpn_executables = {
                "VPN_NMCLI_BIN": vpn_nmcli_bin,
                "VPN_SUDO_BIN": vpn_sudo_bin,
            }
            for variable_name, executable_path in vpn_executables.items():
                if not executable_path.is_absolute():
                    raise RuntimeError(f"{variable_name} must be an absolute path.")

        raw_vpn_timeout = os.getenv("VPN_COMMAND_TIMEOUT_SECONDS", "10").strip()
        try:
            vpn_command_timeout_seconds = float(raw_vpn_timeout)
        except ValueError as error:
            raise RuntimeError("VPN_COMMAND_TIMEOUT_SECONDS must be a number.") from error
        if not 1 <= vpn_command_timeout_seconds <= 60:
            raise RuntimeError("VPN_COMMAND_TIMEOUT_SECONDS must be between 1 and 60.")

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise RuntimeError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")

        return cls(
            telegram_bot_token=token,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            gemini_timeout_seconds=gemini_timeout_seconds,
            telegram_admin_user_ids=telegram_admin_user_ids,
            telegram_concurrent_updates=telegram_concurrent_updates,
            jira_rules_path=jira_rules_path,
            vpn_enabled=vpn_enabled,
            vpn_connection_name=vpn_connection_name,
            vpn_profile_path=vpn_profile_path,
            vpn_allow_start=vpn_allow_start,
            vpn_nmcli_bin=vpn_nmcli_bin,
            vpn_sudo_bin=vpn_sudo_bin,
            vpn_command_timeout_seconds=vpn_command_timeout_seconds,
            log_level=log_level,
        )

    @staticmethod
    def _environment_bool(name: str, *, default: bool) -> bool:
        raw_value = os.getenv(name)
        if raw_value is None:
            return default
        normalized = raw_value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise RuntimeError(f"{name} must be true or false.")
