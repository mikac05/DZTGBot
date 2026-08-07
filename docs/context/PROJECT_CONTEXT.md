# DZTGBot project context

## Purpose and boundary

DZTGBot is a long-running Python 3.12 Telegram bot intended for a privately managed Linux server. It accepts direct forwards and replies directly to forwards from private or group chats, normalizes their origin/text/media metadata, asks Gemini for a strictly validated Jira task template, presents an interactive Telegram preview with inline confirmation buttons (`[✅ Create Issue]` / `[❌ Cancel]`), and posts issues directly to a self-hosted Jira Server / Data Center instance (behind L2TP/IPsec VPN) using per-user Personal Access Tokens (PATs).

## Current architecture

- `src/dztgbot/core.py`: forward-only Telegram intake, normalized dataclasses, preview rendering with inline confirmation keyboard, and issue creation callbacks.
- `src/dztgbot/analysis.py`: async Google GenAI request with production system instructions, strict Pydantic validation, and bounded preview rendering.
- `src/dztgbot/jira_client.py`: non-blocking Jira Server / Data Center REST API v2 client (`/myself` validation and `/issue` creation).
- `src/dztgbot/user_store.py`: atomic disk-backed storage for per-user Jira credentials (PATs) with mode 0600 file permissions.
- `src/dztgbot/jira_auth.py`: `/start`, private-chat `/auth` PAT collection conversation (with token message auto-deletion), and `/logout` handlers.
- `src/dztgbot/rules.py`: atomic disk-backed rules with hot reload and a last-known-good fallback.
- `src/dztgbot/admin.py`: numeric-ID-restricted `/rules`, `/setrules`, `/vpn`, and `/vpnstart` commands.
- `src/dztgbot/vpn.py`: read-only status and optional narrowly authorized start for one NetworkManager L2TP/IPsec connection.
- `src/dztgbot/config.py`: environment-only configuration validation including `JIRA_URL`, `JIRA_VERIFY_SSL`, `JIRA_DEFAULT_PROJECT_KEY`, and `USER_CREDENTIALS_PATH`.
- `src/dztgbot/__main__.py`: fully async polling lifecycle (`allowed_updates` for messages and callback queries) and privacy-safe error logging.
- `scripts/deploy.sh`: rerunnable Ubuntu 22.04-only deployment gate.
- `deploy/systemd/dztgbot.service`: non-root, journald-backed, hardened long-running service template.
- `tests/test_bot.py`: offline behavioral, credential storage, and safety tests.
- `scripts/handoff.py`: cross-account/device context validation and safe Git synchronization.

## Settled decisions

- Use `python-telegram-bot` 22.8 for its async polling API and no inbound web-server requirement.
- Use the official `google-genai` async client and strict local Pydantic validation.
- Analyze only forwarded content. Ordinary messages are ignored.
- Require per-user authentication for Jira access: users send their Jira Personal Access Token via `/auth` in private chat; tokens are immediately deleted from Telegram chat history and stored locally with mode 0600 permissions.
- Explicit human approval required before issue creation: previews present inline `[✅ Create Issue]` and `[❌ Cancel]` buttons; issues are created only upon user confirmation.
- Connect to Jira Server / Data Center REST API v2 using Bearer auth (PAT).
- Keep all server secrets in environment variables or an ignored `.env`/protected systemd environment file.
- Use NetworkManager L2TP/IPsec because the supplied VPN server setup cannot use WireGuard.
- Map the private Windows VPN profile only through the tracked placeholder `.nmconnection` template; never commit or quote the private XML or resulting profile.
- Keep the bot responsive when VPN status is disabled/down and still generate a Jira preview while warning that Jira is unreachable.
- Log to journald without forwarded text, generated descriptions, tokens, provider error text, VPN endpoints, or credentials.
- Synchronize durable AI context through ordinary Git commits. Never store chat history or secrets as continuity data.

## Deployment facts

- The installer accepts Ubuntu 22.04 only and refuses every other distribution or release.
- Ubuntu requires an approved Python 3.12 interpreter path when `python3.12` is not already available.
- The installer never enables NetworkManager or starts the full-tunnel VPN automatically.
- The protected environment file and VPN profile must stay outside the checkout, root-owned, and mode `0600`.

## Required private inputs

These names may be documented, but their values must never be written to tracked files:

- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `TELEGRAM_ADMIN_USER_IDS`
- approved Jira task rules
- approved Gemini system instruction and analysis prompt
- when VPN is enabled: private NetworkManager profile, local connection name, endpoint, username, password, and IPsec key
- deployment-specific service account, checkout location, and protected environment-file location

## Evidence boundaries

- Offline tests and syntax checks verify local behavior only.
- Real Telegram/Gemini tests require privately supplied credentials.
- VPN compatibility requires a console-supervised test on the target host.
- systemd hardening and package installation require validation on the Ubuntu 22.04 target.
- No live target-server deployment has been completed in this repository history yet.

## Durable context model

- `PROJECT_CONTEXT.md`: stable facts and decisions.
- `CONTINUE_HERE.md`: authoritative detailed checkpoint and next action.
- `HANDOFF.md`: concise snapshot refreshed before switching.
- `AGENTS.md`: command and safety contract.
- `.agents/`: portable Codex/Antigravity skill, rule, and slash workflows.
