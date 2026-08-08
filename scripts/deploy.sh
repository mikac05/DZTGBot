#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly SERVICE_NAME="dztgbot.service"
readonly UNIT_DESTINATION="/etc/systemd/system/${SERVICE_NAME}"
readonly STATE_ROOT="/var/lib/dztgbot"
readonly SUDOERS_DESTINATION="/etc/sudoers.d/dztgbot-vpn"
# Minimum free space on the state filesystem before install/migrate (KiB).
readonly MIN_STATE_FREE_KIB=262144
# Bounded wait for systemd active state after start/restart.
readonly SERVICE_ACTIVE_WAIT_SECONDS=20

log() {
    printf '[dztgbot-deploy] %s\n' "$*"
}

die() {
    printf '[dztgbot-deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        die "Run this script as root with sudo."
    fi
}

require_safe_absolute_path() {
    local label="$1"
    local value="$2"
    if [[ -z "$value" || "$value" != /* || "$value" == "/" ]]; then
        die "${label} must be a non-root absolute path."
    fi
    if [[ ! "$value" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
        die "${label} may contain only letters, digits, slash, dot, underscore, and hyphen."
    fi
    case "$value" in
        */../*|*/..|*/./*|*/.) die "${label} must not contain dot path segments." ;;
    esac
}

require_root_managed_parent() {
    local label="$1"
    local path="$2"
    local parent owner mode
    parent="$(dirname -- "$path")"
    owner="$(stat -c '%u' "$parent")"
    mode="$(stat -c '%a' "$parent")"
    [[ "$owner" == "0" ]] || die "The parent directory for ${label} must be owned by root."
    if (( (8#$mode & 8#022) != 0 )); then
        die "The parent directory for ${label} must not be writable by group or other users."
    fi
}

path_looks_synced_or_network() {
    local value
    value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    case "$value" in
        *onedrive*|*dropbox*|*google\ drive*|*googledrive*|*icloud*|*nextcloud*)
            return 0
            ;;
    esac
    return 1
}

normalize_bool() {
    local value="${1,,}"
    case "$value" in
        1|true|yes|on) printf 'true' ;;
        0|false|no|off) printf 'false' ;;
        *) return 1 ;;
    esac
}

env_value() {
    local key="$1"
    local count
    local value

    count="$(awk -F= -v wanted="$key" '
        /^[[:space:]]*#/ { next }
        {
            name=$1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
            if (name == wanted) matches++
        }
        END { print matches + 0 }
    ' "$DZTGBOT_ENV_FILE")"
    if [[ "$count" -gt 1 ]]; then
        die "The environment file contains duplicate ${key} entries."
    fi
    if [[ "$count" -eq 0 ]]; then
        printf ''
        return
    fi

    value="$(awk -F= -v wanted="$key" '
        /^[[:space:]]*#/ { next }
        {
            name=$1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
            if (name == wanted) {
                sub(/^[^=]*=/, "", $0)
                print $0
                exit
            }
        }
    ' "$DZTGBOT_ENV_FILE")"

    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 ]]; then
        if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
            value="${value:1:${#value}-2}"
        fi
    fi
    printf '%s' "$value"
}

require_configured_value() {
    local key="$1"
    local value
    value="$(env_value "$key")"
    if [[ -z "$value" || "$value" == TODO_* ]]; then
        die "${key} must be configured in the protected environment file."
    fi
}

detect_platform() {
    [[ -r /etc/os-release ]] || die "/etc/os-release is unavailable."
    # /etc/os-release is a trusted, root-owned operating-system file.
    # shellcheck disable=SC1091
    source /etc/os-release
    readonly PLATFORM_ID="${ID:-}"
    readonly PLATFORM_VERSION="${VERSION_ID:-}"

    case "${PLATFORM_ID}:${PLATFORM_VERSION}" in
        ubuntu:24.04*) ;;
        *) die "The only supported target is Ubuntu 24.04; detected ${PLATFORM_ID:-unknown} ${PLATFORM_VERSION:-unknown}." ;;
    esac
}

