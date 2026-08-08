# DZTGBot

Python 3.12 Telegram bot for private-chat Jira task drafting on a single Ubuntu 24.04 host. It accepts direct forwards, replies to forwards, and manual drafts; asks Gemini for a structured Jira task template; presents a human-reviewable preview with cryptographic callback tokens; and creates or updates issues in a self-hosted Jira Server / Data Center instance only after explicit human confirmation.

The Telegram client is `python-telegram-bot` 22.8 (async long polling). Gemini uses Google's `google-genai` SDK with local Pydantic validation and automatic free-tier model fallback. Jira uses REST API v2 with per-user Bearer PATs over a shared `httpx` client. Durable workflow state lives in a local-disk SQLite WAL database.

## Product boundaries (first release)

| Boundary | Enforcement |
| --- | --- |
| Private chat only | Drafts, callbacks, Jira mutations, auth, and admin commands refuse non-private chats |
| PAT-only authentication | Passwords, Basic auth, and session cookies are rejected |
| Human confirmation | Jira create runs only after the user presses the confirm control on a review preview |
| No automatic full-tunnel VPN start | Installer never brings the tunnel up; optional `/vpnstart` requires `VPN_ALLOW_START=true` and narrow sudoers |
| Unsupported non-photo media | Photos may be attached after create; documents/audio/video/voice are not multimodal-analyzed and are not uploaded as Jira attachments |
| Offline automation never mutates Jira | Unit tests, CI, and deploy preflight use fakes/local checks only |

## What the bot does

### Intake and analysis

- Direct Telegram forwards and messages that reply directly to a forward.
- Manual drafts via `/new` (quick title or guided flow).
- Short multi-forward batching scoped by actor + chat (+ optional thread), with resource bounds.
- Gemini structured templates using runtime Jira rules; model IDs are chosen by the application (no `GEMINI_MODEL` setting).
- Review preview rendered as HTML with durable draft identity in SQLite.

### Authentication

- `/auth` collects a Jira Personal Access Token in private chat only.
- Conversation is time-bounded (`AUTH_TTL_SECONDS`, default 180).
- Credential messages are deleted when Telegram permits; deletion failure is warned.
- PATs are stored in a service-owned `0600` JSON file with copy-on-write and corruption quarantine (`UserStore`).
- `/logout` removes the local copy only; it does not revoke the token at Jira.
- Optional defence-in-depth allowlist: `TELEGRAM_ALLOWED_USER_IDS`.

### Create, update, attachments, reconciliation

- Confirm / cancel / toggle type / toggle priority / edit on review cards.
- Callback data uses `j1:<action>:<opaque_token>`; only SHA-256 token hashes are stored.
- Create and update paths claim durable submission attempts before calling Jira.
- Ambiguous create timeouts enter `SUBMISSION_UNKNOWN` and require human reconciliation before any retry (no blind re-create).
- Retryable failures preserve the draft and expose retry/cancel controls.
- Published-issue edit uses stored published metadata and template diffs; unknown update outcomes require reconciliation.
- Photo attachments upload after create with dedupe/status tracking; partial attachment failure is a first-class state.
- Keyed concurrency (`KeyedProcessor`) serializes work per workflow or collection key when `TELEGRAM_CONCURRENT_UPDATES > 1`.

### Admin

Private-chat administrators listed in `TELEGRAM_ADMIN_USER_IDS`:

- `/rules` — view runtime rules
- `/setrules` — atomic replace with previous-version retention
- `/vpn` — read-only NetworkManager L2TP/IPsec status
- `/vpnstart` — optional narrowly authorized start of one named connection

### Media capability boundary

| Media | Analysis input | Jira attachment |
| --- | --- | --- |
| Text / caption | Yes | N/A |
| Photo | Caption/text + photo label; bytes not sent to Gemini | Yes, after successful create (size/count bounds) |
| Document / video / voice / audio | Media-type label only; not multimodal | No — unsupported for upload in this release |

Users should not assume captionless images or non-photo files become complete Jira descriptions.

## Architecture snapshot

