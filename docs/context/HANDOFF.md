# DZTGBot handoff

## Current objective

Complete end-to-end implementation of DZTGBot — an async Python 3.12 Telegram bot that normalizes forwarded messages, uses Gemini AI to analyze them into Jira task templates, prompts users for explicit confirmation via interactive Telegram inline buttons (`[✅ Create Issue]` / `[❌ Cancel]`), and posts issues directly to a self-hosted Jira Server / Data Center instance via REST API v2 using per-user Personal Access Tokens (PATs).

## Completed

- Implemented the Python 3.12 async forward-only Telegram bot core (`core.py`, `analysis.py`, `config.py`, `__main__.py`).
- Implemented production Gemini AI system instructions and prompt in `analysis.py` with Pydantic `JiraTaskTemplate` validation.
- Implemented `JIRA_URL`, `JIRA_VERIFY_SSL`, `JIRA_DEFAULT_PROJECT_KEY`, and `USER_CREDENTIALS_PATH` configuration settings.
- Implemented non-blocking Jira Server / Data Center REST API v2 client (`jira_client.py`) supporting PAT authentication (`/myself`) and issue creation (`/issue`).
- Implemented secure per-user credential store (`user_store.py`) using atomic writes and mode 0600 file permissions.
- Implemented Telegram auth flow (`jira_auth.py`): `/start`, private-chat `/auth` PAT collection (with token message auto-deletion), and `/logout`.
- Implemented interactive Telegram preview rendering in `core.py` with inline confirmation buttons (`[✅ Create Issue]` / `[❌ Cancel]`).
- Added runtime Jira rules management with admin-only hot updates and last-known-good fallback (`rules.py`, `admin.py`).
- Added NetworkManager L2TP/IPsec status/start support (`vpn.py`).
- Added hardened systemd service (`deploy/systemd/dztgbot.service`) and rerunnable Ubuntu 24.04 deployment script (`scripts/deploy.sh`).
- Wrote step-by-step deployment guide for Ubuntu 24.04.
- Added 24 offline behavioral, credential storage, and secret-safety tests.

## Decisions

- Git-tracked files are the only transferable memory. Account memory and authenticated sessions are never assumed.
- `DZTGBot handoff` is the only phrase that authorizes the workflow to commit and push this repository.
- `DZTGBot continue` may fetch and fast-forward only when the checkout is clean.
- Per-user Jira authentication: users send their Jira Personal Access Token via `/auth` in private chat; tokens are auto-deleted from chat history and stored in `user_credentials.json` (mode 0600).
- Explicit human confirmation: previews feature inline `[✅ Create Issue]` and `[❌ Cancel]` buttons; issues are created only upon explicit user tap.
- Target platform is strictly Ubuntu 24.04.
- Secrets are kept outside tracked files (in environment variables or `/etc/dztgbot/env`).

## Open items

- Git remote `origin` write access is pending; commits are saved locally until upstream permission or remote URL is updated.
- Real bot tokens, Gemini API keys, Jira server URL, and numeric admin IDs must be entered on the target server in `/etc/dztgbot/env`.
- Individual Telegram users must run `/auth` to supply their Jira PAT.

## Exact next action

Resolve Git remote `origin` write permission (or update remote URL), run `DZTGBot handoff` again to push, and then follow `scripts/deploy.sh` instructions to deploy on the target Ubuntu 24.04 server.

## Verification

- Test suite: 24 offline tests passed in Python 3.12 (`.venv`).
- Handoff safety validation: secret scan and ignore rules passed (`python scripts/handoff.py validate`).
- Structural file validation passed.

## Transfer limitations

Git does not transfer `.env`, private VPN files, tokens, keys, server access, Telegram/Gemini sessions, browser authentication, connector permissions, or AI-account memory. Configure those separately and privately on each authorized environment.

## Git snapshot metadata

<!-- HANDOFF-METADATA:START -->
- Generated UTC: `2026-08-07T06:24:26Z`
- Branch: `main`
- Upstream: `origin/main`
- Base commit before this handoff: `8a4ab42e6128`
- Working-tree entries before metadata refresh: `1`
- The handoff commit is the commit containing this file.
<!-- HANDOFF-METADATA:END -->