prepare_environment_file() {
    require_safe_absolute_path "DZTGBOT_ENV_FILE" "$DZTGBOT_ENV_FILE"
    if [[ -L "$DZTGBOT_ENV_FILE" ]]; then
        die "The protected environment file must not be a symbolic link."
    fi

    local parent
    parent="$(dirname -- "$DZTGBOT_ENV_FILE")"
    if [[ ! -d "$parent" ]]; then
        install -d -o root -g root -m 0700 "$parent"
    fi
    DZTGBOT_ENV_FILE="$(realpath -m -- "$DZTGBOT_ENV_FILE")"
    if [[ "$DZTGBOT_ENV_FILE" == "$PROJECT_DIR"/* ]]; then
        die "The protected environment file must be outside the project checkout."
    fi
    require_root_managed_parent "DZTGBOT_ENV_FILE" "$DZTGBOT_ENV_FILE"

    if [[ ! -e "$DZTGBOT_ENV_FILE" ]]; then
        install -o root -g root -m 0600 "$PROJECT_DIR/.env.example" "$DZTGBOT_ENV_FILE"
        log "Created a protected placeholder environment file."
        log "Edit it in place with sudoedit, replace required TODO values, then run this script again."
        log "Required keys include WORKFLOW_DB_PATH under ${STATE_ROOT} (for example ${STATE_ROOT}/workflow.sqlite3)."
        log "GEMINI_MODEL is not used by the application and is not required."
        exit 2
    fi

    [[ -f "$DZTGBOT_ENV_FILE" ]] || die "The environment path must be a regular file."
    [[ "$(stat -c '%u' "$DZTGBOT_ENV_FILE")" == "0" ]] || die "The environment file must be owned by root."
    [[ "$(stat -c '%a' "$DZTGBOT_ENV_FILE")" == "600" ]] || die "The environment file permissions must be exactly 0600."
}

validate_optional_true_invariant() {
    local key="$1"
    local raw
    raw="$(env_value "$key")"
    if [[ -z "$raw" ]]; then
        return
    fi
    local normalized
    normalized="$(normalize_bool "$raw")" || die "${key} must be true or false."
    if [[ "$normalized" != "true" ]]; then
        die "${key} cannot be false; the first release hard-enforces this invariant."
    fi
}

validate_environment() {
    local key
    # GEMINI_MODEL is intentionally absent: model selection is application-managed.
    for key in TELEGRAM_BOT_TOKEN GEMINI_API_KEY TELEGRAM_ADMIN_USER_IDS JIRA_RULES_PATH JIRA_URL WORKFLOW_DB_PATH; do
        require_configured_value "$key"
    done

    local admin_ids
    admin_ids="$(env_value TELEGRAM_ADMIN_USER_IDS)"
    [[ "$admin_ids" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]] || die "TELEGRAM_ADMIN_USER_IDS must contain comma-separated positive integers without spaces."

    local concurrent_updates
    concurrent_updates="$(env_value TELEGRAM_CONCURRENT_UPDATES)"
    concurrent_updates="${concurrent_updates:-4}"
    [[ "$concurrent_updates" =~ ^[0-9]+$ ]] || die "TELEGRAM_CONCURRENT_UPDATES must be an integer."
    if (( concurrent_updates < 1 || concurrent_updates > 32 )); then
        die "TELEGRAM_CONCURRENT_UPDATES must be between 1 and 32."
    fi

    # First-release security invariants when present in the environment file.
    validate_optional_true_invariant "AUTH_PAT_ONLY"
    validate_optional_true_invariant "PRIVATE_CHAT_ONLY"

    JIRA_RULES_PATH="$(env_value JIRA_RULES_PATH)"
    require_safe_absolute_path "JIRA_RULES_PATH" "$JIRA_RULES_PATH"
    [[ "$(dirname -- "$JIRA_RULES_PATH")" == "$STATE_ROOT" ]] || \
        die "JIRA_RULES_PATH must be a direct child of ${STATE_ROOT} so systemd can grant narrowly scoped write access."

    WORKFLOW_DB_PATH="$(env_value WORKFLOW_DB_PATH)"
    require_safe_absolute_path "WORKFLOW_DB_PATH" "$WORKFLOW_DB_PATH"
    [[ "$(dirname -- "$WORKFLOW_DB_PATH")" == "$STATE_ROOT" ]] || \
        die "WORKFLOW_DB_PATH must be a direct child of ${STATE_ROOT} (for example ${STATE_ROOT}/workflow.sqlite3)."
    if [[ "$WORKFLOW_DB_PATH" == "$PROJECT_DIR"/* ]]; then
        die "WORKFLOW_DB_PATH must be outside the project checkout."
    fi
    if path_looks_synced_or_network "$WORKFLOW_DB_PATH"; then
        die "WORKFLOW_DB_PATH must not reside on cloud-synced or network-storage path markers."
    fi
    if [[ -L "$WORKFLOW_DB_PATH" ]]; then
        die "WORKFLOW_DB_PATH must not be a symbolic link."
    fi

    USER_CREDENTIALS_PATH="$(env_value USER_CREDENTIALS_PATH)"
    if [[ -n "$USER_CREDENTIALS_PATH" && "$USER_CREDENTIALS_PATH" != TODO_* ]]; then
        require_safe_absolute_path "USER_CREDENTIALS_PATH" "$USER_CREDENTIALS_PATH"
        [[ "$(dirname -- "$USER_CREDENTIALS_PATH")" == "$STATE_ROOT" ]] || \
            die "USER_CREDENTIALS_PATH must be a direct child of ${STATE_ROOT} when configured."
        if [[ "$USER_CREDENTIALS_PATH" == "$PROJECT_DIR"/* ]]; then
            die "USER_CREDENTIALS_PATH must be outside the project checkout."
        fi
    else
        USER_CREDENTIALS_PATH="${STATE_ROOT}/user_credentials.json"
    fi

    local jira_url
    jira_url="$(env_value JIRA_URL)"
    [[ "$jira_url" == https://* ]] || die "JIRA_URL must use the https scheme."
    case "$jira_url" in
        *@*|*"?"*|*"#"*) die "JIRA_URL must not contain credentials, query strings, or fragments." ;;
    esac

    local raw_vpn_enabled raw_vpn_allow_start
    raw_vpn_enabled="$(env_value VPN_ENABLED)"
    raw_vpn_allow_start="$(env_value VPN_ALLOW_START)"
    VPN_ENABLED="$(normalize_bool "${raw_vpn_enabled:-false}")" || die "VPN_ENABLED must be true or false."
    VPN_ALLOW_START="$(normalize_bool "${raw_vpn_allow_start:-false}")" || die "VPN_ALLOW_START must be true or false."
    if [[ "$VPN_ALLOW_START" == "true" && "$VPN_ENABLED" != "true" ]]; then
        die "VPN_ALLOW_START cannot be true while VPN_ENABLED is false."
    fi

    VPN_CONNECTION_NAME="$(env_value VPN_CONNECTION_NAME)"
    VPN_PROFILE_PATH="$(env_value VPN_PROFILE_PATH)"
    VPN_NMCLI_BIN="$(env_value VPN_NMCLI_BIN)"
    VPN_SUDO_BIN="$(env_value VPN_SUDO_BIN)"
    VPN_NMCLI_BIN="${VPN_NMCLI_BIN:-/usr/bin/nmcli}"
    VPN_SUDO_BIN="${VPN_SUDO_BIN:-/usr/bin/sudo}"

    if [[ "$VPN_ENABLED" == "true" ]]; then
        if [[ -z "$VPN_CONNECTION_NAME" || "$VPN_CONNECTION_NAME" == TODO_* ]]; then
            die "VPN_CONNECTION_NAME must be configured when VPN_ENABLED=true."
        fi
        if [[ -z "$VPN_PROFILE_PATH" || "$VPN_PROFILE_PATH" == TODO_* ]]; then
            die "VPN_PROFILE_PATH must be configured when VPN_ENABLED=true."
        fi
        require_safe_absolute_path "VPN_PROFILE_PATH" "$VPN_PROFILE_PATH"
        require_safe_absolute_path "VPN_NMCLI_BIN" "$VPN_NMCLI_BIN"
        require_safe_absolute_path "VPN_SUDO_BIN" "$VPN_SUDO_BIN"
        local resolved_vpn_profile
        resolved_vpn_profile="$(realpath -e -- "$VPN_PROFILE_PATH")"
        if [[ "$resolved_vpn_profile" == "$PROJECT_DIR"/* ]]; then
            die "The private VPN profile must be outside the project checkout."
        fi
        [[ ! -L "$VPN_PROFILE_PATH" && -f "$VPN_PROFILE_PATH" ]] || die "VPN_PROFILE_PATH must be a regular, non-symlink file."
        [[ "$(stat -c '%u' "$VPN_PROFILE_PATH")" == "0" ]] || die "The VPN profile must be owned by root."
        [[ "$(stat -c '%a' "$VPN_PROFILE_PATH")" == "600" ]] || die "The VPN profile permissions must be exactly 0600."
        require_root_managed_parent "VPN_PROFILE_PATH" "$VPN_PROFILE_PATH"
    fi
}

install_base_packages() {
    if [[ "${DZTGBOT_INSTALL_SYSTEM_PACKAGES:-true}" != "true" ]]; then
        log "Skipping operating-system package installation by request."
        return
    fi

    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates sudo
}

install_vpn_packages() {
    [[ "$VPN_ENABLED" == "true" ]] || return
    if [[ "${DZTGBOT_INSTALL_SYSTEM_PACKAGES:-true}" != "true" ]]; then
        log "VPN package installation was skipped; validating the existing client."
    else
        local xl2tpd_was_active="false"
        if systemctl is-active --quiet xl2tpd.service; then
            xl2tpd_was_active="true"
        fi

        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y software-properties-common
        add-apt-repository -y universe
        apt-get update
        apt-get install -y network-manager network-manager-l2tp strongswan ppp xl2tpd sudo

        if [[ "$xl2tpd_was_active" == "false" ]] && systemctl is-active --quiet xl2tpd.service; then
            systemctl disable --now xl2tpd.service
        fi
    fi

    [[ -x "$VPN_NMCLI_BIN" ]] || die "The configured nmcli executable is unavailable."
    [[ -x "$VPN_SUDO_BIN" ]] || die "The configured sudo executable is unavailable."
    if ! systemctl is-active --quiet NetworkManager.service; then
        die "NetworkManager is inactive. Enable it only from an out-of-band console after confirming existing interface ownership, then rerun deployment."
    fi
    if systemctl is-active --quiet xl2tpd.service; then
        die "The system xl2tpd service is already active and may own UDP port 1701. Review it before deploying NetworkManager-l2tp."
    fi
}

ensure_service_account() {
    if id "$DZTGBOT_SERVICE_USER" >/dev/null 2>&1; then
        [[ "$(id -u "$DZTGBOT_SERVICE_USER")" != "0" ]] || die "The service account must not be root."
        local existing_shell
        existing_shell="$(getent passwd "$DZTGBOT_SERVICE_USER" | cut -d: -f7)"
        case "$existing_shell" in
            */nologin|*/false) ;;
            *) die "The existing service account must use a nologin or false shell." ;;
        esac
    else
        local nologin_shell
        nologin_shell="$(command -v nologin || true)"
        [[ -n "$nologin_shell" ]] || die "nologin executable not found."
        useradd --system --user-group --no-create-home --home-dir /nonexistent --shell "$nologin_shell" "$DZTGBOT_SERVICE_USER"
    fi
    SERVICE_GROUP="$(id -gn "$DZTGBOT_SERVICE_USER")"
}

