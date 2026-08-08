# DZTGBot handoff

## Current objective

The multi-agent remediation program in `MASTER_PLAN.md` is complete through Phase 9 in the current uncommitted working tree. The remaining objective is supervised Ubuntu 24.04 and live-service validation under separate authorization; the service remains pilot-only until that evidence exists.

Success for the completed offline program means domain isolation, explicit FSM transitions, cryptographic bound callbacks, SQLite WAL persistence, copy-on-write credentials, pure application services, failure-preserving Jira mutations, keyed bounded concurrency, privacy-safe observability, reproducible quality gates, truthful operations documentation, and aligned release evidence. Those gates are implemented and passing offline.

## Completed

- **Phases 0–3:** contracts, canonical domain/FSM/security policy, durable SQLite and credential persistence, and pure application services.
- **Phase 4:** lifecycle-managed Jira/Gemini gateways, canonical payloads, request-local PATs, durable attempts, ambiguous-outcome reconciliation, attachment retry, rules caching, provider boundaries, and strict configuration.
- **Phase 5:** thin HTML Telegram UI, strict bound callback keyboards, PAT-only private auth/admin, and non-authoritative legacy facades.
- **Phase 6:** composition-root cutover to SQLite-only workflow authority, deterministic startup/shutdown, integrated restart/recovery/concurrency/security matrices, strict current+1 aggregate CAS, and removal of legacy workflow keys/unbound callbacks/raw tasks.
- **Phase 7:** keyed workflow/collection processing, concurrency-one fallback, global/per-actor resource bounds, queue/deadline/retry/cooldown controls, and opaque privacy-safe metrics.
- **Phase 8:** pinned runtime/dev dependencies, Ruff, strict mypy, focused branch coverage, Ubuntu 24.04 CI, deployment/database hardening, current architecture/migration records, workflow DB runbook, credential threat model, and full audit reconciliation.
- **Phase 9:** architecture, performance/recovery, and security release-verification reports with matching evidence identity and external validation boundary.

Key changed areas include `src/dztgbot/domain/`, `src/dztgbot/services/`, `src/dztgbot/infrastructure/`, `src/dztgbot/ui/`, `src/dztgbot/__main__.py`, `src/dztgbot/config.py`, auth/admin/facades, deployment assets, quality configuration, operations/security/architecture documentation, and comprehensive offline tests.

## Decisions

- First safe release remains private-chat-only and PAT-only.
- SQLite on a protected local runtime filesystem is the sole workflow/callback/attempt/attachment/published authority.
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

Keep the service pilot-only. With explicit target-environment authorization, execute the supervised sequence in `docs/end-to-end-test-plan.md` and `docs/operations/workflow-db-runbook.md`, beginning with Ubuntu 24.04 CI/ShellCheck and deployment preflight before external API or VPN actions.

If cross-account/device synchronization is desired first, explicitly issue `DZTGBot handoff`. This run did not commit or push.

## Verification

- Full offline suite: **449 tests run; 448 passed, 1 Windows-only platform skip**.
- Ruff, strict mypy (29 gated files), `pip check`, compilation, Git-Bash deploy syntax, and diff checks passed.
- Focused branch coverage: **91%** for FSM/callback/security and **77%** for repository/submission.
- Recovery/concurrency/resource matrix: 44 tests repeated three times, all 132 executions passed.
- Phase 9 reports agree on base HEAD `03499594a4f8975ae046fa513c9aada7e1c836b6`, uncommitted remediation state, environment, counts, skip, residual risk, and external evidence boundary.

## Git snapshot metadata

<!-- HANDOFF-METADATA:START -->
- Generated UTC: `2026-08-08T05:11:04Z`
- Branch: `main`
- Upstream: `origin/main`
- Base commit before this handoff: `03499594a4f8`
- Working-tree entries before metadata refresh: `77`
- The handoff commit is the commit containing this file.
<!-- HANDOFF-METADATA:END -->
