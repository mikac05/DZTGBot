# DZTGBot project context

## Purpose and boundary

DZTGBot is a long-running Python 3.12 Telegram bot intended for a privately managed Ubuntu 24.04 server. It accepts direct forwards, replies to forwards, and manual drafts; asks Gemini for a structured Jira task template; presents a human-reviewable Telegram preview; and creates or updates issues in a self-hosted Jira Server / Data Center instance behind NetworkManager L2TP/IPsec using per-user credentials.

## Current architecture

- `src/dztgbot/domain/`: canonical domain models (`Draft`, `JiraTaskTemplate`, `SourceMessageRef`, `Attachment`, `SubmissionAttempt`, `PublishedIssue`), `DraftState` state machine, callback token grammar (`j1:<action>:<token>`), typed error taxonomy (`DomainError`), security policy, and pure Python domain protocols (`DraftRepositoryPort`, `UserRepositoryPort`, `RulesRepositoryPort`, `AIAnalyzerPort`, `JiraGatewayPort`, `VpnManagerPort`, `TaskSchedulerPort`, `ClockPort`, `IdGeneratorPort`, `RendererPort`).
- `src/dztgbot/services/`: pure application use cases (`WorkflowService` for manual draft creation, template edits, issue type/priority toggling, cancellation, and expiration; `ConnectivityService` for lazy VPN checks with single-flight locking and positive-status TTL caching; `IntakeService` for workflow-scoped message collection and batching; `CallbackService` for callback token verification and authorization).
- `src/dztgbot/infrastructure/persistence/`: SQLite workflow repository (`SQLiteWorkflowRepository`), versioned schema migrations (`001_initial.sql`, `002_indexes.sql`), SHA-256 callback token hashing, atomic one-winner compare-and-swap (CAS) state transitions, and submission attempt claims.
- `src/dztgbot/user_store.py`: atomic copy-on-write storage for per-user Jira credentials (PATs) with corruption quarantine and mode `0600` file permissions.
- `src/dztgbot/rules.py`: atomic disk-backed rules with hot reload and a last-known-good fallback.
- `src/dztgbot/admin.py`: numeric-ID-restricted `/rules`, `/setrules`, `/vpn`, and `/vpnstart` commands.
- `src/dztgbot/vpn.py`: read-only status and optional narrowly authorized start for one NetworkManager L2TP/IPsec connection.
- `src/dztgbot/config.py`: environment-only configuration validation including `JIRA_URL`, `JIRA_VERIFY_SSL`, `JIRA_DEFAULT_PROJECT_KEY`, and `USER_CREDENTIALS_PATH`.
- `src/dztgbot/core.py`: legacy Telegram bot handlers (to be refactored into thin presentation handlers in Phase 5 & 6).
- `src/dztgbot/__main__.py`: async entry point and global error handler.
- `scripts/deploy.sh`: rerunnable Ubuntu 24.04-only deployment gate.
- `deploy/systemd/dztgbot.service`: non-root, journald-backed, hardened long-running service template.
- `tests/`: 212 offline unit tests covering domain models, FSM transitions, callback security, error classifications, SQLite migrations & repository, UserStore copy-on-write, WorkflowService, ConnectivityService, intake service, callback authorization, VPN, and privacy-safe logging.
- `scripts/handoff.py`: cross-account/device context validation and safe Git synchronization.

## Settled decisions

- Multi-agent remediation plan finalized in `MASTER_PLAN.md`.
- Use `python-telegram-bot` 22.8 for its async polling API.
- Require per-user authentication for Jira access: users send their Jira Personal Access Token (PAT) via `/auth` in private chat; PATs are deleted from chat history and stored locally with copy-on-write and mode 0600 permissions.
- Basic authentication (passwords) and browser session cookies are rejected. Auth conversations expire after 3 minutes and warn on message deletion failure.
- Explicit human approval required before issue creation: previews present inline `[✅ Create Issue]` and `[❌ Cancel]` buttons; issues are created only upon user confirmation.
- Connect to Jira Server / Data Center REST API v2 using Bearer auth (PAT).
- Workflow, callback, attempt, attachment, and published issue state are persisted in a local-disk SQLite database using WAL mode outside the Git checkout.
- Callback data uses the strict schema `j1:<action>:<opaque_token>` containing 128 bits of randomness. Only SHA-256 token hashes are stored in SQLite.
- Ambiguous create timeouts transition drafts to `SUBMISSION_UNKNOWN` state. Automatic re-creation is forbidden and requires human reconciliation.
- Keep all server secrets in environment variables or an ignored `.env`/protected systemd environment file.
- Use NetworkManager L2TP/IPsec because the supplied VPN server setup cannot use WireGuard.
- Map the private Windows VPN profile only through the tracked placeholder `.nmconnection` template; never commit or quote the private XML or resulting profile.
- Log to journald without forwarded text, generated descriptions, tokens, provider error text, VPN endpoints, or credentials.
- Synchronize durable AI context through ordinary Git commits. Never store chat history or secrets as continuity data.

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

- 212/212 offline unit tests pass in 0.858s.
- Syntactical compilation and type consistency verified across domain, infrastructure persistence, and application service modules.
- Real Telegram, Gemini, Jira, VPN, systemd, and server behavior require fresh validation in the target environment.
- Authenticated sessions, credentials, server configuration, and external runtime state do not transfer through Git.

## Durable context model

- `PROJECT_CONTEXT.md`: stable facts and decisions.
- `CONTINUE_HERE.md`: authoritative detailed checkpoint and next action.
- `HANDOFF.md`: concise snapshot refreshed before switching.
- `AGENTS.md`: command and safety contract.
- `MASTER_PLAN.md`: multi-agent execution master plan and milestone exit criteria.
- `plan_antigravity.md`, `plan_codex.md`, `plan_grok.md`: domain-specific architecture, quality, and security proposals.
- `docs/architecture/`: system architecture contracts, layer dependency rules, and provider boundaries.
- `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md`: decision-grade workflow review.
