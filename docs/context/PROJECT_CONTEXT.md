# DZTGBot project context

## Purpose and boundary

DZTGBot is a long-running Python 3.12 Telegram bot intended for a privately managed Ubuntu 24.04 server. It accepts direct forwards, replies to forwards, and manual drafts; asks Gemini for a strictly validated Jira task template; presents a human-reviewable Telegram preview; and creates or updates issues in a self-hosted Jira Server / Data Center instance behind NetworkManager L2TP/IPsec using per-user Jira Personal Access Tokens.

The first safe release is private-chat-only. Every Jira create or update requires explicit human confirmation. Automated tests never perform real external mutations.

## Current architecture

- `src/dztgbot/domain/`: frozen domain entities, full `DraftState` FSM, typed error/certainty taxonomy, strict `j1:<action>:<opaque-token>` callback grammar, security policies, and provider-free ports.
- `src/dztgbot/services/`: pure workflow, connectivity, intake, callback authorization, submission/reconciliation, attachment, resource-limit, card-tracker, unread notification poller, issue triage, and observability services.
- `src/dztgbot/infrastructure/persistence/`: versioned SQLite WAL repository (schema v4) for drafts, callback hashes, attempts, attachments, published issues, card message tracker, unread notification tracker, expiry, and atomic current+1 revision CAS.
- `src/dztgbot/infrastructure/jira_gateway.py`: one lifecycle-managed `httpx.AsyncClient`, request-local PAT headers, canonical payload/diff/hash mapping, dynamic workflow transitions, issue links (`Relates to`, `Impediment`), issue watchers, bounded timeouts/errors, metadata caching, and no blind create retry.
- `src/dztgbot/infrastructure/gemini_gateway.py`: strict structured DTOs, prompt budgets, total deadlines, bounded fallback, rate-limit classification, and no unsupported media bytes.
- `src/dztgbot/infrastructure/keyed_processor.py`: workflow/collection keyed serialization with bounded admission and validated concurrency-one fallback.
- `src/dztgbot/ui/`: HTML-safe renderers (Universal Cards, Compact Paginated Search, Standup Digest), auth-aware reply keyboards, action bar keyboards (`Move`, `Edit`, `Comment`, `Block`, `Assign`, `Watch`, `Sub-task`, `Figma Spec`), and thin private handlers using parse -> service -> I/O -> service -> render.
- `src/dztgbot/__main__.py`: sole composition root, PTB update processor, auth-aware reply keyboards, `/standup` command, inline query, link unfurling, resource-limit/metrics wiring, and deterministic reverse-order teardown.
- `src/dztgbot/user_store.py`: PAT-only copy-on-write credential storage with schema/size/regular-file validation, mode `0600`, corruption quarantine, and previous-copy recovery.
- `src/dztgbot/jira_auth.py` and `admin.py`: three-minute PAT-only private auth, optional allowlist enforcement, dynamic 3-row authenticated reply keyboard, honest local logout, and private numeric-ID-restricted administration.
- `src/dztgbot/rules.py`: bounded signature-cached rules with atomic update and last-known-good recovery.
- `src/dztgbot/core.py`, `analysis.py`, and `jira_client.py`: non-authoritative compatibility facades only; they own no workflow state.
- `scripts/deploy.sh` and `deploy/systemd/dztgbot.service`: Ubuntu 24.04-only deployment, protected local workflow DB preflight/backup/migration, non-root runtime, root-owned secrets, and narrow optional VPN controls.
- `pyproject.toml`, `requirements-dev.txt`, and `.github/workflows/quality.yml`: pinned quality stack with Ruff, strict mypy, focused branch coverage, ShellCheck, and offline CI.

Dependency direction is `ui -> services -> domain`; infrastructure implements ports and the composition root injects adapters. Domain/services import no Telegram or provider SDK types. SQLite is the only workflow authority.

## Settled decisions