ensure_state_disk_space() {
    local target="$1"
    local free_kib
    if ! free_kib="$(df -Pk -- "$target" 2>/dev/null | awk 'NR==2 {print $4}')"; then
        die "Unable to measure free disk space for ${target}."
    fi
    [[ "$free_kib" =~ ^[0-9]+$ ]] || die "Unable to parse free disk space for ${target}."
    if (( free_kib < MIN_STATE_FREE_KIB )); then
        die "Insufficient free disk space under ${target}: need at least ${MIN_STATE_FREE_KIB} KiB, found ${free_kib} KiB."
    fi
    if ! touch -- "${target}/.dztgbot-write-probe" 2>/dev/null; then
        die "State directory ${target} is not writable (read-only filesystem or permissions failure)."
    fi
    rm -f -- "${target}/.dztgbot-write-probe"
}

build_virtual_environment() {
    local python_candidate="${DZTGBOT_PYTHON_BIN:-}"
    if [[ -z "$python_candidate" ]]; then
        python_candidate="$(command -v python3.12 || command -v python3 || true)"
    fi
    [[ -n "$python_candidate" ]] || die "Python 3.12 is required. Supply an approved absolute binary through DZTGBOT_PYTHON_BIN."
    require_safe_absolute_path "DZTGBOT_PYTHON_BIN" "$python_candidate"
    [[ -x "$python_candidate" ]] || die "The configured Python executable is unavailable."
    "$python_candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' || die "The deployment interpreter must be Python 3.12."

    VENV_DIR="$PROJECT_DIR/.venv"
    [[ ! -L "$VENV_DIR" ]] || die "The virtual-environment path must not be a symbolic link."
    [[ ! -e "$VENV_DIR" || -d "$VENV_DIR" ]] || die "The virtual-environment path must be a directory."
    "$python_candidate" -m venv "$VENV_DIR"
    # Runtime install only. Quality tools (ruff/mypy/coverage/ShellCheck) belong to
    # requirements-dev.txt and the offline CI workflow — not the production venv.
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"
    "$VENV_DIR/bin/python" -m pip check
    PYTHONPATH="$PROJECT_DIR/src:$PROJECT_DIR" "$VENV_DIR/bin/python" -m compileall -q "$PROJECT_DIR/src"
    # Offline unit suite only. Never contacts Telegram, Gemini, Jira, VPN, or systemd.
    PYTHONPATH="$PROJECT_DIR/src:$PROJECT_DIR" "$VENV_DIR/bin/python" -m unittest discover -s "$PROJECT_DIR/tests" -v

    chown -R "root:$SERVICE_GROUP" "$VENV_DIR"
    chmod -R u=rwX,g=rX,o= "$VENV_DIR"
    chmod a+rx "$PROJECT_DIR"
    chmod -R a+rX "$PROJECT_DIR/src"
    if ! sudo -u "$DZTGBOT_SERVICE_USER" test -r "$PROJECT_DIR/src/dztgbot/__main__.py"; then
        die "The service account cannot read the project. Move it outside a private home/root directory or correct directory traversal permissions."
    fi
    sudo -u "$DZTGBOT_SERVICE_USER" test -x "$VENV_DIR/bin/python" || die "The service account cannot execute the virtual-environment Python."
    if sudo -u "$DZTGBOT_SERVICE_USER" test -w "$PROJECT_DIR/src/dztgbot/__main__.py"; then
        die "The service account must not be able to modify application source files."
    fi
}

