# DZTGBot handoff

## Current objective

Complete end-to-end implementation and remote deployment of DZTGBot — an async Python 3.12 Telegram bot running on an Ubuntu 24.04 server that normalizes forwarded messages, uses Gemini AI to analyze them into Jira task templates, prompts users for confirmation via interactive Telegram inline buttons, supports attachment uploading, automatic free-tier model failover, persistent ReplyKeyboard menus, and posts issues directly to a self-hosted Jira Server / Data Center instance via REST API v2 using per-user Personal Access Tokens (PATs) over NetworkManager L2TP/IPsec split-tunneled VPN.

## Completed

- **Remote Server Deployment**: Fully deployed and active on Oracle Cloud ARM Ubuntu 24.04 (`129.150.55.33`) as a hardened systemd service (`dztgbot.service`).
- **NetworkManager Split Tunneling**: Configured L2TP/IPsec connection (`dztgbot-vpn`) with `ipv4.never-default yes` and route `207.148.45.197/32`. Fixed Netplan configuration to ensure `enp0s3` interface is managed by NetworkManager while preserving direct SSH and Telegram API connectivity.
- **Automatic VPN Triggers in JiraClient**: Integrated `vpn_manager` directly into `JiraClient` so that `validate_credentials` (during `/auth`), `create_issue`, `update_issue`, and `add_attachment` automatically bring up the VPN tunnel before issuing HTTP requests to Jira.
- **Gemini Free-Tier Model Auto-Fallback**: Implemented priority fallback queue (`gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.1-flash-lite`) that seamlessly handles HTTP 429 rate limit errors.
- **Taiwan Traditional Chinese Localization**: All bot text, buttons, errors, help guides (`/help`), and interactive draft menus translated to Taiwan Traditional Chinese.
- **Secure Per-User Credentials**: User PATs collected via `/auth` in private chat with automatic token message deletion, stored atomically with 0600 file permissions.
- **Image Attachments**: Displays image counts in preview and uploads compressed Telegram photos directly to Jira issues.
- **Test Suite**: All 30/30 offline unit tests pass locally and on the target deployment server.

## Decisions

- **Split Tunneling**: VPN routes strictly Jira traffic (`207.148.45.197/32`) via `ppp0`. All other traffic (Telegram API `api.telegram.org`, Gemini API, SSH) uses the server's direct internet connection (`enp0s3`).
- **Auto VPN Start**: `JiraClient` manages VPN state lazily, triggering `vpn_manager.start()` on demand before any Jira REST API request.
- **Secrets & Storage**: No credentials or VPN secrets in tracked files. Sudoers rule `/etc/sudoers.d/dztgbot-vpn` grants non-root `dztgbot` service user permission to execute only specific nmcli commands without password.

## Open items

- None. Server is live and fully functional.

## Exact next action

Monitor live bot operations via `sudo journalctl -u dztgbot.service -f` on the remote server.

## Verification

- **Remote Service Status**: `dztgbot.service` active and running (`Active: active (running)`).
- **Unit Tests**: 30/30 tests passed in Python 3.12 (`.venv`).
- **Live VPN Test**: Activated L2TP/IPsec tunnel to `66.203.159.79`, verified 33ms ping to Jira `207.148.45.197`, verified concurrent direct access to `api.telegram.org`.
- **Handoff Safety**: Validated context boundaries and secret safety rules.

## Git snapshot metadata

<!-- HANDOFF-METADATA:START -->
- Generated UTC: `2026-08-07T09:59:48Z`
- Branch: `main`
- Upstream: `origin/main`
- Base commit before this handoff: `71f9f728bd6d`
- Working-tree entries before metadata refresh: `2`
- The handoff commit is the commit containing this file.
<!-- HANDOFF-METADATA:END -->
