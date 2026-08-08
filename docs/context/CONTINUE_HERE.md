# Continue here

## Current status

The DZTGBot remediation program in `MASTER_PLAN.md` is complete through Phase 9 in the current uncommitted working tree layered on base `HEAD` `03499594a4f8975ae046fa513c9aada7e1c836b6`.

Implemented and offline-verified:

- frozen domain/FSM/callback/security contracts;
- SQLite WAL workflow, callback, attempt, attachment, and published-issue authority;
- copy-on-write PAT storage with corruption recovery;
- pure workflow, intake, callback, connectivity, submission, attachment, and recovery services;
- lifecycle-managed Jira and Gemini gateways with request-local credentials and bounded behavior;
- private-chat-only, PAT-only Telegram UI/auth/admin flows with strict bound callbacks;
- composition-root cutover with no legacy workflow authority or raw unowned tasks;
- keyed processing, global/per-actor resource bounds, deadlines, retry budgets, cooldown recovery, and privacy-safe metrics;
- reproducible Ruff, strict mypy, branch coverage, unit, dependency, compilation, and Ubuntu CI gates;
- deployment/database runbooks, current architecture/migration records, credential threat model, audit reconciliation, and three Phase 9 verification reports.

## Required verification on resume

- Full suite: **449 tests run; 448 passed and 1 Windows-only platform skip**.
- Ruff: passed.
- Strict mypy: passed for 29 gated source files.
- `pip check`: passed.
- Source/test compilation: passed.
- Git-Bash `bash -n scripts/deploy.sh`: passed.
- FSM/callback/security branch gate: **91%** (required 90%).
- Repository/submission branch gate: **77%** (required 75%).
- Recovery/concurrency matrix: 44 tests repeated three times, all 132 executions passed.
- No `expectedFailure`, legacy workflow-authority keys, unbound production callback prefix, or raw `asyncio.create_task` remains in application source.

Phase 9 reports:

- `docs/reviews/architecture-remediation-verification.md`
- `docs/reviews/performance-recovery-verification.md`
- `docs/reviews/security-release-verification.md`

All three use the same base HEAD, uncommitted-tree identity, date, environment, test count, skip, residual risks, and external evidence boundary.

## Exact next action

Keep the service pilot-only. On an approved Ubuntu 24.04 target, perform the supervised validation sequence in `docs/end-to-end-test-plan.md` and `docs/operations/workflow-db-runbook.md`, beginning with ShellCheck/CI and deployment preflight before any Telegram, Gemini, Jira, VPN, or systemd action.

If the user wants to Git-synchronize this completed offline state first, they must explicitly issue `DZTGBot handoff`; this continuation did not commit or push.

## Inputs still required from the user or target environment

- Ubuntu 24.04 CI/ShellCheck and a real deployment preflight.
- Protected workflow DB ownership/mode, backup/restore, disk-full, and restart drills on the target volume.
- Supervised Telegram polling and BotFather command verification.
- Supervised Gemini structured-output verification.
- Supervised Jira metadata, marker capability, create/update/attachment, ambiguous-outcome reconciliation, and concurrent human-edit verification.
- Console-supervised NetworkManager L2TP/IPsec full-tunnel and recovery testing.
- systemd lifecycle and journald privacy verification.

No external operation should be inferred from the completed offline program.

## Do not redo

- Do not repeat Phases 0–9 implementation or discovery.
- Do not reintroduce Telegram `user_data` workflow authority, unbound callbacks, blind Jira create retry, per-request shared credentials, or raw unowned tasks.
- Do not implement credential encryption without a separately approved root-managed key lifecycle and vetted AEAD/rotation/backup/rollback design.
- Do not run live external mutations or deploy without explicit authorization and target access.