prepare_rules() {
    [[ ! -L "$STATE_ROOT" ]] || die "The application state directory must not be a symbolic link."
    [[ ! -e "$STATE_ROOT" || -d "$STATE_ROOT" ]] || die "The application state path must be a directory."
    install -d -o "$DZTGBOT_SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$STATE_ROOT"
    ensure_state_disk_space "$STATE_ROOT"

    if [[ ! -e "$JIRA_RULES_PATH" ]]; then
        install -o "$DZTGBOT_SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 "$PROJECT_DIR/config/jira_rules.example.txt" "$JIRA_RULES_PATH"
        log "Seeded placeholder Jira rules; replace them with approved rules through /setrules."
    fi
    [[ -f "$JIRA_RULES_PATH" && ! -L "$JIRA_RULES_PATH" ]] || die "JIRA_RULES_PATH must be a regular, non-symlink file."
    [[ -s "$JIRA_RULES_PATH" ]] || die "The runtime Jira rules file must not be empty."
    chown "$DZTGBOT_SERVICE_USER:$SERVICE_GROUP" "$JIRA_RULES_PATH"
    chmod 0600 "$JIRA_RULES_PATH"
}

stop_service_if_running() {
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        log "Stopping ${SERVICE_NAME} before workflow-database maintenance."
        systemctl stop "$SERVICE_NAME"
    fi
}

