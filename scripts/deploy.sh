#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly SERVICE_NAME="dztgbot.service"
readonly UNIT_DESTINATION="/etc/systemd/system/${SERVICE_NAME}"
readonly STATE_ROOT="/var/lib/dztgbot"
readonly SUDOERS_DESTINATION="/etc/sudoers.d/dztgbot-vpn"

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
        ubuntu:22.04*|ubuntu:24.04*) ;;
        *) die "Supported target platforms are Ubuntu 22.04 and 24.04 LTS; detected ${PLATFORM_ID:-unknown} ${PLATFORM_VERSION:-unknown}." ;;
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
        exit 2
    fi

    [[ -f "$DZTGBOT_ENV_FILE" ]] || die "The environment path must be a regular file."
    [[ "$(stat -c '%u' "$DZTGBOT_ENV_FILE")" == "0" ]] || die "The environment file must be owned by root."
    [[ "$(stat -c '%a' "$DZTGBOT_ENV_FILE")" == "600" ]] || die "The environment file permissions must be exactly 0600."
}

validate_environment() {
    local key
    for key in TELEGRAM_BOT_TOKEN GEMINI_API_KEY GEMINI_MODEL TELEGRAM_ADMIN_USER_IDS JIRA_RULES_PATH JIRA_URL; do
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

    JIRA_RULES_PATH="$(env_value JIRA_RULES_PATH)"
    require_safe_absolute_path "JIRA_RULES_PATH" "$JIRA_RULES_PATH"
    [[ "$(dirname -- "$JIRA_RULES_PATH")" == "$STATE_ROOT" ]] || \
        die "JIRA_RULES_PATH must be a direct child of ${STATE_ROOT} so systemd can grant narrowly scoped write access."

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
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"
    "$VENV_DIR/bin/python" -m pip check
    PYTHONPATH="$PROJECT_DIR/src" "$VENV_DIR/bin/python" -m compileall -q "$PROJECT_DIR/src"
    PYTHONPATH="$PROJECT_DIR/src" "$VENV_DIR/bin/python" -m unittest discover -s "$PROJECT_DIR/tests" -v

    chown -R "root:$SERVICE_GROUP" "$VENV_DIR"
    chmod -R go-w "$VENV_DIR"
    if ! runuser -u "$DZTGBOT_SERVICE_USER" -- test -r "$PROJECT_DIR/src/dztgbot/__main__.py"; then
        die "The service account cannot read the project. Move it outside a private home/root directory or correct directory traversal permissions."
    fi
    runuser -u "$DZTGBOT_SERVICE_USER" -- test -x "$VENV_DIR/bin/python" || die "The service account cannot execute the virtual-environment Python."
    if runuser -u "$DZTGBOT_SERVICE_USER" -- test -w "$PROJECT_DIR/src/dztgbot/__main__.py"; then
        die "The service account must not be able to modify application source files."
    fi
}

prepare_rules() {
    [[ ! -L "$STATE_ROOT" ]] || die "The application state directory must not be a symbolic link."
    [[ ! -e "$STATE_ROOT" || -d "$STATE_ROOT" ]] || die "The application state path must be a directory."
    install -d -o "$DZTGBOT_SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$STATE_ROOT"

    if [[ ! -e "$JIRA_RULES_PATH" ]]; then
        install -o "$DZTGBOT_SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 "$PROJECT_DIR/config/jira_rules.example.txt" "$JIRA_RULES_PATH"
        log "Seeded placeholder Jira rules; replace them with approved rules through /setrules."
    fi
    [[ -f "$JIRA_RULES_PATH" && ! -L "$JIRA_RULES_PATH" ]] || die "JIRA_RULES_PATH must be a regular, non-symlink file."
    [[ -s "$JIRA_RULES_PATH" ]] || die "The runtime Jira rules file must not be empty."
    chown "$DZTGBOT_SERVICE_USER:$SERVICE_GROUP" "$JIRA_RULES_PATH"
    chmod 0600 "$JIRA_RULES_PATH"
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
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        systemctl restart "$SERVICE_NAME"
    else
        systemctl start "$SERVICE_NAME"
    fi
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
        die "The bot did not reach the active state."
    fi
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
    build_virtual_environment
    prepare_rules
    install_vpn_packages
    configure_vpn_sudoers
    install_systemd_unit
    start_service

    log "Deployment completed and ${SERVICE_NAME} is active."
    log "View logs with: journalctl -u ${SERVICE_NAME} -f"
    log "Run the Phase 6 Telegram test plan before treating this release as production-ready."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
