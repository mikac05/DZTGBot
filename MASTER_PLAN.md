# DZTGBot definitive remediation master plan

Date: 2026-08-07  
Role: Lead Architect synthesis  
Inputs: `plan_antigravity.md`, `plan_codex.md`, `plan_grok.md`, all cross-audit sections, the 2026-08-07 end-to-end audit, and the current repository architecture.

## 1. Objective and success boundary

Move DZTGBot from a trusted serial-use pilot to a production-safe single-host service for approved users, without adding unrelated product features.

The program succeeds when workflow identity, authorization, persistence, concurrency, Jira mutation recovery, credential handling, resource bounds, observability, and tests enforce the invariants in this document. Human confirmation before Jira creation, lazy VPN operation, Ubuntu 24.04 deployment, and the prohibition on real external mutations in automated tests remain mandatory.

This plan does not authorize source-code changes by itself. Each phase still requires the normal implementation approval and verification. Live Telegram, Gemini, Jira, VPN, systemd, or server work requires separate explicit authorization and target-environment access.

## 2. Definitive architectural decisions

These decisions resolve the disagreements across the three proposals.

1. **Initial chat scope:** authentication, drafts, callbacks, Jira mutations, and administrator operations will be private-chat-only for the first safe release. Domain entities still retain `chat_id` and optional `message_thread_id` so a separately approved group workflow can be added later without redesigning identity.
2. **Authentication:** Jira Personal Access Tokens only. Password, Basic-auth, and session-cookie inputs are rejected. Auth conversations expire after three minutes and warn the user when Telegram cannot delete the credential message.
3. **Workflow persistence:** SQLite is mandatory for production workflow, callback, attempt, attachment, and published-issue state. It must reside on a local protected runtime filesystem, never in the Git checkout, OneDrive, or another synchronized/network folder. An in-memory repository is test-only.
4. **Credential persistence:** immediately retain the protected `0600` file boundary but fix schema validation, corruption recovery, permissions, and copy-on-write consistency. Application-layer encryption is not an early blocker; Grok owns a later threat-model/key-lifecycle decision. No home-grown cryptography is permitted.
5. **Domain model:** frozen standard-library domain types and explicit factories/transitions. Strict Pydantic DTOs validate Gemini, Jira, configuration, and persistence boundaries, then map explicitly into domain types. Preserve current field meaning, including `issuetype` and `acceptance_criteria: list[str]`.
6. **Callback design:** callback data is `j1:<short-action>:<opaque-token>`. The token contains at least 128 bits of cryptographic randomness; only its hash is stored. It resolves to workflow, owner, chat/thread, preview message, expected revision/state, action, expiry, and one-shot status. Possession of the token is never sufficient authorization.
7. **Mutation certainty:** a Jira create timeout, disconnect after dispatch, process loss, or malformed success response becomes `SUBMISSION_UNKNOWN`. It is not retryable, ordinarily cancellable, or automatically expirable. Another create is forbidden until reconciliation proves the first did not succeed.
8. **Jira idempotency:** persist the attempt before I/O and include an approved stable marker in the create request. A Jira search may prove an issue exists, but an immediate negative JQL result never proves absence because indexing can lag. If the deployed Jira cannot support reliable reconciliation, the workflow remains supervised and unresolved rather than risking a duplicate.
9. **Concurrency:** temporarily set stateful processing to one update at a time during cutover. The target is transactionally enforced revision compare-and-swap plus keyed serialization by workflow. Independent slow external I/O may run concurrently only within explicit global/per-actor limits.
10. **Task lifecycle:** no raw unowned `asyncio.create_task`. Short batching deadlines use application-owned cancellable tasks with an injectable clock. PTB `JobQueue` is not added unless its optional dependency and shutdown behavior are separately approved.
11. **Network lifecycle:** one process-lifecycle `httpx.AsyncClient` for Jira, with per-request auth headers, bounded pools, bounded response extraction, and separate timeout categories. Tests must prove credentials never cross requests.
12. **Media scope:** the first release is capability-honest. Unsupported or content-empty media is rejected or marked attachment-only. Sending media bytes to Gemini is deferred to a separate privacy, size, and cost review.
13. **Quality rollout:** characterize first, then add strict typing/linting and high branch coverage to new workflow/security/orchestration modules. Do not block the first safety fixes on whole-repository strictness or a cosmetic file-size limit.
14. **External tests:** CI and deployment preflight use fakes only and never create or update real Jira issues. Supervised live validation is a later human-authorized release gate.

## 3. Non-negotiable invariants

