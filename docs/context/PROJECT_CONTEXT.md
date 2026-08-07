# DZTGBot project context

## Purpose and boundary

DZTGBot is a long-running Python 3.12 Telegram bot intended for a privately managed Ubuntu 24.04 server. It accepts direct forwards, replies to forwards, and manual drafts; asks Gemini for a structured Jira task template; presents a human-reviewable Telegram preview; and creates or updates issues in a self-hosted Jira Server / Data Center instance behind NetworkManager L2TP/IPsec using per-user credentials.

## Current architecture

- `src/dztgbot/core.py`: forwarded-message batching, manual drafts, mutable in-memory draft state, preview rendering, inline callbacks, Jira create/update orchestration, and photo attachments.
- `src/dztgbot/analysis.py`: async Google GenAI request with production system instructions, strict Pydantic validation, and bounded preview rendering.
- `src/dztgbot/jira_client.py`: non-blocking Jira Server / Data Center REST API v2 client for credential validation, issue creation/update, and attachments, with lazy VPN startup.
- `src/dztgbot/user_store.py`: atomic disk-backed storage for per-user Jira credentials (PATs) with mode 0600 file permissions.
- `src/dztgbot/jira_auth.py`: `/start`, `/help`, private-chat `/auth` credential collection conversation (with message auto-deletion), and `/logout` handlers.
- `src/dztgbot/rules.py`: atomic disk-backed rules with hot reload and a last-known-good fallback.
- `src/dztgbot/admin.py`: numeric-ID-restricted `/rules`, `/setrules`, `/vpn`, and `/vpnstart` commands.
- `src/dztgbot/vpn.py`: read-only status and optional narrowly authorized start for one NetworkManager L2TP/IPsec connection.
- `src/dztgbot/config.py`: environment-only configuration validation including `JIRA_URL`, `JIRA_VERIFY_SSL`, `JIRA_DEFAULT_PROJECT_KEY`, and `USER_CREDENTIALS_PATH`.
- `src/dztgbot/__main__.py`: fully async polling lifecycle (`allowed_updates` for messages and callback queries) and privacy-safe error logging.
- `scripts/deploy.sh`: rerunnable Ubuntu 24.04-only deployment gate.
- `deploy/systemd/dztgbot.service`: non-root, journald-backed, hardened long-running service template.
- `tests/test_bot.py`: offline utility, store, parser, client, model-fallback, VPN, and privacy-logging tests; it does not currently exercise complete Telegram handler journeys.
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

## Current architectural constraints

- Only Jira credential collection uses a formal `ConversationHandler` state. Draft, batch, attachment, and publication workflows use mutable `context.user_data` flags and objects.
- Draft and callback actions are not assigned unique operation identifiers. Generic callbacks resolve against the caller's latest in-memory state.
- `context.user_data` scopes draft and batch state to a Telegram user, not to a chat or individual workflow.
- The application enables concurrent update processing even though it uses `ConversationHandler` and shared mutable user state.
- Credentials and rules are persisted locally; drafts, batches, auth conversations, and publication state are lost on restart.
- The application has no database, workflow idempotency store, inbound/outbound rate limiter, functional health check, metrics backend, or alert integration.
- Media types are normalized, but Gemini receives only text and media-type labels. Only Telegram photos are subsequently uploaded to Jira.
- Group intake depends on external Telegram BotFather privacy/admin configuration that is not represented in this repository.
- `scripts/deploy.sh` currently requires a `GEMINI_MODEL` environment entry even though the application uses a fixed model queue and does not read that entry.

## Deployment facts

- The installer accepts Ubuntu 24.04 only and refuses every other distribution or release.
- Python 3.12 is the default system interpreter on Ubuntu 24.04 LTS.
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
- A 2026-08-07 repository review found 30/30 offline tests passing but no complete handler-level journey, concurrency, stale-callback, or mutation-recovery tests.
- The current Windows virtual environment reports two unsupported-platform packages through `pip check`; this does not establish the state of the separately managed Ubuntu environment.
- Real Telegram, Gemini, Jira, VPN, systemd, and server behavior require fresh validation in the target environment.
- Prior handoff records report a live Ubuntu 24.04 deployment and successful split-tunnel validation. The 2026-08-07 workflow review did not independently revalidate that external state.
- Authenticated sessions, credentials, server configuration, and external runtime state do not transfer through Git.

## Durable context model

- `PROJECT_CONTEXT.md`: stable facts and decisions.
- `CONTINUE_HERE.md`: authoritative detailed checkpoint and next action.
- `HANDOFF.md`: concise snapshot refreshed before switching.
- `AGENTS.md`: command and safety contract.
- `.agents/`: portable Codex/Antigravity skill, rule, and slash workflows.
- `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md`: decision-grade workflow, UX, resilience, maintainability, and operations review with ranked remediation priorities.