backup_existing_workflow_database() {
    local source_db="$1"
    local backup_dir="${STATE_ROOT}/backups"
    local stamp backup_path
    install -d -o "$DZTGBOT_SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$backup_dir"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_path="${backup_dir}/workflow-${stamp}.sqlite3"

    # Online-safe SQLite backup API (service is already stopped for deploy).
    if ! sudo -u "$DZTGBOT_SERVICE_USER" "$VENV_DIR/bin/python" - "$source_db" "$backup_path" <<'PY'
import sqlite3
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
if not src.is_file():
    raise SystemExit("source database missing")
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
try:
    destination = sqlite3.connect(dst)
    try:
        source.backup(destination)
    finally:
        destination.close()
finally:
    source.close()
print(f"backup_ok path={dst.name}")
PY
    then
        die "Workflow database backup failed. Resolve the failure before migration."
    fi

    chown "$DZTGBOT_SERVICE_USER:$SERVICE_GROUP" "$backup_path"
    chmod 0600 "$backup_path"
    log "Created workflow database backup under ${backup_dir}."
}

verify_workflow_integrity() {
    local db_path="$1"
    local result
    result="$(sudo -u "$DZTGBOT_SERVICE_USER" "$VENV_DIR/bin/python" - "$db_path" <<'PY'
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    row = connection.execute("PRAGMA integrity_check").fetchone()
finally:
    connection.close()
print(row[0] if row else "failed")
PY
)" || die "Workflow database integrity check could not run."
    [[ "$result" == "ok" ]] || die "Workflow database integrity check failed (${result}). Restore from a known-good backup before continuing."
}