- A callback affects exactly the workflow and revision rendered by its bound preview.
- Actor, private chat, optional thread, preview message, action, state, revision, expiry, and one-shot status are verified before mutation.
- Two drafts, chats, batches, users, or published issues never share state or attachments.
- Every legal transition is explicit, atomic, and revision-checked; illegal or replayed transitions fail closed with safe feedback.
- A draft and submission attempt are persisted before Jira create dispatch.
- A definite failure retains retry context; an unknown outcome can never dispatch another create without reconciliation.
- `SUBMISSION_UNKNOWN` records are not deleted by ordinary expiry/retention jobs.
- No Telegram, Gemini, Jira, VPN, or file-download await occurs while a workflow database transaction or keyed state lock is held.
- Credential memory and disk agree after every successful or failed store/remove operation.
- Only a workflow in an attachment-accepting state can receive a photo, and batch limits are checked before attachment mutation.
- All queues, prompts, retries, response/error bodies, files, retained state, and concurrency have configured bounds.
- Create, update, preview, validation, and diff use one canonical field mapping; approved Jira fields are never silently truncated or substituted.
- Logs and metrics contain opaque correlation IDs and fixed event/outcome codes, not message text, provider errors, tokens, callback tokens, Telegram file IDs, raw Telegram user IDs, VPN details, or credential-bearing URLs.
- Shutdown completes owned work within a deadline or leaves a durable state that is safe to resume or reconcile.

## 4. Target architecture

```text
src/dztgbot/
  domain/                 # frozen entities, policies, transitions, ports
  services/               # workflow, intake, callbacks, submission, attachments
  infrastructure/         # SQLite, Jira/Gemini adapters, keyed processing
  ui/                     # Telegram renderers, keyboards, thin handlers
  config.py               # validated runtime configuration
  __main__.py             # composition root and lifecycle only
```

Dependency direction is `ui -> services -> domain`, with `infrastructure` implementing domain/service ports. Domain and services must not import `telegram` or `telegram.ext`. Provider SDK objects and exceptions stop at infrastructure adapters.

Migration is incremental. Existing modules remain compatibility facades until the integration phase; there will not be a big-bang directory move or a long-lived dual source of truth.

## 5. Multi-agent file-ownership rules

1. Each path below has one exclusive owner for the entire program. Another agent may review it but must not edit it.
2. Agents import frozen interfaces from another owner's files; requested interface changes are sent to that owner.
3. Only Antigravity edits composition/export files such as `__main__.py`, `core.py`, and package `__init__.py` files.
4. Only Codex edits workflow persistence, provider gateways, recovery/performance services, and quality-tool configuration.
5. Only Grok edits auth/security/config/deployment/user-facing operational documentation.
6. `MASTER_PLAN.md` remains Lead-Architect-owned. Implementation agents do not revise it while executing tasks.
7. Work inside a phase may run in parallel only after its stated interface dependencies are frozen.

### Exclusive ownership map

| Owner | Exclusive areas |
|---|---|
| Antigravity | `src/dztgbot/domain/models.py`, `domain/ports.py`, all package `__init__.py` files, `services/workflow_service.py`, `services/connectivity_service.py`, `src/dztgbot/ui/**`, `src/dztgbot/core.py`, `src/dztgbot/__main__.py`, `src/dztgbot/vpn.py`, `docs/architecture/**` |
| Codex | `domain/fsm.py`, `domain/errors.py`, `infrastructure/persistence/**`, `infrastructure/jira_gateway.py`, `infrastructure/gemini_gateway.py`, `infrastructure/keyed_processor.py`, `services/intake_service.py`, `services/submission_service.py`, `services/attachment_service.py`, `services/limits.py`, `services/observability.py`, current `analysis.py`, `jira_client.py`, `rules.py`, quality configuration, Codex-owned tests/reports |
| Grok | `domain/callbacks.py`, `domain/policy.py`, `services/callback_service.py`, current `jira_auth.py`, `admin.py`, `user_store.py`, `config.py`, `.env.example`, deployment assets/scripts, user-facing docs, Grok-owned tests/reports |

## 6. Milestone overview

