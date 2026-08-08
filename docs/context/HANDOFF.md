# DZTGBot handoff

## Current objective

The complete remediation program in `MASTER_PLAN.md` and subsequent Jira 8.4.1 UX Expansion phases are 100% finished and verified offline. The remaining objective is supervised Ubuntu 24.04 target deployment and live-service validation under separate authorization; the service remains pilot-only until target environment evidence exists.

Success for the completed program includes clean domain isolation, explicit FSM transitions, cryptographic bound callbacks, SQLite WAL persistence (schema v4), copy-on-write credentials, pure application services, failure-preserving Jira mutations, dynamic workflow transitions, issue links, watchers, auto-detected Figma URLs, compact paginated search, daily standup summary reports, background unread notifications, privacy-safe observability, reproducible quality gates, truthful operations documentation, and aligned release evidence.

## Completed

- **Phases 0–9 Remediation:** Domain contracts, SQLite WAL workflow authority, pure application services, lifecycle-managed gateways, thin Telegram UI, composition-root cutover, keyed concurrency, quality gates, and Phase 9 verification reports.
- **Jira 8.4.1 UX & Triage Expansion:**
  - Dynamic auth-aware main reply keyboard (`[🔑 連結 Jira]` vs `[📋 指派給我的] [🚩 我建的] / [🔍 搜尋] [📝 新建] / [🚪 Logout]`).
  - Compact paginated JQL search result list (5 items per page) with 1-tap card preview buttons (`[1. PROJ-123]`).
  - Universal Issue Card Action Bar: Dynamic Primary Transition (`[▶️ Start Dev]`), `[➡️ Move]`, `[📝 Edit]`, `[💬 Comment]`, `[⚠️ Block]`, `[👤 Assign]`, `[👁️ Watch]`, `[➕ Sub-task]`, `[Open in Jira ↗]`, and auto-detected `[🎨 Figma Spec ↗]`.
  - Direct Reply to Issue Card for instant Jira text comment and photo attachment uploads.
  - Forward `PROD-xxx` key auto-detection -> auto-targets `BOT` project & executes `POST /issueLink` (`Relates to`).
  - Background Unread Notification Poller (`NotificationPollerService`) running every 300s (5 min) with SQLite schema v4 tracker.
  - Executive Daily Standup Summary Report (`/standup`) grouping tickets into Blocked, In Progress, In QA/Review, and Recently Done.
  - Zero-effort Figma URL auto-detection from description & dynamic `[🎨 Figma Spec ↗]` button placement.

## Decisions

- First safe release remains private-chat-only and PAT-only.
- SQLite on a protected local runtime filesystem is the sole workflow/callback/attempt/attachment/published authority (schema v4).
- Existing aggregates update only through atomic current-revision-plus-one CAS; same, stale, skipped, or concurrent-loser revisions fail closed.
- Jira create unknown outcomes never blind-retry and require positive reconciliation.
- Callback possession alone is insufficient; actor, chat/thread, preview message, action, state, revision, expiry, and one-shot status are checked.
- Keyed concurrency is optional and bounded; validated concurrency one remains the fallback.
- Credential encryption remains explicitly deferred pending a separately approved external key lifecycle and vetted AEAD/rotation/backup/rollback design.
- Automated checks remain offline and never mutate real Telegram, Gemini, Jira, VPN, systemd, or server state.

## Open items

- ShellCheck/Ubuntu 24.04 CI has not run on this Windows host, although Git-Bash syntax validation passed.
- Jira Server/Data Center support for the stable create marker must be confirmed before live use.
- Target-host DB permissions, migration, backup/restore, disk-full, corruption, and restart behavior require supervised validation.
- Telegram, Gemini, Jira create/update/attachment/reconciliation, NetworkManager L2TP/IPsec, systemd, and journald privacy remain externally unverified.
- Group workflows, multimodal Gemini media bytes, horizontal deployment, and credential encryption remain explicitly deferred.

## Exact next action

Keep the service pilot-only. With explicit target-environment authorization, execute the supervised sequence in `docs/operations/end-to-end-test-plan.md` and `docs/operations/workflow-db-runbook.md`, beginning with Ubuntu 24.04 CI/ShellCheck and deployment preflight before external API or VPN actions.

## Verification

- Full offline suite: **466 tests run; 465 passed, 1 Windows-only platform skip**.
- Ruff: passed (`All checks passed!`).
- Strict mypy: passed for all source files (`Success: no issues found in 34 source files`).
- `pip check`, compilation, Git-Bash deploy syntax, and diff checks passed.
- Focused branch coverage: **91%** for FSM/callback/security and **77%** for repository/submission.
- Recovery/concurrency/resource matrix: 44 tests repeated three times, all 132 executions passed.

## Git snapshot metadata

<!-- HANDOFF-METADATA:START -->
- Generated UTC: `2026-08-08T16:32:07Z`
- Branch: `main`
- Upstream: `origin/main`
- Base commit before this handoff: `41f89d7c8187`
- Working-tree entries before metadata refresh: `5`
- The handoff commit is the commit containing this file.
<!-- HANDOFF-METADATA:END -->
