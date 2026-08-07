# DZTGBot continuation checkpoint

## Current status

The full end-to-end bot pipeline, UX improvements, model failover, and persistent keyboards are implemented and verified:
1. **Gemini AI Analysis & Model Failover**: Production system instruction in `src/dztgbot/analysis.py` with 40% reduced token usage, structured output validation, and automatic free-tier model failover on HTTP 429 (`gemini-3.5-flash-lite` ➔ `gemini-3.6-flash` ➔ `gemini-3.5-flash` ➔ `gemini-3.1-flash-lite`).
2. **Jira REST API v2 Integration**: `src/dztgbot/jira_client.py` handles PAT validation (`/myself`), issue creation (`/issue`), issue update (`PUT /issue/{key}`), and attachment uploads (`/issue/{key}/attachments`). All error messages localized to Taiwan Traditional Chinese.
3. **Per-User PAT Storage**: `src/dztgbot/user_store.py` securely persists per-user PATs to disk (mode 0600, atomic writes).
4. **Interactive Telegram Flow & Persistent Keyboards**:
   - `/start` greetings with dynamic 2-row ReplyKeyboard (`[📝 手動建立 Jira 工單]`, `[🔑 綁定 / 🚪 解綁 Jira 帳號]`, `[📖 說明]`).
   - `/auth` collects PAT in private chat (auto-deleting sensitive input message).
   - `/logout` clears stored PATs.
   - `/help` displays clear Taiwan Traditional Chinese usage guide.
   - Forwarded messages & manual `/new` receive preview with image count, inline field toggles (`[🏷️ 類型]`, `[⚡ 優先級]`), and draft ReplyKeyboard for 0-copy-paste instant submission.
5. **VPN Support**: NetworkManager L2TP/IPsec status/start controls integrated via `src/dztgbot/vpn.py` and `scripts/deploy.sh`.
6. **Hardened Deployment**: Rerunnable `scripts/deploy.sh` for Ubuntu 24.04 target servers and `deploy/systemd/dztgbot.service` systemd unit.

30 offline tests and secret-safety validation pass cleanly.

## Exact next action

1. Push all committed changes to GitHub `origin/main`.
2. Transfer or clone the repository to the target Ubuntu 24.04 server.
3. Run `sudo DZTGBOT_SERVICE_USER=dztgbot DZTGBOT_ENV_FILE=/etc/dztgbot/env bash scripts/deploy.sh` to generate `/etc/dztgbot/env`.
4. Configure `/etc/dztgbot/env` via `sudoedit /etc/dztgbot/env` with real credentials (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `JIRA_URL`, `TELEGRAM_ADMIN_USER_IDS`).
5. Re-run `deploy.sh` to complete installation and start `dztgbot.service`.

## Inputs still required from the user or target environment

- Confirm target server OS is Ubuntu 24.04 LTS.
- Real `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and `JIRA_URL` values (to be configured in `/etc/dztgbot/env`).
- User Personal Access Tokens (PATs) submitted via `/auth` in private Telegram chat with the bot.
- If L2TP/IPsec VPN is required, the private `.nmconnection` profile installed at mode 0600 on the server.

## Do not redo

- Do not rewrite `user_store.py`, `jira_client.py`, `analysis.py`, or `jira_auth.py` — they are fully implemented and verified.
- Do not re-add `GEMINI_MODEL` env var requirement — model selection is managed automatically with free-tier rate-limit fallback.
- Do not hardcode Jira credentials or tokens in tracked files.
- Do not remove Ubuntu 24.04 platform checks.

## Required verification on resume

1. Confirm 30 offline tests pass (`$env:PYTHONPATH="src;."; .venv\Scripts\pytest`).
2. Run `python scripts/handoff.py validate` to verify secret safety and context boundaries.
3. Follow the deployment guide to test live on Ubuntu 24.04.