| Phase | Milestone | Parallel owners | Exit gate |
|---|---|---|---|
| 0 | Safety baseline and contracts | All three | Existing behavior characterized; known defects have explicit target tests; no source behavior changed |
| 1 | Domain, FSM, callback, and policy foundation | All three after model freeze | Legal transition matrix, strict callback grammar, typed errors, and policy tests pass |
| 2 | Durable workflow and safe credential persistence | Codex + Grok | Restart, conflict, corruption, disk-failure, and copy-on-write tests pass |
| 3 | Pure application services | All three | Intake, workflow, callback authorization, and no-I/O-in-lock invariants pass without Telegram imports |
| 4 | Provider gateways and mutation recovery | Codex + Antigravity + Grok config | Shared transport, strict DTOs, unknown-outcome recovery, attachment retry, and credential-isolation tests pass |
| 5 | Telegram UI, auth, and admin cutover modules | Antigravity + Grok + Codex facades | Private-only controls, bound callbacks, correct rendering, and handler journeys pass |
| 6 | Composition-root cutover | Antigravity, then Codex/Grok verification | SQLite is sole workflow authority; no legacy latest-state mutation path or unowned task remains |
| 7 | Keyed concurrency, bounds, and observability | Codex, then Antigravity wiring and Grok privacy gate | Curated concurrency/fault matrix passes under configured limits |
| 8 | Quality, deployment, documentation, and credential threat model | All three | Reproducible Ubuntu checks, truthful docs, migration/rollback plan, and security decisions complete |
| 9 | Release verification | All three | Offline evidence recorded; any live validation separately authorized and honestly reported |

## 7. Detailed phased tasks

## Phase 0 — Safety baseline and frozen contracts

### Task P0-A — Architecture and interface contract

**Goal & Requirements**

- Document dependency direction, aggregate boundaries, callback lifecycle, state ownership, and the incremental cutover path.
- Specify ports without importing Telegram/provider types.
- Record private-only first-release scope, local-disk SQLite boundary, human confirmation, lazy VPN, and no-real-external-CI rules.
- Avoid personal machine paths and `file://` references.

**Target Files**

- `docs/architecture/workflow-contracts.md`
- `docs/architecture/dependency-rules.md`

**Assigned Agent:** Antigravity

### Task P0-C — Characterization and known-defect harness

**Goal & Requirements**

- Add deterministic Telegram/context/provider fakes and a fake clock.
- Characterize behavior that must survive refactoring.
- Add target-invariant regression cases for ordinary-photo contamination, the 21st-message attachment leak, reply-keyboard confirm no-op, out-of-order batch analysis, lock-held-across-I/O, lost draft, double confirm, stale published buttons, and UserStore memory/disk disagreement.
- Known defects may be tracked as `expectedFailure` only during this phase and must be converted to ordinary passing tests with the corresponding fix. Never assert insecure behavior as desired behavior.

**Target Files**

- `tests/support/workflow_fakes.py`
- `tests/test_handler_characterization.py`
- `tests/test_known_workflow_defects.py`

**Assigned Agent:** Codex

### Task P0-G — Security contract baseline

**Goal & Requirements**

- Add tests for private auth/admin boundaries, PAT-only target behavior, auth expiry, credential deletion failure, stale/foreign callbacks, group PII/rules disclosure, safe logging, and TLS verification configuration.
- Use deterministic tables rather than a new fuzz/property dependency in the first cut.
- Never place credential-shaped values in tracked fixtures beyond approved test-only sentinels.

**Target Files**

- `tests/support/security_fakes.py`
- `tests/test_security_contracts.py`
- `tests/test_privacy_logging_contracts.py`

**Assigned Agent:** Grok

### Phase 0 exit gate

- Current 30 tests still pass.
- New characterization tests pass; each known defect has a named target test.
- Architecture interfaces and security assumptions are frozen before source extraction starts.

## Phase 1 — Domain, FSM, callback, and policy foundation

### Task P1-A — Canonical domain entities and ports

**Goal & Requirements**

- Define frozen `Draft`, `JiraTaskTemplate`, `SourceMessageRef`, `Attachment`, `SubmissionAttempt`, `PublishedIssue`, and opaque correlation identifiers.
- Preserve current template field semantics and list-valued acceptance criteria.
- Reject default/invalid owner and chat IDs; represent UTC timestamps explicitly.
- Define repository, clock, ID/token, analyzer, Jira gateway, VPN, task scheduler, and renderer ports using `Protocol` rather than `I*` framework classes.
- Do not persist raw provider exceptions as domain fields.

**Target Files**

- `src/dztgbot/domain/models.py`
- `src/dztgbot/domain/ports.py`
- `tests/test_domain_models.py`

**Assigned Agent:** Antigravity

### Task P1-C — Complete state machine and typed certainty/errors

**Goal & Requirements**

- Define states for collection, analysis, review/edit, submitting, definite retryable failure, submission unknown, created, attaching, partial attachment, published update review/update/unknown, cancellation, expiry, and supervised unresolved abandonment.
- Keep retryability separate from mutation certainty.
- Define the legal transition table and compare-and-swap transition command/result types.
- Make `SUBMISSION_UNKNOWN` non-retryable, non-expiring, and not ordinarily cancellable.
- Add exhaustive table-driven tests for every legal and illegal transition.