- Composition root: `src/dztgbot/__main__.py`
- Domain FSM, callbacks, policy, ports: `src/dztgbot/domain/`
- Application services: `src/dztgbot/services/`
- Gateways + SQLite + keyed processor: `src/dztgbot/infrastructure/`
- Telegram UI: `src/dztgbot/ui/`
- Auth facade: `src/dztgbot/jira_auth.py`
- Admin / VPN: `src/dztgbot/admin.py`, `src/dztgbot/vpn.py`
- Settings: `src/dztgbot/config.py` (environment / `.env` only)

Durable contracts: `docs/architecture/current-architecture.md`, `docs/architecture/workflow-contracts.md`, `docs/architecture/dependency-rules.md`.

## Configuration surface

Tracked template: `.env.example`. Production uses a **root-owned `0600` environment file outside the checkout**, loaded by systemd before privilege drop.

### Required for production start

| Variable | Role |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `GEMINI_API_KEY` | Gemini API key |
| `TELEGRAM_ADMIN_USER_IDS` | Comma-separated numeric admin IDs |
| `JIRA_RULES_PATH` | Runtime rules file (production: under `/var/lib/dztgbot`) |
| `JIRA_URL` | HTTPS Jira base URL (no credentials/query/fragment) |
| `WORKFLOW_DB_PATH` | Absolute local SQLite path outside checkout/sync storage (production: `/var/lib/dztgbot/workflow.sqlite3`) |

### Important optional / bounded settings

`GEMINI_TIMEOUT_SECONDS`, `TELEGRAM_CONCURRENT_UPDATES`, `TELEGRAM_ALLOWED_USER_IDS`, `JIRA_VERIFY_SSL`, `JIRA_CA_BUNDLE_PATH`, `JIRA_DEFAULT_PROJECT_KEY`, `USER_CREDENTIALS_PATH`, `AUTH_TTL_SECONDS`, `AUTH_PAT_ONLY` (must remain true), `PRIVATE_CHAT_ONLY` (must remain true), `MAX_BATCH_MESSAGES`, `MAX_MESSAGE_CHARACTERS`, `MAX_PROMPT_CHARACTERS`, `MAX_ATTACHMENT_BYTES`, `MAX_ATTACHMENT_COUNT`, `MAX_QUEUE_SIZE`, `MAX_CONCURRENT_GEMINI`, `MAX_CONCURRENT_JIRA`, VPN keys, `LOG_LEVEL`.

**Not used:** `GEMINI_MODEL`. Model selection and free-tier fallback are application-managed. Deploy and startup must not require it.

## Cross-account and cross-device continuity

Portable AI handoff lives under `docs/context/`, governed by `AGENTS.md`. It transfers project facts and next actions—never credentials or authenticated sessions.

```text
DZTGBot handoff
DZTGBot continue
```

Google Antigravity workflows: `/dztgbot-handoff`, `/dztgbot-continue`.

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env
mkdir -p var
cp config/jira_rules.example.txt var/jira_rules.txt
# Set TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, TELEGRAM_ADMIN_USER_IDS, JIRA_URL,
# and an absolute WORKFLOW_DB_PATH on local disk outside the checkout (not OneDrive).
PYTHONPATH=src python -m dztgbot
```

Replace `TODO_...` values only in ignored local `.env`. Never place real credentials in Git, commands, logs, screenshots, or documentation.

### Offline tests (runtime deps)

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

### Quality gates (development / CI deps)

```bash
python -m pip install -r requirements-dev.txt
python -m pip check
python -m compileall -q src tests
python -m ruff check src/dztgbot/domain src/dztgbot/services src/dztgbot/infrastructure src/dztgbot/ui src/dztgbot/__main__.py
python -m mypy
python -m coverage run -m unittest discover -s tests -v
python -m coverage report --fail-under=90 --include='src/dztgbot/domain/fsm.py,src/dztgbot/domain/callbacks.py,src/dztgbot/domain/policy.py,src/dztgbot/services/callback_service.py'
python -m coverage report --fail-under=75 --include='src/dztgbot/infrastructure/persistence/workflow_sqlite.py,src/dztgbot/services/submission_service.py'
# ShellCheck on Ubuntu/CI only:
shellcheck scripts/deploy.sh
```

CI is defined in `.github/workflows/quality.yml` (Ubuntu 24.04, Python 3.12, `requirements-dev.txt`). It never performs real Jira, Telegram, Gemini, VPN, or systemd mutations.

## Automated target-server deployment

`scripts/deploy.sh` supports **Ubuntu 24.04 only** with Python 3.12. It refuses every other distribution or Ubuntu release. It does **not** open firewall ports, enable NetworkManager, load a VPN profile into the kernel path beyond package install, or start the full VPN tunnel.

```bash
sudo \
  DZTGBOT_SERVICE_USER=TODO_REPLACE_WITH_SERVICE_USER \
  DZTGBOT_ENV_FILE=/TODO_REPLACE_WITH_ABSOLUTE_ENVIRONMENT_FILE \
  bash scripts/deploy.sh
