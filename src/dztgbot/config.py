"""Environment-based application configuration."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)

# First-release security defaults (MASTER_PLAN §2). Not weakenable via env.
DEFAULT_AUTH_TTL_SECONDS = 180
DEFAULT_MAX_BATCH_MESSAGES = 20
DEFAULT_MAX_MESSAGE_CHARACTERS = 8_000
DEFAULT_MAX_PROMPT_CHARACTERS = 32_000
DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_ATTACHMENT_COUNT = 10
DEFAULT_MAX_QUEUE_SIZE = 100
DEFAULT_MAX_CONCURRENT_GEMINI = 2
DEFAULT_MAX_CONCURRENT_JIRA = 4

_PROJECT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,19}$")

# Cloud-sync path markers (mirrors infrastructure persistence policy; config must
# not import infrastructure packages).
_SYNCED_PATH_MARKERS = frozenset(
    {
        "dropbox",
        "google drive",
        "googledrive",
        "icloud drive",
        "icloudrive",
    }
)

# One privacy-safe message for the verify-disable escape hatch (no host/path/URL).
JIRA_VERIFY_DISABLED_WARNING = (
    "JIRA_VERIFY_SSL is disabled; TLS certificate verification is off "
    "(explicit escape hatch only). Prefer a root-managed custom CA bundle."
)


def _repository_root() -> Path:
    """Return the Git checkout root (…/src/dztgbot/config.py → parents[2])."""

    return Path(__file__).resolve().parents[2]


def _is_synced_path(path: Path) -> bool:
    for part in path.parts:
        normalized = part.casefold()
        if normalized.startswith("onedrive") or normalized in _SYNCED_PATH_MARKERS:
            return True
    return False


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded only from the process environment or a local .env file."""

    telegram_bot_token: str = field(repr=False)
    gemini_api_key: str = field(repr=False)
    gemini_timeout_seconds: float
    telegram_admin_user_ids: frozenset[int]
    telegram_concurrent_updates: int
    telegram_allowed_user_ids: frozenset[int] | None
    jira_rules_path: Path
    workflow_db_path: Path | None
    vpn_enabled: bool
    vpn_connection_name: str
    vpn_profile_path: Path | None
    vpn_allow_start: bool
    vpn_nmcli_bin: Path
    vpn_sudo_bin: Path
    vpn_command_timeout_seconds: float
    log_level: str
    jira_url: str
    jira_verify_ssl: bool
    jira_ca_bundle_path: Path | None
    jira_default_project_key: str | None
    user_credentials_path: Path
    auth_ttl_seconds: int
    auth_pat_only: bool
    private_chat_only: bool
    max_batch_messages: int
    max_message_characters: int
    max_prompt_characters: int
    max_attachment_bytes: int
    max_attachment_count: int
    max_queue_size: int
    max_concurrent_gemini: int
    max_concurrent_jira: int
    notification_poll_interval_seconds: int = 300

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

        gemini_timeout_seconds = cls._parse_float(
            "GEMINI_TIMEOUT_SECONDS",
            default="30",
            minimum=1.0,
            maximum=120.0,
        )

        raw_admin_ids = os.getenv("TELEGRAM_ADMIN_USER_IDS", "").strip()
        if not raw_admin_ids or raw_admin_ids.startswith("TODO_"):
            raise RuntimeError(
                "TELEGRAM_ADMIN_USER_IDS must contain authorised numeric Telegram user IDs."
            )
        telegram_admin_user_ids = cls._parse_positive_id_set(
            raw_admin_ids,
            name="TELEGRAM_ADMIN_USER_IDS",
            required=True,
        )

        telegram_concurrent_updates = cls._parse_int(
            "TELEGRAM_CONCURRENT_UPDATES",
            default="4",
            minimum=1,
            maximum=32,
        )

        notification_poll_interval_seconds = cls._parse_int(
            "NOTIFICATION_POLL_INTERVAL_SECONDS",
            default="300",
            minimum=60,
            maximum=86400,
        )

        telegram_allowed_user_ids = cls._parse_optional_allowed_users(
            os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        )

        raw_rules_path = os.getenv("JIRA_RULES_PATH", "").strip()
        if not raw_rules_path or raw_rules_path.startswith("TODO_"):
            raise RuntimeError("JIRA_RULES_PATH must point to the runtime rules file.")
        jira_rules_path = Path(raw_rules_path).expanduser()

        workflow_db_path = cls._parse_workflow_db_path(
            os.getenv("WORKFLOW_DB_PATH", "").strip()
        )

        jira_url = cls._parse_jira_url(os.getenv("JIRA_URL", "").strip())

        jira_verify_ssl = cls._environment_bool("JIRA_VERIFY_SSL", default=True)
        jira_ca_bundle_path = cls._parse_optional_absolute_path(
            os.getenv("JIRA_CA_BUNDLE_PATH", "").strip(),
            name="JIRA_CA_BUNDLE_PATH",
        )
        if jira_ca_bundle_path is not None and not jira_verify_ssl:
            raise RuntimeError(
                "JIRA_CA_BUNDLE_PATH requires JIRA_VERIFY_SSL=true; "
                "do not combine a custom CA with verify-disable."
            )
        if not jira_verify_ssl:
            # Escape hatch: one fixed, privacy-safe warning (no URL/host/path).
            LOGGER.warning(JIRA_VERIFY_DISABLED_WARNING)

        jira_default_project_key = cls._parse_optional_project_key(
            os.getenv("JIRA_DEFAULT_PROJECT_KEY", "").strip()
        )

        raw_creds_path = os.getenv("USER_CREDENTIALS_PATH", "").strip()
        if raw_creds_path and not raw_creds_path.startswith("TODO_"):
            user_credentials_path = Path(raw_creds_path).expanduser()
        else:
            # Explicit backward-compatible default next to the rules file.
            user_credentials_path = jira_rules_path.parent / "user_credentials.json"

        vpn_enabled = cls._environment_bool("VPN_ENABLED", default=False)
        vpn_allow_start = cls._environment_bool("VPN_ALLOW_START", default=False)
        if vpn_allow_start and not vpn_enabled:
            raise RuntimeError("VPN_ALLOW_START cannot be true while VPN_ENABLED is false.")
        vpn_connection_name = os.getenv("VPN_CONNECTION_NAME", "").strip()
        if vpn_connection_name.startswith("TODO_"):
            vpn_connection_name = ""
        raw_vpn_profile_path = os.getenv("VPN_PROFILE_PATH", "").strip()
        if raw_vpn_profile_path.startswith("TODO_"):
            raw_vpn_profile_path = ""

        if vpn_enabled:
            if not vpn_connection_name:
                raise RuntimeError(
                    "VPN_CONNECTION_NAME must be configured when VPN_ENABLED=true."
                )
            if len(vpn_connection_name) > 128 or any(
                character in vpn_connection_name for character in ("\0", "\n", "\r")
            ):
                raise RuntimeError("VPN_CONNECTION_NAME is invalid.")
            if not raw_vpn_profile_path:
                raise RuntimeError("VPN_PROFILE_PATH must be configured when VPN_ENABLED=true.")
            vpn_profile_path = Path(raw_vpn_profile_path).expanduser()
            if not vpn_profile_path.is_absolute():
                raise RuntimeError("VPN_PROFILE_PATH must be an absolute path.")
        elif raw_vpn_profile_path:
            vpn_profile_path = Path(raw_vpn_profile_path).expanduser()
            if not vpn_profile_path.is_absolute():
                raise RuntimeError("VPN_PROFILE_PATH must be an absolute path when set.")
        else:
            vpn_profile_path = None

        vpn_nmcli_bin = Path(os.getenv("VPN_NMCLI_BIN", "/usr/bin/nmcli").strip() or "/usr/bin/nmcli")
        vpn_sudo_bin = Path(os.getenv("VPN_SUDO_BIN", "/usr/bin/sudo").strip() or "/usr/bin/sudo")
        if vpn_enabled:
            vpn_executables = {
                "VPN_NMCLI_BIN": vpn_nmcli_bin,
                "VPN_SUDO_BIN": vpn_sudo_bin,
            }
            for variable_name, executable_path in vpn_executables.items():
                if not executable_path.is_absolute():
                    raise RuntimeError(f"{variable_name} must be an absolute path.")

        vpn_command_timeout_seconds = cls._parse_float(
            "VPN_COMMAND_TIMEOUT_SECONDS",
            default="10",
            minimum=1.0,
            maximum=60.0,
        )

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise RuntimeError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")

        auth_ttl_seconds = cls._parse_int(
            "AUTH_TTL_SECONDS",
            default=str(DEFAULT_AUTH_TTL_SECONDS),
            minimum=60,
            maximum=900,
        )

        # First-release invariants: PAT-only and private-chat-only cannot be disabled.
        auth_pat_only = cls._environment_bool("AUTH_PAT_ONLY", default=True)
        if not auth_pat_only:
            raise RuntimeError(
                "AUTH_PAT_ONLY cannot be false; the first release enforces PAT-only authentication."
            )
        private_chat_only = cls._environment_bool("PRIVATE_CHAT_ONLY", default=True)
        if not private_chat_only:
            raise RuntimeError(
                "PRIVATE_CHAT_ONLY cannot be false; the first release is private-chat-only."
            )

        max_batch_messages = cls._parse_int(
            "MAX_BATCH_MESSAGES",
            default=str(DEFAULT_MAX_BATCH_MESSAGES),
            minimum=1,
            maximum=50,
        )
        max_message_characters = cls._parse_int(
            "MAX_MESSAGE_CHARACTERS",
            default=str(DEFAULT_MAX_MESSAGE_CHARACTERS),
            minimum=100,
            maximum=32_000,
        )
        max_prompt_characters = cls._parse_int(
            "MAX_PROMPT_CHARACTERS",
            default=str(DEFAULT_MAX_PROMPT_CHARACTERS),
            minimum=1_000,
            maximum=200_000,
        )
        if max_prompt_characters < max_message_characters:
            raise RuntimeError(
                "MAX_PROMPT_CHARACTERS must be greater than or equal to MAX_MESSAGE_CHARACTERS."
            )
        max_attachment_bytes = cls._parse_int(
            "MAX_ATTACHMENT_BYTES",
            default=str(DEFAULT_MAX_ATTACHMENT_BYTES),
            minimum=1,
            maximum=50 * 1024 * 1024,
        )
        max_attachment_count = cls._parse_int(
            "MAX_ATTACHMENT_COUNT",
            default=str(DEFAULT_MAX_ATTACHMENT_COUNT),
            minimum=1,
            maximum=50,
        )
        max_queue_size = cls._parse_int(
            "MAX_QUEUE_SIZE",
            default=str(DEFAULT_MAX_QUEUE_SIZE),
            minimum=1,
            maximum=10_000,
        )
        max_concurrent_gemini = cls._parse_int(
            "MAX_CONCURRENT_GEMINI",
            default=str(DEFAULT_MAX_CONCURRENT_GEMINI),
            minimum=1,
            maximum=16,
        )
        max_concurrent_jira = cls._parse_int(
            "MAX_CONCURRENT_JIRA",
            default=str(DEFAULT_MAX_CONCURRENT_JIRA),
            minimum=1,
            maximum=16,
        )

        return cls(
            telegram_bot_token=token,
            gemini_api_key=gemini_api_key,
            gemini_timeout_seconds=gemini_timeout_seconds,
            telegram_admin_user_ids=telegram_admin_user_ids,
            telegram_concurrent_updates=telegram_concurrent_updates,
            telegram_allowed_user_ids=telegram_allowed_user_ids,
            jira_rules_path=jira_rules_path,
            workflow_db_path=workflow_db_path,
            vpn_enabled=vpn_enabled,
            vpn_connection_name=vpn_connection_name,
            vpn_profile_path=vpn_profile_path,
            vpn_allow_start=vpn_allow_start,
            vpn_nmcli_bin=vpn_nmcli_bin,
            vpn_sudo_bin=vpn_sudo_bin,
            vpn_command_timeout_seconds=vpn_command_timeout_seconds,
            log_level=log_level,
            jira_url=jira_url,
            jira_verify_ssl=jira_verify_ssl,
            jira_ca_bundle_path=jira_ca_bundle_path,
            jira_default_project_key=jira_default_project_key,
            user_credentials_path=user_credentials_path,
            auth_ttl_seconds=auth_ttl_seconds,
            auth_pat_only=auth_pat_only,
            private_chat_only=private_chat_only,
            max_batch_messages=max_batch_messages,
            max_message_characters=max_message_characters,
            max_prompt_characters=max_prompt_characters,
            max_attachment_bytes=max_attachment_bytes,
            max_attachment_count=max_attachment_count,
            max_queue_size=max_queue_size,
            max_concurrent_gemini=max_concurrent_gemini,
            max_concurrent_jira=max_concurrent_jira,
            notification_poll_interval_seconds=notification_poll_interval_seconds,
        )

    @property
    def jira_tls_verify(self) -> bool | str:
        """Value suitable for httpx ``verify=``: False, True, or a CA bundle path string."""

        if not self.jira_verify_ssl:
            return False
        if self.jira_ca_bundle_path is not None:
            return str(self.jira_ca_bundle_path)
        return True

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

    @classmethod
    def _parse_int(
        cls,
        name: str,
        *,
        default: str,
        minimum: int,
        maximum: int,
    ) -> int:
        raw_value = os.getenv(name, default).strip()
        try:
            value = int(raw_value)
        except ValueError as error:
            raise RuntimeError(f"{name} must be an integer.") from error
        if not minimum <= value <= maximum:
            raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
        return value

    @classmethod
    def _parse_float(
        cls,
        name: str,
        *,
        default: str,
        minimum: float,
        maximum: float,
    ) -> float:
        raw_value = os.getenv(name, default).strip()
        try:
            value = float(raw_value)
        except ValueError as error:
            raise RuntimeError(f"{name} must be a number.") from error
        if not minimum <= value <= maximum:
            raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
        return value

    @staticmethod
    def _parse_positive_id_set(
        raw_value: str,
        *,
        name: str,
        required: bool,
    ) -> frozenset[int]:
        try:
            values = frozenset(
                int(part.strip()) for part in raw_value.split(",") if part.strip()
            )
        except ValueError as error:
            raise RuntimeError(
                f"{name} must be a comma-separated list of integers."
            ) from error
        if required and not values:
            raise RuntimeError(f"{name} must contain positive integers.")
        if any(user_id <= 0 for user_id in values):
            raise RuntimeError(f"{name} must contain positive integers.")
        return values

    @classmethod
    def _parse_optional_allowed_users(cls, raw_value: str) -> frozenset[int] | None:
        if not raw_value or raw_value.startswith("TODO_"):
            return None
        return cls._parse_positive_id_set(
            raw_value,
            name="TELEGRAM_ALLOWED_USER_IDS",
            required=False,
        ) or None

    @staticmethod
    def _parse_jira_url(raw_value: str) -> str:
        if not raw_value or raw_value.startswith("TODO_"):
            raise RuntimeError(
                "JIRA_URL must be configured with the Jira server base URL "
                "(e.g. https://jira.example.com)."
            )

        parsed = urlparse(raw_value)
        if parsed.scheme != "https":
            raise RuntimeError("JIRA_URL must use the https scheme.")
        if not parsed.hostname:
            raise RuntimeError("JIRA_URL must include a host name.")
        if parsed.username is not None or parsed.password is not None:
            raise RuntimeError("JIRA_URL must not contain credentials.")
        if parsed.fragment:
            raise RuntimeError("JIRA_URL must not contain a fragment.")
        if parsed.query:
            raise RuntimeError("JIRA_URL must not contain a query string.")
        # Disallow userinfo-style leftovers and non-http(s) netloc oddities.
        if "@" in (parsed.netloc or ""):
            raise RuntimeError("JIRA_URL must not contain credentials.")

        host = parsed.hostname
        port = parsed.port
        netloc = host if port is None else f"{host}:{port}"
        path = (parsed.path or "").rstrip("/")
        return f"https://{netloc}{path}"

    @staticmethod
    def _parse_optional_project_key(raw_value: str) -> str | None:
        if not raw_value or raw_value.startswith("TODO_"):
            return None
        normalized = raw_value.upper()
        if not _PROJECT_KEY_PATTERN.fullmatch(normalized):
            raise RuntimeError(
                "JIRA_DEFAULT_PROJECT_KEY must be 2–20 characters, "
                "starting with a letter, containing only A–Z and 0–9."
            )
        return normalized

    @staticmethod
    def _parse_optional_absolute_path(raw_value: str, *, name: str) -> Path | None:
        if not raw_value or raw_value.startswith("TODO_"):
            return None
        path = Path(raw_value).expanduser()
        if not path.is_absolute():
            raise RuntimeError(f"{name} must be an absolute path.")
        return path

    @classmethod
    def _parse_workflow_db_path(cls, raw_value: str) -> Path | None:
        """Optional during cutover; when set must be absolute, local, outside checkout."""

        if not raw_value or raw_value.startswith("TODO_"):
            return None

        path = Path(raw_value).expanduser()
        if not path.is_absolute():
            raise RuntimeError("WORKFLOW_DB_PATH must be an absolute path.")
        # Prefer the checkout check first so a repo that itself lives under a
        # sync folder still fails with the checkout-specific message.
        repo_root = _repository_root()
        if _is_under(path, repo_root):
            raise RuntimeError(
                "WORKFLOW_DB_PATH must be outside the Git checkout."
            )
        if _is_synced_path(path):
            raise RuntimeError(
                "WORKFLOW_DB_PATH must not reside on a cloud-synced path "
                "(for example OneDrive, Dropbox, or iCloud)."
            )
        return path