**Target Files**

- `src/dztgbot/domain/fsm.py`
- `src/dztgbot/domain/errors.py`
- `tests/test_workflow_fsm.py`
- `tests/test_error_classification.py`

**Assigned Agent:** Codex

### Task P1-G — Callback grammar and security policy

**Goal & Requirements**

- Implement strict `j1:<action>:<opaque-token>` parsing with total length, alphabet, version, and action allowlists.
- Define hashed-token records, one-shot/expiry semantics, and authorization inputs without logging or echoing attacker-controlled data.
- Define first-release policies: private-only workflows/admin, PAT-only auth, actor/chat/thread/message binding, and safe user-visible denial codes.
- Generate no token shorter than 128 random bits.

**Target Files**

- `src/dztgbot/domain/callbacks.py`
- `src/dztgbot/domain/policy.py`
- `tests/test_callback_grammar.py`
- `tests/test_security_policy.py`

**Assigned Agent:** Grok

### Phase 1 exit gate

- New domain modules have no Telegram/provider imports.
- FSM and callback grammar have exhaustive branch tests.
- Antigravity freezes models/ports before Codex and Grok finalize dependent implementations.

## Phase 2 — Durable workflow and safe credential persistence

### Task P2-C — SQLite workflow repository and migrations

**Goal & Requirements**

- Implement production SQLite storage for workflows, callback records, attempts, attachments, published issues, revisions, and expiry.
- Enable foreign keys; use short transactions, unique constraints, indexes, schema versions, and a bounded busy timeout.
- Verify WAL only on a local runtime filesystem. Tests use temporary local directories and never the OneDrive checkout.
- Store callback token hashes, not raw tokens.
- Implement atomic revision CAS, one-winner attempt claims, restart recovery, non-deletion of unknown outcomes, and terminal-state retention.
- No network awaits inside transactions.

**Target Files**

- `src/dztgbot/infrastructure/persistence/workflow_sqlite.py`
- `src/dztgbot/infrastructure/persistence/migrations/001_initial.sql`
- `src/dztgbot/infrastructure/persistence/migrations/002_indexes.sql`
- `tests/test_workflow_repository.py`
- `tests/test_workflow_migrations.py`

**Assigned Agent:** Codex

### Task P2-G — Credential-store correctness and corruption safety

**Goal & Requirements**

- Fix mutate-before-persist using copy-on-write: persist a complete validated snapshot, then swap memory only after success.
- Validate top-level and entry schemas, IDs, types, file regularity, ownership/permissions as supported, and safe maximum sizes.
- Fail safely on corrupt/truncated/wrong-shaped storage; retain a recoverable previous copy rather than silently becoming empty.
- Fsync the parent directory after replace where supported.
- Keep PAT values redacted from representations and failures.

**Target Files**

- `src/dztgbot/user_store.py`
- `tests/test_user_store_failures.py`
- `tests/test_user_store_permissions.py`

**Assigned Agent:** Grok

### Phase 2 exit gate

- Crash/restart and concurrent-claim tests pass.
- Simulated credential write/remove failure leaves memory and disk identical to their prior state.
- No production workflow state is process-memory-only.

## Phase 3 — Pure application services

### Task P3-C — Event-driven intake and batching service

**Goal & Requirements**

- Implement workflow-scoped collection keyed by owner/chat/thread and seal each batch into its own draft before analysis.
- Check deduplication, batch count, content budget, and attachment eligibility before mutation.
- Replace the 500 ms polling loop with an injectable cancellable deadline owned through the scheduler port.
- Never hold a lock or transaction while sending Telegram messages or calling Gemini.
- Prevent later batches or out-of-order results from overwriting earlier drafts/attachments.

**Target Files**

- `src/dztgbot/services/intake_service.py`
- `tests/test_intake_service.py`
- `tests/test_batch_concurrency.py`

**Assigned Agent:** Codex

### Task P3-A — Workflow and connectivity orchestration

**Goal & Requirements**

- Implement pure use cases for starting/manual editing/reviewing/cancelling/expiring drafts through ports.
- Keep all state changes in the repository and all rendering/Telegram work outside the service.
- Implement one logical lazy-VPN ensure operation with a short positive cache/single-flight behavior and invalidation on connection error.
- Preserve the existing narrow NetworkManager L2TP/IPsec boundary and `VPN_ALLOW_START` rules.

**Target Files**

- `src/dztgbot/services/workflow_service.py`
- `src/dztgbot/services/connectivity_service.py`
- `src/dztgbot/vpn.py`
- `tests/test_workflow_service.py`
- `tests/test_connectivity_service.py`

**Assigned Agent:** Antigravity

