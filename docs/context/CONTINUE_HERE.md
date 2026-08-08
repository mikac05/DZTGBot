# Continue here

## Current status

The DZTGBot remediation program in `MASTER_PLAN.md` and subsequent Jira 8.4.1 UX Expansion phases are 100% complete and fully verified offline.

Implemented and offline-verified:

- frozen domain/FSM/callback/security contracts;
- SQLite WAL workflow, callback, attempt, attachment, card message tracker, unread notification tracker, and published-issue authority (schema v4);
- copy-on-write PAT storage with corruption recovery;
- pure workflow, intake, callback, connectivity, submission, attachment, card tracker, notification, issue triage, and recovery services;
- lifecycle-managed Jira and Gemini gateways with request-local credentials, dynamic workflow transitions, issue links (`Relates`), watchers, and bounded behavior;
- private-chat-only, PAT-only Telegram UI with dynamic auth-aware reply keyboard (`[🔑 連結 Jira]` vs `[📋 指派給我的] [🚩 我建的] / [🔍 搜尋] [📝 新建] / [🚪 Logout]`);
- compact paginated JQL search results (5 items/page) with 1-tap card preview buttons (`[1. PROJ-123]`);
- Universal Issue Card Action Bar (`[▶️ Start Dev]`, `[➡️ Move]`, `[📝 Edit]`, `[💬 Comment]`, `[⚠️ Block]`, `[👤 Assign]`, `[👁️ Watch]`, `[➕ Sub-task]`, `[Open in Jira ↗]`, `[🎨 Figma Spec ↗]`);
- Direct reply-to-card for instant comment & photo attachment uploads to Jira;
- PROD ticket forward auto-link hook (`Relates to`);
- Executive Daily Standup Summary Report (`/standup`) grouping tickets into Blocked, In Progress, In QA/Review, and Recently Done;
- Background Unread Notification Poller (`NotificationPollerService`) with 300s (5 min) interval;
- Figma URL auto-detection in description & dynamic `[🎨 Figma Spec ↗]` button placement;
- multi-language UI support (🇹🇼 繁體中文, 🇺🇸 English, 🇨🇳 简体中文) via `/start`, `/language`, and `[🌐 語言設置]`;
- mandatory Simplified Chinese (简体中文) enforcement for AI auto-generated issue summary & description;
- optional Basic Auth switch (`AUTH_PAT_ONLY=false`) with isolated modular compatibility;
- reorganized best-practice documentation hierarchy and master index (`docs/README.md`);
- composition-root cutover with no legacy workflow authority or raw unowned tasks;
- keyed processing, global/per-actor resource bounds, deadlines, retry budgets, cooldown recovery, and privacy-safe metrics;
- reproducible Ruff, strict mypy, branch coverage, unit, dependency, compilation, and Ubuntu CI gates;
- deployment/database runbooks, current architecture/migration records, credential threat model, audit reconciliation, and verification reports.

## Required verification on resume

- Full suite: **472 tests run; 471 passed and 1 Windows-only platform skip**.
- Ruff: passed (`All checks passed!`).
- Strict mypy: passed for all source files (`Success: no issues found in 35 source files`).
- `pip check`: passed.
- Source/test compilation: passed.
- Git-Bash `bash -n scripts/deploy.sh`: passed.
- FSM/callback/security branch gate: **91%** (required 90%).
- Repository/submission branch gate: **77%** (required 75%).
- Recovery/concurrency matrix: 44 tests repeated three times, all 132 executions passed.

## Exact next action

Keep the service pilot-only. On an approved Ubuntu 24.04 target, perform the supervised validation sequence in `docs/operations/end-to-end-test-plan.md` and `docs/operations/workflow-db-runbook.md`, beginning with ShellCheck/CI and deployment preflight before any Telegram, Gemini, Jira, VPN, or systemd action.

## Inputs still required from the user or target environment

- Ubuntu 24.04 CI/ShellCheck and a real deployment preflight.
- Protected workflow DB ownership/mode, backup/restore, disk-full, and restart drills on the target volume.
- Supervised Telegram polling and BotFather command verification.
- Supervised Gemini structured-output verification.
- Supervised Jira metadata, marker capability, create/update/attachment, ambiguous-outcome reconciliation, and concurrent human-edit verification.
- Console-supervised NetworkManager L2TP/IPsec full-tunnel and recovery testing.
- systemd lifecycle and journald privacy verification.

## Do not redo

- Do not repeat completed phases, feature implementations, or discovery.
- Do not reintroduce Telegram `user_data` workflow authority, unbound callbacks, blind Jira create retry, per-request shared credentials, or raw unowned tasks.
- Do not implement credential encryption without a separately approved root-managed key lifecycle and vetted AEAD/rotation/backup/rollback design.
- Do not run live external mutations or deploy without explicit authorization and target access.
