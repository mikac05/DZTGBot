# Continue here

## Current status

DZTGBot is fully deployed, configured, tested, and actively running on the remote Oracle Cloud ARM Ubuntu 24.04 server (`129.150.55.33`) with active NetworkManager L2TP/IPsec split tunneling and automatic VPN triggering on all Jira API requests (including `/auth` PAT validation, issue creation, and image uploads).

- **Code & Test Status**: 30/30 unit tests pass locally and on the remote server.
- **Server Deployment**: Active systemd service `dztgbot.service` running under service user `dztgbot` on Oracle Cloud ARM Ubuntu 24.04.
- **VPN Split Tunneling**: Configured with NetworkManager (`dztgbot-vpn`). `enp0s3` set to NetworkManager managed (`renderer: NetworkManager`). Only Jira IP `207.148.45.197/32` routes through L2TP/IPsec `ppp0` tunnel (`ipv4.never-default yes`). Telegram API and server SSH remain on direct internet.
- **Automatic VPN Triggers**: `JiraClient` automatically triggers `_ensure_vpn()` prior to all API calls (`validate_credentials`, `create_issue`, `update_issue`, `add_attachment`), preventing timeouts during user authentication or posting.
- **UI & Localization**: All user-facing interaction, menus, error messages, and guidance localized in Taiwan Traditional Chinese. Dynamic main menu keyboard (`[🔑 綁定 Jira 帳號] / [🚪 解綁 Jira 帳號]`, `[📝 手動建立 Jira 工單]`, `[📖 說明]`) persistent in Telegram client.
- **Gemini Free-Tier Failover**: Automatically cycles models (`gemini-3.5-flash-lite` -> `gemini-3.6-flash` -> `gemini-3.5-flash` -> `gemini-3.1-flash-lite`) on HTTP 429 rate limit.

## Exact next action

None required for deployment. The service is live and fully operational on the remote Ubuntu server. Optional next step: Monitor bot usage via `sudo journalctl -u dztgbot.service -f` on the remote server.

## Inputs still required from the user or target environment

None. Server environment and user credentials store are operational.

## Do not redo

- Do not re-run Netplan or NetworkManager interface setup; `enp0s3` is already managed and split tunneling is verified.
- Do not remove `_ensure_vpn()` calls in `JiraClient`.

## Required verification on resume

- Run `.venv/bin/python -m unittest discover -s tests -v` to ensure test suite remains green.
- Verify `sudo systemctl status dztgbot.service` is active on the target server.