### Task P3-G — Callback authorization service

**Goal & Requirements**

- Resolve token hash, load workflow, verify actor/private chat/thread/preview message/action/state/revision/expiry, and atomically consume one-shot actions.
- Expire old preview actions whenever a new preview revision is committed.
- Return fixed safe outcomes for foreign, stale, replayed, expired, or already-processing actions.
- Ensure copied/replayed callbacks cannot mutate even within the same private chat.

**Target Files**

- `src/dztgbot/services/callback_service.py`
- `tests/test_callback_authorization.py`
- `tests/test_callback_replay.py`

**Assigned Agent:** Grok

### Phase 3 exit gate

- Services import no Telegram types.
- Deterministic interleaving tests prove one-winner transitions and cross-workflow isolation.
- Known photo, batching, and stale-callback tests are now ordinary passing tests.

## Phase 4 — Provider gateways and mutation recovery

### Task P4-C1 — Jira gateway, canonical payloads, and shared transport

**Goal & Requirements**

- Build strict Jira request/response/error DTOs and one canonical create/update/diff field mapper.
- Use one lifecycle-managed `httpx.AsyncClient`, per-request authorization, bounded connection limits, distinct connect/read/write/pool timeouts, and bounded error parsing.
- Prove one user's PAT cannot become another user's default/header state.
- Remove silent issue-type substitution and unapproved summary truncation.
- Fetch/cache metadata per relevant project/permission scope with a bounded TTL; never cache auth failure.
- Retry only classified safe/idempotent operations, honor `Retry-After`, and never blind-retry create.

**Target Files**

- `src/dztgbot/infrastructure/jira_gateway.py`
- `tests/test_jira_gateway.py`
- `tests/test_jira_credential_isolation.py`
- `tests/test_jira_payload_parity.py`

**Assigned Agent:** Codex

### Task P4-C2 — Submission, reconciliation, and attachment services

**Goal & Requirements**

- Persist and claim an attempt before create I/O; compute a canonical request hash.
- Include the approved Jira marker mechanism when supported.
- Classify definite rejection versus safe retryable failure versus unknown outcome.
- On unknown outcome, disable create retry and reconcile with bounded positive searches/probes; never interpret immediate absence as proof of non-creation.
- Implement published-edit reload, complete diff, confirmation, concurrency conflict, and durable update recovery.
- Persist per-attachment status; bound count/size/type/total bytes; deduplicate; reuse transport/VPN; retry attachments without recreating the issue.

**Target Files**

- `src/dztgbot/services/submission_service.py`
- `src/dztgbot/services/attachment_service.py`
- `tests/test_submission_recovery.py`
- `tests/test_ambiguous_create.py`
- `tests/test_attachment_service.py`
- `tests/test_published_update_conflicts.py`

**Assigned Agent:** Codex

### Task P4-C3 — Gemini gateway and rules efficiency

**Goal & Requirements**

- Validate Gemini DTOs strictly and map to the canonical domain template.
- Enforce per-message and total prompt budgets, one end-to-end deadline, bounded retry/backoff, and typed rate-limit detection.
- Synchronize model-health state and avoid a global sticky outage.
- Read rule content only when a file identity/size/mtime signature changes; enforce rule size and preserve last-known-good behavior.
- Keep unsupported media bytes out of Gemini and describe their actual capability accurately.

**Target Files**

- `src/dztgbot/infrastructure/gemini_gateway.py`
- `src/dztgbot/rules.py`
- `tests/test_gemini_gateway.py`
- `tests/test_rules_cache.py`

**Assigned Agent:** Codex

### Task P4-A — Provider orchestration boundaries

**Goal & Requirements**

- Review and enforce the use-case boundary between workflow services, connectivity, Jira/Gemini ports, and UI.
- Ensure adapters never call Telegram and domain/services never depend on provider SDK exceptions.
- Supply architecture contract tests that detect forbidden imports and cycles.

**Target Files**

- `tests/test_architecture_dependencies.py`
- `docs/architecture/provider-boundaries.md`

**Assigned Agent:** Antigravity

### Task P4-G — Strict configuration and TLS/security defaults

**Goal & Requirements**

- Add absolute `WORKFLOW_DB_PATH` outside the checkout, auth TTL, queue/size/concurrency limits, and optional allowed-user policy configuration.
- Validate Jira URLs with a real parser; reject credentials, missing host, fragments, and unexpected queries.
- Represent absent paths as `None`, validate project keys/numeric bounds, and keep private-chat/PAT-only defaults.
- Prefer a root-managed custom CA bundle. Retain verify-disable only as an explicit escape hatch with one privacy-safe startup warning.