- Use `python-telegram-bot` 22.8 async polling without a web server.
- Use Jira PAT-only Bearer authentication. Passwords, Basic auth, and session cookies are rejected.
- Keep authentication, workflows, callbacks, Jira mutations, and admin commands private-chat-only for the first release.
- Dynamic auth-aware main reply keyboard (`[🔑 連結 Jira]` vs `[📋 指派給我的] [🚩 我建的] / [🔍 搜尋] [📝 新建] / [🚪 Logout]`).
- Compact paginated JQL search results (5 items per page) with 1-tap card preview buttons (`[1. PROJ-123]`).
- Universal Issue Card Action Bar: Dynamic Primary Transition (`[▶️ Start Dev]`), `[➡️ Move]`, `[📝 Edit]`, `[💬 Comment]`, `[⚠️ Block]`, `[👤 Assign]`, `[👁️ Watch]`, `[➕ Sub-task]`, `[Open in Jira ↗]`, and auto-detected `[🎨 Figma Spec ↗]`.
- Direct Reply to Card for instant Jira comment & photo attachment uploads.
- Auto-detect `PROD-xxx` keys on forward/input -> auto-target `BOT` project & execute `POST /issueLink` (`Relates to`).
- Executive Standup Summary Report (`/standup`) grouping tickets into Blocked, In Progress, In QA/Review, and Recently Done.
- Background Unread Notification Poller (`NotificationPollerService`) running every 300s (5 min) with SQLite schema v4 tracker.
- Persist workflow state in a protected local SQLite WAL database outside Git, synchronized folders, and network filesystems.
- Store callback token hashes only. Tokens contain at least 128 bits of cryptographic randomness and bind exact workflow authorization context.
- Treat a dispatched Jira create/update with unknown outcome as reconciliation-required and never automatically retry it.
- Require atomic current+1 revision CAS for existing aggregate changes; same/stale/skipped revisions fail closed.
- Keep a single shared Jira transport with request-local credentials and bounded concurrency.
- Keep Gemini text-only for the first release; unsupported media bytes are not sent to the model.
- Keep NetworkManager L2TP/IPsec because the supplied server is incompatible with WireGuard.
- Keep credentials under host confinement and mode `0600`; encryption is deferred until a separately approved root-managed key lifecycle and vetted AEAD/rotation/backup/rollback design exists.
- Keep all live validation separately authorized and supervised.

## Required private inputs

These names may be documented, but their values must never be written to tracked files:

- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `TELEGRAM_ADMIN_USER_IDS`
- optional `TELEGRAM_ALLOWED_USER_IDS`
- approved Jira task rules and Gemini prompts
- per-user Jira PATs
- when VPN is enabled: private NetworkManager profile, connection name, endpoint, username, password, and IPsec key
- deployment-specific service account, checkout location, protected environment path, workflow DB path, backup location, and custom CA path

## Evidence boundaries

- Offline suite: **466 tests run; 465 passed and one Windows-only `O_NOFOLLOW` skip**.
- Ruff, strict mypy for all source files, `pip check`, compilation, Git-Bash deploy syntax, and diff checks pass.
- Focused branch coverage is >90% for FSM/callback/security and >77% for repository/submission.
- Recovery/concurrency/resource matrix passed 132/132 repeated executions.
- Real Telegram, Gemini, Jira, VPN, systemd, Ubuntu deployment, target DB operations, and server behavior remain unverified.
- The service remains pilot-only pending supervised target-environment validation.
- ShellCheck is configured in Ubuntu 24.04 CI but was unavailable locally.
- Real Telegram, Gemini, Jira, VPN, systemd, Ubuntu deployment, target DB operations, and server behavior remain unverified.
- The service remains pilot-only pending supervised target-environment validation.

## Durable context model

- `PROJECT_CONTEXT.md`: stable architecture, decisions, private-input names, and evidence boundaries.
- `CONTINUE_HERE.md`: exact checkpoint and one concrete next action.
- `HANDOFF.md`: concise operational snapshot refreshed before switching.
- `AGENTS.md`: command and safety contract.
- `MASTER_PLAN.md`: completed multi-agent remediation specification and exit gates.
- `docs/architecture/`: contracts, implemented architecture, provider boundaries, and migration record.
- `docs/operations/workflow-db-runbook.md`: target DB operations and recovery.
- `docs/security/credential-threat-model.md`: selected credential boundary and encryption deferral.
- `docs/reviews/*verification.md`: aligned Phase 9 architecture, performance/recovery, and security evidence.
- `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md`: original audit whose findings are mapped in current documentation and tests.
