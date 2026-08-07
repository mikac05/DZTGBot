# DZTGBot continuation checkpoint

## Current status

The full end-to-end bot pipeline is implemented and tested:
1. **Gemini AI Analysis**: Production system instruction and prompt in `src/dztgbot/analysis.py` with strict Pydantic `JiraTaskTemplate` schema validation.
2. **Jira REST API v2 Integration**: `src/dztgbot/jira_client.py` handles PAT validation (`/myself`) and issue creation (`/issue`).
3. **Per-User PAT Storage**: `src/dztgbot/user_store.py` securely persists per-user PATs to disk (mode 0600, atomic writes).
4. **Interactive Telegram Flow**: `/start` greets, `/auth` collects PAT in private chat (auto-deleting the message), `/logout` removes credentials, and forwarded messages receive a preview with `[✅ Create Issue]` and `[❌ Cancel]` inline buttons.
5. **VPN Support**: NetworkManager L2TP/IPsec status/start controls integrated via `src/dztgbot/vpn.py` and `scripts/deploy.sh`.
6. **Hardened Deployment**: Rerunnable `scripts/deploy.sh` for Ubuntu 22.04 target servers and `deploy/systemd/dztgbot.service` systemd unit.

24 offline tests and secret-safety validation pass cleanly.

## Exact next action

1. Resolve Git write access to `origin` (or update remote URL) and push the branch.
2. Transfer or clone the repository to the target Ubuntu 22.04 server.
3. Run `sudo DZTGBOT_SERVICE_USER=dztgbot DZTGBOT_ENV_FILE=/etc/dztgbot/env bash scripts/deploy.sh` to generate `/etc/dztgbot/env`.
4. Configure `/etc/dztgbot/env` via `sudoedit /etc/dztgbot/env` with real credentials (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `JIRA_URL`, `TELEGRAM_ADMIN_USER_IDS`).
5. Re-run `deploy.sh` to complete installation and start `dztgbot.service`.

## Inputs still required from the user or target environment

- Confirm target server OS is Ubuntu 22.04 LTS.
- Real `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`, and `JIRA_URL` values (to be configured in `/etc/dztgbot/env`).
- User Personal Access Tokens (PATs) submitted via `/auth` in private Telegram chat with the bot.
- If L2TP/IPsec VPN is required, the private `.nmconnection` profile installed at mode 0600 on the server.

## Do not redo

- Do not rewrite `user_store.py`, `jira_client.py`, or `jira_auth.py` — they are fully implemented and verified.
- Do not remove inline `[✅ Create Issue]` / `[❌ Cancel]` buttons — explicit human confirmation before posting is a core requirement.
- Do not hardcode Jira credentials or tokens in tracked files.
- Do not remove Ubuntu 22.04 platform checks.

## Required verification on resume

1. Confirm 24 offline tests pass (`$env:PYTHONPATH="src;."; .venv\Scripts\pytest`).
2. Run `python scripts/handoff.py validate` to verify secret safety and context boundaries.
3. Follow the deployment guide to test live on Ubuntu 22.04.