**Target Files**

- `src/dztgbot/config.py`
- `.env.example`
- `tests/test_config_security.py`
- `tests/test_config_paths.py`

**Assigned Agent:** Grok

### Phase 4 exit gate

- Fault injection proves at-most-one create dispatch per attempt and no second create from unknown state.
- Jira credentials remain request-local under concurrency.
- Create/update field parity and attachment retry tests pass.
- Shared transport, rules cache, and prompt bounds have measurable deterministic tests.

## Phase 5 — Telegram UI, authentication, and administrator modules

### Task P5-A — Thin Telegram UI and rendering

**Goal & Requirements**

- Build pure HTML formatters and centralized keyboard constructors using bound callback tokens.
- Add thin draft/callback handlers following parse -> service -> I/O -> service -> render.
- Always use matching parse mode and escaping; keep outputs within Telegram limits.
- Restore/remove reply keyboards consistently, provide explicit stale/expired/already-processing feedback, and never resolve “latest” state.
- Register no background task directly; use the scheduler/application port.

**Target Files**

- `src/dztgbot/ui/rendering.py`
- `src/dztgbot/ui/keyboards.py`
- `src/dztgbot/ui/handlers/drafts.py`
- `src/dztgbot/ui/handlers/callbacks.py`
- `tests/test_ui_rendering.py`
- `tests/test_draft_handler_journeys.py`

**Assigned Agent:** Antigravity

### Task P5-G — PAT-only authentication and private administration

**Goal & Requirements**

- Make auth private-only and PAT-only with a three-minute conversation timeout.
- Delete credential messages best-effort; warn the user when deletion fails without logging content.
- Prevent late ordinary text/menu input from being consumed as credentials.
- Make `/rules`, `/setrules`, `/vpn`, `/vpnstart`, and identity-bearing `/start` behavior private-safe.
- Keep `/logout` claims accurate: local removal does not revoke Jira PATs.

**Target Files**

- `src/dztgbot/jira_auth.py`
- `src/dztgbot/admin.py`
- `tests/test_auth_handlers.py`
- `tests/test_admin_private_only.py`

**Assigned Agent:** Grok

### Task P5-C — Legacy provider compatibility facades

**Goal & Requirements**

- Adapt current import paths to the new domain and gateway contracts without maintaining a second workflow state.
- Re-export or map `JiraTaskTemplate` without field drift.
- Ensure legacy callers receive typed safe errors and do not instantiate per-request HTTP clients.
- Mark facades for removal only after composition cutover; do not duplicate provider logic.

**Target Files**

- `src/dztgbot/analysis.py`
- `src/dztgbot/jira_client.py`
- `tests/test_legacy_facades.py`

**Assigned Agent:** Codex

### Phase 5 exit gate

- Complete mocked journeys pass for forward/manual/review/create/retry/reconcile/attachment/edit/auth/admin flows.
- Private-only and no-PII/no-rules-disclosure policies pass.
- Every rendered callback is bound and old preview tokens are invalidated correctly.

## Phase 6 — Composition-root cutover

### Task P6-A — Integrate and remove legacy workflow authority

**Goal & Requirements**

- Wire models, repositories, services, gateways, UI handlers, task lifecycle, and graceful close in one composition root.
- Explicitly invoke command registration in the custom lifecycle.
- Set update concurrency to one for the initial cutover.
- Make SQLite the only workflow authority; remove mutation dependence on `pending_template`, `pending_photo_file_ids`, `pending_batch`, `editing_draft`, `editing_published_key`, and `last_published`.
- Keep a short compatibility facade only where imports require it; do not dual-write `user_data` and SQLite.
- Close Telegram/provider clients and owned tasks deterministically at shutdown.

**Target Files**

- `src/dztgbot/__main__.py`
- `src/dztgbot/core.py`
- `src/dztgbot/__init__.py`
- `src/dztgbot/domain/__init__.py`
- `src/dztgbot/services/__init__.py`
- `src/dztgbot/infrastructure/__init__.py`
- `src/dztgbot/infrastructure/persistence/__init__.py`
- `src/dztgbot/ui/__init__.py`
- `src/dztgbot/ui/handlers/__init__.py`
- `tests/test_application_wiring.py`
- `tests/test_graceful_shutdown.py`

**Assigned Agent:** Antigravity

### Task P6-C — Recovery and concurrency integration verification

**Goal & Requirements**

- Exercise deterministic cross-chat, out-of-order analysis, double-click, stale revision, restart-in-every-state, timeout-after-commit, attachment partial, and shutdown cases against the integrated application fakes.
- Verify no network await occurs under repository transactions/locks.
- Convert all Phase 0 `expectedFailure` cases to normal passing tests.