```

On the first run, if the environment file is missing, the script creates a root-owned `0600` placeholder from `.env.example` and exits. Edit with `sudoedit`, set at least:

- all required tokens/keys/IDs
- `JIRA_URL`
- `JIRA_RULES_PATH=/var/lib/dztgbot/jira_rules.txt`
- `WORKFLOW_DB_PATH=/var/lib/dztgbot/workflow.sqlite3`

Then rerun the same command.

### Deploy sequence

1. Validate Ubuntu 24.04 and the root-owned `0600` environment file (values never printed).
2. Validate required settings, including `WORKFLOW_DB_PATH` under `/var/lib/dztgbot` on non-synced local paths.
3. Create or validate a dedicated non-login service account.
4. Stop the unit if it is already active (deterministic maintenance window).
5. Build `.venv` from **`requirements.txt` only**, compile sources, run **offline unit tests** (no live providers).
6. Prepare `/var/lib/dztgbot` (mode `0700`), rules file (`0600`), disk-space and writability checks.
7. Prepare workflow DB: ownership/mode `0600`, integrity check when present, SQLite backup under `/var/lib/dztgbot/backups/`, migration preflight as the service user.
8. Install VPN client packages only when `VPN_ENABLED=true` (still no automatic tunnel start).
9. Generate exact VPN sudoers rules only when `VPN_ALLOW_START=true`.
10. Render and verify the hardened systemd unit; enable and start/restart with bounded active wait.

Set `DZTGBOT_INSTALL_SYSTEM_PACKAGES=false` only when an administrator has already installed and verified every required OS package.

Operator runbook for the workflow database: [`docs/operations/workflow-db-runbook.md`](docs/operations/workflow-db-runbook.md).
Credential threat model: [`docs/security/credential-threat-model.md`](docs/security/credential-threat-model.md).
Supervised live plan: [`docs/end-to-end-test-plan.md`](docs/end-to-end-test-plan.md).

## Secrets and runtime state

| Asset | Location | Mode / ownership |
| --- | --- | --- |
| Environment file | Outside checkout; path chosen by operator | root `0600` |
| Workflow SQLite (+ WAL/SHM) | `WORKFLOW_DB_PATH` under `/var/lib/dztgbot` | service user `0600` |
| Workflow backups | `/var/lib/dztgbot/backups/` | service user `0700` dir / `0600` files |
| Rules | `JIRA_RULES_PATH` | service user `0600` |
| User PATs JSON | `USER_CREDENTIALS_PATH` (default beside rules) | service user `0600` |
| VPN profile | Outside checkout | root `0600` |

At-rest encryption of PAT fields is **explicitly deferred** until a separately approved root-managed key lifecycle, vetted AEAD format, rotation, backup recovery, and rollback design exists. Host confinement (`0600` + non-root service + hardened unit) is the selected boundary today. Details: `docs/security/credential-threat-model.md`.

## L2TP/IPsec compatibility

The private `src/ref/vpnsettings.xml` remains ignored by Git. Only non-secret compatibility characteristics are documented:

| Windows profile setting | Linux mapping |
| --- | --- |
| L2TP tunnel | NetworkManager L2TP plugin |
| IPsec PSK authentication | Private `ipsec-psk` value |
| MS-CHAPv2 | MS-CHAPv2 enabled; weaker alternatives refused |
| Optional PPP encryption | `require-mppe=no`, pending supervised compatibility confirmation |
| Split tunneling disabled | Full tunnel via `never-default=false` |

Tracked template: `config/l2tp-ipsec.example.nmconnection` (placeholders only). Keep `VPN_ALLOW_START=false` until console-supervised testing proves SSH recovery and Telegram/Gemini routing. Stopping the bot does not disconnect an already-active tunnel.

## Service operations

```bash
sudo systemctl status dztgbot.service --no-pager
sudo systemctl restart dztgbot.service
sudo systemctl stop dztgbot.service
sudo systemctl disable dztgbot.service
sudo journalctl -u dztgbot.service -f
```

Logs must never contain tokens, keys, forwarded text, generated descriptions, VPN endpoints, or credentials.

## Audit improvement map (2026-08-07 review)

Cross-map of the 17 ranked improvements and major workflow/UX weaknesses from [`docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md`](docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md). Status is relative to the current codebase; live Telegram/Jira/VPN/systemd behaviour still requires supervised target-environment verification (Phase 9).

| # | Improvement | Status |
| --- | --- | --- |
| 1 | Durable uniquely identified drafts + callback binding | **Implemented** — SQLite drafts, `j1:` tokens bound to draft/revision/chat/owner |
| 2 | Explicit workflow FSM | **Implemented** — `domain/fsm.py` full lifecycle including unknown/retry/attach states |
| 3 | Mutation recovery and idempotency | **Implemented** — attempt claims, `SUBMISSION_UNKNOWN` / update unknown, reconcile before retry |
| 4 | Telegram concurrency and task lifecycle | **Implemented** — `KeyedProcessor` + scheduler; graceful reverse-order shutdown |
| 5 | Private, time-bounded, PAT-only auth | **Implemented** — private + TTL + PAT-only + delete warning; project create-permission preflight still limited |
| 6 | Live Jira metadata discovery | **Partially deferred** — gateway metadata cache exists; full required-field/permission preflight not claimed complete |
| 7 | Multimodal honesty | **Implemented boundary** — photo attach path; non-photo upload/analysis explicitly unsupported and documented |
| 8 | Telegram interaction polish | **Partially implemented** — HTML cards, private-only, keyboards; some UX polish still deferred |
| 9 | Quotas / backpressure | **Implemented** — resource limiter, queue bounds, concurrent Gemini/Jira caps |
| 10 | Transactional store + stronger secrets | **Implemented store** — SQLite WAL + migrations + backups in deploy; **encryption deferred** (threat model) |
| 11 | Diff-based published updates | **Implemented path** — template diff + published metadata; supervised live conflict cases remain |
| 12 | Production observability | **Partial** — privacy-safe metrics counters; no external alerting stack |
| 13 | Handler integration / E2E tests | **Offline implemented** — large unit/integration suite; supervised live E2E plan separate |
| 14 | Split `core.py` responsibilities | **Implemented** — services/ui/gateways; `core.py` non-authoritative facade |
| 15 | Reconcile operational documentation | **This task (P8-G)** — README, deploy, E2E, runbooks, threat model |
| 16 | Admin change governance | **Partial** — private-only admin + atomic rules; no multi-party approval/diff UI |
| 17 | Privacy / retention / support paths | **Partial** — retention helpers and logging policy; product support channel not shipped |

### Major weakness themes

| Theme | Status |
| --- | --- |
| Stale callback / cross-chat draft collision | Mitigated by durable IDs + token authorization |
| Create timeout duplicates | Mitigated by unknown state + reconciliation |
| Password/cookie auth surface | Removed (PAT-only) |
| Group admin/rules disclosure | Mitigated (private-only admin/workflows) |
| In-memory-only drafts across restart | Mitigated (SQLite WAL) |
| `GEMINI_MODEL` / “Jira not implemented” docs drift | Corrected in this documentation pass |
| Live multi-user production proof | **Not claimed** — supervised verification still required |

## Production verification

Follow [`docs/end-to-end-test-plan.md`](docs/end-to-end-test-plan.md). A real Telegram exchange requires a developer-owned bot token; Gemini requires a private API key; Jira mutations require human confirmation in a supervised window and must never run inside CI. Untouched placeholders are intentionally rejected.