prepare_workflow_database() {
    # Protected local SQLite outside checkout/sync/network storage.
    require_safe_absolute_path "WORKFLOW_DB_PATH" "$WORKFLOW_DB_PATH"
    [[ "$(dirname -- "$WORKFLOW_DB_PATH")" == "$STATE_ROOT" ]] || \
        die "WORKFLOW_DB_PATH must remain a direct child of ${STATE_ROOT}."

    install -d -o "$DZTGBOT_SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$STATE_ROOT"
    install -d -o "$DZTGBOT_SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "${STATE_ROOT}/backups"
    ensure_state_disk_space "$STATE_ROOT"

    if [[ -e "$WORKFLOW_DB_PATH" ]]; then
        [[ ! -L "$WORKFLOW_DB_PATH" ]] || die "WORKFLOW_DB_PATH must not be a symbolic link."
        [[ -f "$WORKFLOW_DB_PATH" ]] || die "WORKFLOW_DB_PATH must be a regular file when present."
        chown "$DZTGBOT_SERVICE_USER:$SERVICE_GROUP" "$WORKFLOW_DB_PATH"
        chmod 0600 "$WORKFLOW_DB_PATH"
        verify_workflow_integrity "$WORKFLOW_DB_PATH"
        backup_existing_workflow_database "$WORKFLOW_DB_PATH"
    fi

    # Migration preflight: open repository as the service account, apply schema, report version.
    # This never contacts Telegram, Gemini, Jira, or NetworkManager.
    if ! sudo -u "$DZTGBOT_SERVICE_USER" \
        env "WORKFLOW_DB_PATH=${WORKFLOW_DB_PATH}" \
        "PYTHONPATH=${PROJECT_DIR}/src" \
        "$VENV_DIR/bin/python" - <<'PY'
import asyncio
import os
from pathlib import Path

from dztgbot.infrastructure.persistence.workflow_sqlite import (
    LATEST_SCHEMA_VERSION,
    SQLiteWorkflowRepository,
)

async def main() -> None:
    path = Path(os.environ["WORKFLOW_DB_PATH"])
    repository = SQLiteWorkflowRepository(path)
    await repository.initialize()
    version = await repository.schema_version()
    await repository.close()
    if version != LATEST_SCHEMA_VERSION:
        raise SystemExit(f"unexpected schema version {version}")
    print(f"workflow_migration_preflight_ok schema_version={version}")

asyncio.run(main())
PY
    then
        die "Workflow database migration preflight failed. See docs/operations/workflow-db-runbook.md."
    fi

    [[ -f "$WORKFLOW_DB_PATH" && ! -L "$WORKFLOW_DB_PATH" ]] || die "Workflow database was not created as a regular file."
    chown "$DZTGBOT_SERVICE_USER:$SERVICE_GROUP" "$WORKFLOW_DB_PATH"
    chmod 0600 "$WORKFLOW_DB_PATH"

    # Companion WAL/SHM files, when present, stay service-owned and non-world-readable.
    local companion
    for companion in "${WORKFLOW_DB_PATH}-wal" "${WORKFLOW_DB_PATH}-shm" "${WORKFLOW_DB_PATH}-journal"; do
        if [[ -e "$companion" ]]; then
            chown "$DZTGBOT_SERVICE_USER:$SERVICE_GROUP" "$companion"
            chmod 0600 "$companion"
        fi
    done

    sudo -u "$DZTGBOT_SERVICE_USER" test -r "$WORKFLOW_DB_PATH" || die "The service account cannot read WORKFLOW_DB_PATH."
    sudo -u "$DZTGBOT_SERVICE_USER" test -w "$WORKFLOW_DB_PATH" || die "The service account cannot write WORKFLOW_DB_PATH."

    # Credentials store lives beside rules by default; ensure parent permissions only.
    if [[ -e "$USER_CREDENTIALS_PATH" ]]; then
        [[ ! -L "$USER_CREDENTIALS_PATH" ]] || die "USER_CREDENTIALS_PATH must not be a symbolic link."
        [[ -f "$USER_CREDENTIALS_PATH" ]] || die "USER_CREDENTIALS_PATH must be a regular file when present."
        chown "$DZTGBOT_SERVICE_USER:$SERVICE_GROUP" "$USER_CREDENTIALS_PATH"
        chmod 0600 "$USER_CREDENTIALS_PATH"
    fi

    log "Workflow database path prepared with service ownership and mode 0600."
}