**Target Files**

- `tests/test_integrated_workflow_recovery.py`
- `tests/test_integrated_concurrency_matrix.py`
- `tests/test_restart_matrix.py`
- `tests/test_known_workflow_defects.py`

**Assigned Agent:** Codex

### Task P6-G — Integrated security verification

**Goal & Requirements**

- Verify object authorization, private-only policy, PAT-only input, callback replay/expiry, privacy logging, provider error redaction, and credential failure paths through the composed handlers.
- Confirm callback token/database possession alone cannot bypass actor/chat/message checks.

**Target Files**

- `tests/test_integrated_security.py`
- `tests/test_integrated_authz_matrix.py`

**Assigned Agent:** Grok

### Phase 6 exit gate

- All offline tests pass with update concurrency one.
- Repository searches find no legacy latest-state mutation keys and no raw `asyncio.create_task` in application paths.
- Restart and ambiguous-outcome tests prove no draft loss or blind duplicate.

## Phase 7 — Keyed concurrency, limits, and observability

### Task P7-C — Keyed update processing and bounded resources

**Goal & Requirements**

- Implement keyed serialization by workflow with safe collection keys before workflow creation.
- Add global/per-actor Gemini and Jira/attachment semaphores, bounded queues, overload feedback, total deadlines, retry budgets, and non-sticky circuit/cooldown behavior.
- Add privacy-safe counters/timers using opaque correlation IDs only.
- Benchmark synthetic independent workflows and prove a slow workflow does not block unrelated keys.

**Target Files**

- `src/dztgbot/infrastructure/keyed_processor.py`
- `src/dztgbot/services/limits.py`
- `src/dztgbot/services/observability.py`
- `tests/test_keyed_processor.py`
- `tests/test_resource_bounds.py`
- `tests/test_performance_invariants.py`

**Assigned Agent:** Codex

### Task P7-A — Enable keyed processor in the composition root

**Goal & Requirements**

- Integrate the keyed processor only after P7-C tests pass.
- Preserve the ability to fall back to concurrency one through validated configuration.
- Keep shutdown ownership and no-I/O-in-lock guarantees.

**Target Files**

- `src/dztgbot/__main__.py`
- `tests/test_application_wiring.py`

**Assigned Agent:** Antigravity

### Task P7-G — Observability privacy and abuse-control gate

**Goal & Requirements**

- Verify queue/rate-limit responses reveal no other workflow state.
- Assert metrics/log fields exclude raw user IDs, callback tokens, file IDs, message content, PATs, Jira bodies, and VPN details.
- Validate optional allowlist behavior if enabled by the deployment owner.

**Target Files**

- `tests/test_observability_privacy.py`
- `tests/test_abuse_controls.py`

**Assigned Agent:** Grok

### Phase 7 exit gate

- Curated concurrency schedules pass repeatedly and deterministically.
- Measured concurrency never exceeds configured limits.
- Independent workflow progress improves without weakening any security/recovery invariant.

## Phase 8 — Quality, deployment, documentation, and credential threat model

### Task P8-C — Incremental quality gates and reproducible checks

**Goal & Requirements**

- Add Ruff and one authoritative strict type checker for new domain/service/infrastructure modules.
- Add branch coverage gates focused on FSM, callbacks, repository, submission, and security modules; do not require whole-legacy 90% immediately.
- Add reproducible offline CI for Python 3.12 on Ubuntu, dependency checks, and ShellCheck integration.
- Keep runtime and development dependencies separated and pinned deliberately.

**Target Files**

- `pyproject.toml`
- `requirements-dev.txt`
- `requirements.txt`
- `.github/workflows/quality.yml`
- `tests/test_quality_configuration.py`

**Assigned Agent:** Codex

### Task P8-A — Architecture conformance and migration documentation

**Goal & Requirements**

- Update architecture documents to match the implemented modules and actual dependency graph.
- Document incremental cutover completion and removed compatibility paths.
- Record why file size is not a completion metric and identify enforceable import/interface rules instead.

**Target Files**

- `docs/architecture/current-architecture.md`
- `docs/architecture/migration-record.md`

**Assigned Agent:** Antigravity

### Task P8-G — Deployment, operator docs, and credential threat model

**Goal & Requirements**

- Add protected local workflow DB creation/permissions, backup/restore/migration checks, disk-full recovery, and service restart behavior to deployment.
- Remove obsolete `GEMINI_MODEL` deployment requirements if the application still does not consume it.
- Keep Ubuntu 24.04-only enforcement, root-owned secrets, narrow VPN controls, and no automatic full-tunnel start.
- Reconcile README and end-to-end test plan with actual create/update/auth/media behavior.
- Produce a credential threat model. If separate root-managed key lifecycle, vetted AEAD format, rotation, backup recovery, and rollback are approved, propose encryption as its own later change; otherwise document why `0600` host confinement remains the selected boundary. Do not implement cryptography in this task without that approval.

**Target Files**

- `scripts/deploy.sh`
- `deploy/systemd/dztgbot.service`
- `README.md`
- `docs/end-to-end-test-plan.md`
- `docs/security/credential-threat-model.md`
- `docs/operations/workflow-db-runbook.md`

**Assigned Agent:** Grok

### Phase 8 exit gate

- Quality checks pass in a clean Ubuntu 24.04/Python 3.12 environment.
- Runtime DB is outside the checkout and has tested migration/backup/restore behavior.
- Documentation makes no stale feature or live-validation claims.
- Credential encryption has an explicit approved design or an explicit documented deferral; it is never improvised.

## Phase 9 — Release verification

### Task P9-A — Architecture verification record

**Goal & Requirements**

- Verify dependency direction, composition ownership, removal of legacy workflow authority, task/client shutdown, and private-only initial scope.
- Record evidence only; do not claim external validation.

**Target Files**

- `docs/reviews/architecture-remediation-verification.md`

**Assigned Agent:** Antigravity

### Task P9-C — Performance, recovery, and edge-case verification record

**Goal & Requirements**

- Run the complete offline suite, compilation, type/lint checks, deterministic fault matrix, resource-bound tests, and synthetic performance tests.
- Record environment, commands, counts, limits, and safe results without content or secrets.

**Target Files**

- `docs/reviews/performance-recovery-verification.md`

**Assigned Agent:** Codex

### Task P9-G — Security and release-readiness verification record

**Goal & Requirements**

- Verify authz, PAT-only, private admin/workflows, credential store, privacy logging, TLS configuration, secret boundaries, and operator documentation.
- List every external item still unverified.
- A supervised live test plan may be included, but no real Telegram/Gemini/Jira/VPN/systemd action occurs without separate explicit approval.

**Target Files**

- `docs/reviews/security-release-verification.md`

**Assigned Agent:** Grok

### Phase 9 exit gate

- All three evidence reports agree on commit, environment, passed gates, known residual risk, and external evidence boundary.
- The service remains pilot-only until the user approves and completes the required supervised target-environment validation.

## 8. Critical sequencing and merge rules

1. Phase 0 precedes every source refactor.
2. P1-A freezes domain models/ports before P1-C, P1-G, or repository implementation merge.
3. Phase 2 repositories precede callback and mutation cutover; no production in-memory workflow release is allowed.
4. P3/P4 services are tested through ports before Telegram handlers call them.
5. P4-C Jira contracts freeze before P5-C compatibility work and P6-A wiring.
6. Antigravity performs the only edits to composition/export files after provider/security owners declare their interfaces ready.
7. Concurrency remains one through Phase 6. Keyed concurrency is enabled only after Phase 7 tests pass.
8. No phase may leave two workflow authorities or a blind retry from unknown mutation state.
9. Performance work that determines lifecycle/interfaces—shared client, owned debounce, bounds—lands with the relevant adapter. Advanced tuning and circuit behavior wait until correctness is green.
10. Documentation and deployment updates follow implemented behavior, never precede it as claims.

## 9. Explicitly deferred work

- Group or message-thread mutation workflows beyond retaining compatible identity fields.
- Full multimodal Gemini analysis and non-photo attachment expansion.
- Horizontal/multi-instance deployment; SQLite targets the current single-host service.
- Jira issue creation or update from CI.
- Jira rule governance, second-person approval, or new Jira features outside the remediation boundary.
- Credential encryption until the threat model and external key lifecycle are approved.
- Any live-system validation or redeployment without separate authorization.

## 10. Program definition of done

- All non-negotiable invariants are encoded in passing offline tests.
- Durable state and callback authorization eliminate latest-draft/latest-published routing.
- Jira create/update failures preserve intent and unknown outcomes cannot blind-duplicate.
- Auth is private, PAT-only, time-bounded, and honest about deletion/revocation.
- Credentials and workflow state fail safely under write, corruption, restart, and permission errors.
- Network clients, tasks, locks, transactions, queues, files, and retries have explicit lifecycle and bounds.
- Domain/services are free of Telegram/provider dependencies and `core.py` is no longer a workflow monolith.
- Strict checks gate new critical modules; legacy strictness is retired incrementally.
- Deployment and user documentation match code and evidence.
- Offline and live verification boundaries remain explicit, and no real external mutation occurs without human authorization.