configure_vpn_sudoers() {
    [[ "$VPN_ALLOW_START" == "true" ]] || {
        if [[ -e "$SUDOERS_DESTINATION" ]]; then
            if grep -Fqx '# Managed by DZTGBot deploy.sh; do not edit.' "$SUDOERS_DESTINATION"; then
                rm -f -- "$SUDOERS_DESTINATION"
                log "Removed the managed VPN sudoers rule because remote start is disabled."
            else
                die "Refusing to replace or remove the unmanaged ${SUDOERS_DESTINATION} file."
            fi
        fi
        return
    }

    [[ "$VPN_CONNECTION_NAME" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "VPN_CONNECTION_NAME must use only letters, digits, dot, underscore, or hyphen when remote start is enabled."
    [[ "$VPN_PROFILE_PATH" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "VPN_PROFILE_PATH contains characters that are unsafe in a generated sudoers rule."
    [[ "$VPN_NMCLI_BIN" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "VPN_NMCLI_BIN contains characters that are unsafe in a generated sudoers rule."
    local temporary_sudoers
    temporary_sudoers="$(mktemp)"
    {
        printf '%s\n' '# Managed by DZTGBot deploy.sh; do not edit.'
        printf '%s ALL=(root) NOPASSWD: %s connection load %s\n' "$DZTGBOT_SERVICE_USER" "$VPN_NMCLI_BIN" "$VPN_PROFILE_PATH"
        printf '%s ALL=(root) NOPASSWD: %s connection up %s\n' "$DZTGBOT_SERVICE_USER" "$VPN_NMCLI_BIN" "$VPN_CONNECTION_NAME"
    } > "$temporary_sudoers"
    chmod 0440 "$temporary_sudoers"
    visudo -cf "$temporary_sudoers" >/dev/null
    install -o root -g root -m 0440 "$temporary_sudoers" "$SUDOERS_DESTINATION"
    rm -f -- "$temporary_sudoers"
}

sed_escape() {
    printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

install_systemd_unit() {
    local template="$PROJECT_DIR/deploy/systemd/dztgbot.service"
    local rendered
    local no_new_privileges="true"
    [[ "$VPN_ALLOW_START" == "true" ]] && no_new_privileges="false"
    rendered="$(mktemp --suffix=.service)"

    sed \
        -e "s|TODO_REPLACE_WITH_SERVICE_USER|$(sed_escape "$DZTGBOT_SERVICE_USER")|g" \
        -e "s|TODO_REPLACE_WITH_SERVICE_GROUP|$(sed_escape "$SERVICE_GROUP")|g" \
        -e "s|/TODO_REPLACE_WITH_ABSOLUTE_PROJECT_DIRECTORY|$(sed_escape "$PROJECT_DIR")|g" \
        -e "s|/TODO_REPLACE_WITH_ABSOLUTE_ENVIRONMENT_FILE|$(sed_escape "$DZTGBOT_ENV_FILE")|g" \
        -e "s|/TODO_REPLACE_WITH_ABSOLUTE_VENV_PYTHON|$(sed_escape "$VENV_DIR/bin/python")|g" \
        -e "s|TODO_REPLACE_WITH_NO_NEW_PRIVILEGES|${no_new_privileges}|g" \
        "$template" > "$rendered"

    if grep -Eq '=(/)?TODO_REPLACE' "$rendered"; then
        rm -f -- "$rendered"
        die "The rendered systemd unit still contains unresolved assignment placeholders."
    fi
    systemd-analyze verify "$rendered"
    install -o root -g root -m 0644 "$rendered" "$UNIT_DESTINATION"
    rm -f -- "$rendered"
    systemctl daemon-reload
}

start_service() {
    systemctl enable "$SERVICE_NAME"
    systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log "Restarting active ${SERVICE_NAME}."
        systemctl restart "$SERVICE_NAME"
    else
        log "Starting ${SERVICE_NAME}."
        systemctl start "$SERVICE_NAME"
    fi

    local waited=0
    while (( waited < SERVICE_ACTIVE_WAIT_SECONDS )); do
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            log "${SERVICE_NAME} is active after ${waited}s."
            return
        fi
        # Fail fast on permanent failure states rather than waiting the full window.
        if systemctl is-failed --quiet "$SERVICE_NAME"; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
    die "The bot did not reach the active state within ${SERVICE_ACTIVE_WAIT_SECONDS}s. See docs/operations/workflow-db-runbook.md for recovery."
}

main() {
    require_root
    [[ -n "${DZTGBOT_SERVICE_USER:-}" ]] || die "Set DZTGBOT_SERVICE_USER to the dedicated non-root account name."
    [[ "$DZTGBOT_SERVICE_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "DZTGBOT_SERVICE_USER has an invalid system-account name."
    [[ -n "${DZTGBOT_ENV_FILE:-}" ]] || die "Set DZTGBOT_ENV_FILE to the protected absolute environment-file path."
    DZTGBOT_INSTALL_SYSTEM_PACKAGES="$(normalize_bool "${DZTGBOT_INSTALL_SYSTEM_PACKAGES:-true}")" || die "DZTGBOT_INSTALL_SYSTEM_PACKAGES must be true or false."
    export DZTGBOT_INSTALL_SYSTEM_PACKAGES

    local script_directory
    script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    PROJECT_DIR="$(cd -- "$script_directory/.." && pwd -P)"
    require_safe_absolute_path "project directory" "$PROJECT_DIR"
    [[ -f "$PROJECT_DIR/requirements.txt" ]] || die "Run the checked-in script from a complete DZTGBot checkout."

    detect_platform
    log "Detected supported target: ${PLATFORM_ID} ${PLATFORM_VERSION}."
    prepare_environment_file
    validate_environment
    install_base_packages
    ensure_service_account
    # Stop before replacing the virtualenv and migrating durable state so restart is deterministic.
    stop_service_if_running
    build_virtual_environment
    prepare_rules
    prepare_workflow_database
    install_vpn_packages
    configure_vpn_sudoers
    install_systemd_unit
    start_service

    log "Deployment completed and ${SERVICE_NAME} is active."
    log "View logs with: journalctl -u ${SERVICE_NAME} -f"
    log "Workflow DB runbook: docs/operations/workflow-db-runbook.md"
    log "Credential threat model: docs/security/credential-threat-model.md"
    log "Complete the supervised end-to-end plan before treating this release as production-ready."
    log "This installer never mutates Jira, never starts a full VPN tunnel automatically, and never loads live secrets into the repository."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
