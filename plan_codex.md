# Codex proposal: code quality, performance, and edge-case hardening

Date: 2026-08-07  
Reviewed repository state: `353973a7ccc6` on `main`  
Primary evidence: `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md`, the required context documents, current source, and current offline tests.

## Purpose and scope

This proposal focuses on correctness under concurrency and failure, strict typing and validation, predictable resource use, and maintainable algorithms. It does not propose feature expansion. In particular, multimodal analysis, Jira issue creation during testing, deployment changes, and live external-system validation remain outside this planning pass.

No application source or existing document was changed while preparing this proposal.

## Executive recommendation

Treat workflow identity and mutation recovery as the first performance and quality project. Optimizing individual functions while the bot can route an old callback to the wrong draft, lose a draft before a failed Jira request, or race shared `user_data` would make the unsafe paths faster without making them reliable.

The recommended target is:

1. a typed, durable draft/submission model with explicit states and revisions;
2. callback-to-entity binding plus owner/chat/message authorization;
3. keyed serialization for state transitions, while slow Gemini and Jira I/O remains concurrently bounded;
4. failure-preserving, duplicate-resistant Jira mutation orchestration;
5. reusable network clients, bounded queues, and event-driven batching;
6. strict validation at every Telegram, Gemini, Jira, configuration, and persistence boundary;
7. handler-level, concurrency, recovery, and property-based tests enforced by static analysis.

The safest delivery strategy is several small, independently testable changes rather than a direct rewrite of `core.py`.

## Evidence and discrepancies found in the current tree

The 30 existing offline tests pass on Python 3.12.13. They mostly cover utility functions, stores, VPN parsing, Gemini fallback, and privacy-safe global error logging. They do not instantiate the complete handler graph or test draft ownership, stale callbacks, concurrent updates, restart recovery, or ambiguous external mutations.

The current code confirms the audit's major state and recovery findings and adds these concrete issues:

- `handle_edited_text_input` appends every ordinary photo to `pending_photo_file_ids` before checking whether draft editing is active. A photo sent outside a draft can therefore attach to a later issue.
- `analyze_forward` appends a forwarded photo before enforcing `MAX_BATCH_SIZE`, so a rejected 21st message can still add an attachment.
- The reply-keyboard `✅ 確定提交工單` path sets `editing_draft` to false and then reaches the guard that immediately returns. In the reviewed tree it is a silent no-op, not the second-preview behavior described in the audit. This documentation/code discrepancy should be resolved with a characterization test before changing behavior.
- A batching worker marks itself inactive before Gemini analysis completes. A second batch can analyze concurrently and either result can overwrite the single `pending_template` and shared attachment list.
- The batching lock is held while sending or editing Telegram messages. Network latency therefore extends the critical section.
- `asyncio.create_task` owns the batch worker outside the Telegram application lifecycle. Exceptions, cancellation, and shutdown completion are not managed centrally.
- `UserStore.store` and `UserStore.remove` mutate the in-memory dictionary before the disk write succeeds. A failed write can make memory claim a credential was stored or removed while disk contains the opposite state.
- `UserStore.initialize` silently starts empty for several corrupt-file cases, but other malformed shapes can escape the listed exception set and terminate startup. Silent reset is dangerous for an authentication store.
- `RulesStore.current_rules` reads and compares the entire rules file for every Gemini request, even when the file has not changed.
- `JiraClient` creates and closes a new `httpx.AsyncClient` for every request. Attachment loops also repeat VPN checks and establish new HTTP/TLS connections for every file.
- Jira success responses and `/myself` payloads are accessed as untyped dictionaries. Invalid JSON, missing keys, or wrong field types can escape the intended `JiraClientError` boundary.
- Jira create/update automatically retries a rejected issue type as `Task`. That silently changes approved content and a create retry is a second external mutation.
- Initial create and published update use different field projections. Updates omit labels, components, assignee, and acceptance criteria.
- Gemini fallback uses mutable global model selection without synchronization, detects rate limiting through broad string matching, and applies the configured timeout once per model rather than as one end-to-end deadline.
- Repeated manual reconstruction of `JiraTaskTemplate` and duplicated keyboard rendering create field-loss risk whenever the model changes.
- The README says Jira creation is not implemented, while the current code contains create, update, and attachment mutations. The audit already identifies this broader documentation drift.
- There is no checked-in strict type-checker, linter, coverage gate, or automated handler test harness.

## Required correctness invariants

These invariants should guide the implementation and serve as test names:

1. A Telegram callback can affect exactly the entity named by that callback, never a user's latest entity.
2. Only the draft owner can mutate a draft, and the callback must match the bound chat and preview message.
3. Every state transition is legal, atomic, revision-checked, and auditable without storing message content or credentials in logs.
4. At most one Jira create attempt for a draft may be actively dispatched at a time.
5. A failed or ambiguous Jira request never destroys the draft or its retry/reconciliation information.
6. A callback, message, or task arriving twice has a defined idempotent outcome.
7. Unsupported, stale, expired, unauthorized, and malformed inputs produce explicit safe responses and no mutation.
8. Slow external I/O never occurs while a workflow-state lock or database write transaction is held.
9. All queues, prompts, attachments, retries, response bodies, and retained entities have configured bounds.
10. Shutdown either completes registered work within a deadline or persists a state that is safe to reconcile after restart.

## Proposed design

### 1. Introduce typed domain entities and an explicit state machine

Create a small domain layer independent of Telegram objects. Suggested entities are `Draft`, `SourceMessage`, `Attachment`, `SubmissionAttempt`, and `PublishedIssue`, using frozen dataclasses or strict Pydantic models. Use `StrEnum` for state, callback action, attachment status, mutation result, and error category.

Minimum `Draft` identity and control fields:

- opaque `draft_id` suitable for Telegram's 64-byte callback-data limit;
- `owner_user_id`, `chat_id`, optional `message_thread_id`, and `preview_message_id`;
- monotonic integer `revision` for optimistic concurrency control;
- explicit `state`, `created_at`, `updated_at`, and `expires_at` in UTC;
- validated Jira template snapshot and the rules/metadata revision used to create it;
- bounded source-message references and attachment records;
- current submission attempt and created Jira identity, when known.

Suggested states:

`COLLECTING -> ANALYZING -> REVIEW -> SUBMITTING -> CREATED -> ATTACHING -> COMPLETE`

with explicit side paths to `CANCELLED`, `EXPIRED`, `ANALYSIS_FAILED`, `SUBMISSION_RETRYABLE`, `SUBMISSION_UNKNOWN`, `ATTACHMENT_PARTIAL`, `EDITING_PUBLISHED`, and `UPDATE_RETRYABLE`.

Define allowed transitions in one table or transition function. Invalid transitions should return a typed conflict, not silently fall through. Perform transitions with a compare-and-swap condition on `(draft_id, revision, expected_state)`.

Do not store domain entities as arbitrary strings inside `context.user_data`. Telegram context may retain only small navigation hints; the repository is authoritative.

### 2. Use a transactional repository

Use SQLite for drafts, attempts, attachment status, callback routing, expiry, and published issue references. SQLite is appropriate for the current single-host, single-process deployment and removes full-file rewrites while supplying transactions, unique constraints, and restart recovery.

Recommended storage properties:

- schema version table and forward-only migrations;
- foreign keys enabled;
- WAL mode if verified against the actual local filesystem;
- a bounded busy timeout;
- one serialized writer path, short transactions, and no network calls inside transactions;
- unique constraints for callback token, `(draft_id, attempt_number)`, and the chosen idempotency marker;
- indexes on owner/chat/state, expiry, callback token, and unresolved submission state;
- explicit retention/garbage-collection policy for terminal drafts and attachment metadata;
- backup/recovery procedure tested with a copied database.

Keep the credential-protection decision separate from ordinary workflow data. If credentials remain in JSON temporarily, fix copy-on-write semantics: build a new dictionary, persist it, then swap the in-memory reference only after success. Validate the whole file shape strictly, verify regular-file permissions, reject unsafe corruption rather than silently becoming empty, and retain a safe previous copy. A SQLite credential migration should require an approved encryption/secret-boundary design.

For atomic file writers that remain, fsync the containing directory after `os.replace` on supported systems so the rename itself is durable across a crash.

### 3. Bind and authorize every callback

Encode a compact versioned callback such as `j1:<action>:<opaque-token>`. The opaque token resolves server-side to `draft_id`, revision, owner, chat, message, expiry, and action. Do not put credentials, Jira details, or message content in callback data.

Before any action:

1. parse callback data with a strict length and grammar;
2. load the callback record and draft;
3. compare effective user, chat, message ID, and expected revision;
4. reject expired, stale, already-consumed, or illegal-state actions;
5. atomically consume one-shot actions or advance the revision;
6. render controls for the new revision.

Double-clicks and redelivered callback queries should receive “already processing/completed” without a second mutation. Old copy/edit buttons must resolve to their own published issue or report expiry; they must never consult one `last_published` slot.

### 4. Make concurrency state-aware

As an immediate safety setting, run stateful updates sequentially until the keyed processor is ready. The target design should serialize only transitions for the same workflow key while preserving concurrency across independent workflows.

Use a key such as `(owner_user_id, chat_id, message_thread_id)` during collection and `draft_id` after creation. Keep each critical section limited to repository reads and writes. Release it before Telegram, Gemini, Jira, VPN, or file-download I/O, then commit the result only if the draft revision and expected state still match.

Replace raw `asyncio.create_task` with the Telegram application's task registration or another owned task supervisor. Track task name, workflow ID, cancellation, result, and shutdown deadline without logging private content.

Replace the 500 ms polling batch loop with a cancellable debounce deadline per batch. Each accepted message reschedules one timer. When it fires, atomically seals the batch. This removes repeated wakeups and makes the 2.5-second boundary deterministic under event-loop delay.

Do not mark a batch available for reuse until it has been sealed into its own draft with its own attachment collection. Later messages must create or join another batch rather than share analysis output state.

### 5. Make Jira mutation duplicate-resistant and recoverable

Model submission as persisted orchestration, not a handler-local `try/except`:

- atomically transition `REVIEW -> SUBMITTING` and create an attempt record before I/O;
- compute a canonical request hash from the approved Jira payload;
- attach a stable bot-generated idempotency marker to the Jira issue through an approved Jira property, custom field, or other queryable mechanism;
- after a definite rejection, retain the draft in a retryable state with the safe error category;
- after a timeout, disconnect, process loss, or malformed success response, use `SUBMISSION_UNKNOWN` and reconcile by marker before allowing another create;
- never automatically repeat a create merely because the response was lost;
- after reconciliation, record the issue key and resume pending attachments;
- make Retry and Cancel explicit legal transitions.

The exact Jira marker mechanism must be verified against the deployed Jira Server/Data Center version before implementation. If no reliable queryable marker exists, the UI must require supervised reconciliation for ambiguous creates rather than risk a duplicate.

Remove the silent issue-type fallback. Fetch create metadata and permissions, validate the approved payload, and return the user to review if Jira rejects a field. Safe automatic retries should be limited to operations proven idempotent and to bounded transport/status categories; honor `Retry-After` where available and add exponential backoff with jitter.

For published edits, fetch the current Jira issue, compare it with the stored base snapshot, present a complete diff, and require confirmation. Detect concurrent human changes and ask for reconciliation instead of overwriting them. Use one shared payload builder for create, update, preview, and validation so field support cannot drift.

### 6. Establish strict validation and typed errors

Move business constraints into `JiraTaskTemplate` or a dedicated validated value object so AI, quick, manual, edited, create, and update paths all use the same rules. Add constraints for:

- trimmed, non-empty summary and description with explicit maximum lengths;
- validated project-key grammar and normalization policy;
- metadata-backed issue type and priority;
- bounded, deduplicated labels/components/acceptance criteria;
- Jira-compatible label grammar and length;
- optional assignee normalization;
- total serialized payload and prompt size.

Avoid silently truncating the Jira summary after approval. Validation should show the user what must change.

Validate all provider responses with strict models:

- Gemini structured output;
- Jira `/myself`, create, fetch, update, metadata, error, and attachment responses;
- persisted credential and workflow schemas;
- callback grammar and Telegram context prerequisites;
- configuration URLs, paths, project keys, numeric limits, and incompatible combinations.

Use `urllib.parse` or an equivalent strict URL model for `JIRA_URL`; reject embedded credentials, missing hostnames, fragments, and unexpected query strings. Represent an absent VPN profile as `Path | None`, not `Path('.')`.

Introduce a typed error hierarchy containing at least `kind`, `operation`, `retryability`, `mutation_certainty`, and a fixed safe user message. Preserve the original exception only as an internal cause. Examples include authentication, permission, validation, rate-limit, connectivity, timeout-before-send, outcome-unknown, provider-contract, conflict, stale action, and storage failure.

Do not log provider exception strings, Telegram file IDs, Jira error bodies, tokens, message text, or URLs that may carry authentication material. Log a privacy-safe workflow ID, operation, exception class, latency bucket, and outcome code. Replace broad `except Exception: pass` blocks with narrow exceptions and an intentional fallback; where a best-effort UI edit is ignored, count it and document why.

Define `Protocol` interfaces for the analyzer, workflow repository, credential store, Jira gateway, VPN manager, clock, and task scheduler. This removes `object` plus `hasattr` checks and makes handler tests small and deterministic.

### 7. Improve network and resource performance

#### Jira

Maintain one lifecycle-managed `httpx.AsyncClient` with configured connection-pool limits and per-request authorization headers. This reuses DNS, TCP, and TLS state without sharing a user's credential in default headers. Close it during application shutdown.

Use separate connect, read, write, and pool timeouts. Bound response-body/error extraction. Cache Jira create metadata per project and permission scope for a short TTL, and invalidate it after relevant validation errors. Do not cache authentication failures.

Check/start the VPN once per logical Jira operation. Add a short positive-status TTL and single-flight refresh; invalidate the cache on connection failure. The existing startup lock should remain.

#### Attachments

Validate count, Telegram-reported size, MIME type, and total byte budget before download. Deduplicate by Telegram `file_unique_id` when available, while retaining `file_id` only in protected runtime state. Persist each attachment's status independently.

Use a small bounded pipeline, initially two concurrent transfers globally and one or two per issue after measurement. Avoid downloading all attachments into memory together; stream or spool each file to a protected bounded temporary location if the libraries require a seekable multipart body. Reuse the Jira client and avoid repeating VPN startup per attachment. Report each failed attachment and support retry without recreating the issue.

#### Gemini

Set a maximum message count, per-message character count, total prompt size, and total retained source size. Reject or explicitly summarize overflow before the provider call; do not silently cut approved business data.

Add a global Gemini semaphore, per-user in-flight limit, bounded queue, and clear overload response. Use one end-to-end deadline across all model attempts. Classify rate limits by typed status/code rather than broad substring matching. Apply cooldown/circuit-breaker state per model so a burst does not make every request probe an already rate-limited model. Synchronize model-health updates.

Retry only classified transient failures with a small attempt budget, exponential backoff, jitter, and server guidance. Invalid structured output should generally fail validation rather than consume every model unless a separately bounded repair policy is approved.

#### Rules and local stores

Track a rules-file signature such as inode/file identity, size, and nanosecond modification time. Return the in-memory immutable rules/version when unchanged and read content only after the signature changes. Enforce a rules-size limit before displaying or inserting rules into a prompt.

SQLite makes workflow reads indexed and mutations proportional to the changed entity rather than the number of users or drafts. Until credential storage changes, return immutable objects and avoid taking a lock for pure in-memory reads; serialize only copy-on-write persistence.

### 8. Simplify code structure and algorithms

Split `core.py` by responsibility after characterization tests exist:

- `domain.py`: entities, enums, constraints, transitions;
- `workflow_repository.py`: transactions, migrations, queries, expiry;
- `intake.py`: forward detection, deduplication, batching;
- `draft_handlers.py`: command/message/callback adaptation;
- `rendering.py`: escaped messages and keyboard builders;
- `submission.py`: create/update orchestration and reconciliation;
- `attachments.py`: validation and transfer pipeline;
- `jira_client.py`: transport and provider models only.

Handlers should follow a uniform shape: parse -> authorize -> transition -> perform I/O -> transition -> render. Domain services should not import Telegram classes.

Replace repeated full `JiraTaskTemplate(...)` reconstruction with `model_copy(update=...)` followed by complete revalidation, or named domain update methods. Centralize keyboard construction so every button receives the correct token and revision. Centralize HTML rendering and always set the matching parse mode; never mix unparsed Markdown markers with HTML escaping.

Use a single-pass parser with explicit section tokens for editable text. It should support full-width and ASCII punctuation, preserve intentional blank lines, allow explicitly clearing optional fields, reject duplicate/unknown headers, and distinguish a simple manual title/description from the structured editor format. Parser ambiguity must result in a validation response rather than guessed mutation.

Deduplicate incoming Telegram updates using `update_id` and source messages using `(chat_id, message_id)` or the best available stable origin. Apply the batch-size check before attachment mutation. Check editing state and workflow ownership before accepting manual photos or keyboard labels.

### 9. Tooling and quality gates

Add a development-only quality configuration rather than changing runtime dependency behavior unnecessarily:

- Ruff for formatting and linting, including async, exception, import, and complexity rules;
- mypy in strict mode (or Pyright strict, but select one authoritative checker);
- coverage with branch coverage;
- dependency audit and pinned, reproducible dependency update process;
- CI on Python 3.12 matching Ubuntu 24.04, with Windows only as an optional portability check;
- ShellCheck for `scripts/deploy.sh` and a safe systemd verification step.

Recommended initial gates:

- zero type errors in new domain/repository/orchestration modules;
- no unowned background task;
- no network await inside a workflow lock or database transaction;
- no bare `except` and no unexplained broad exception suppression;
- branch coverage of at least 90% for transition, callback authorization, payload building, and error-classification modules;
- all mutation/recovery invariants tested before enabling concurrency above one.

Do not force the entire legacy module to strict typing in one change. Make the gate strict for new modules and shrink a documented legacy exclusion as handlers migrate.

## Edge-case and regression test plan

### Telegram handler journeys

- direct forward, reply-to-forward, quick manual, and guided manual happy paths;
- ordinary photo outside editing does not enter any draft;
- the 21st forwarded message adds neither source content nor attachment;
- reply-keyboard confirm produces the specified review transition exactly once;
- cancel from a stale reply keyboard cannot cancel another draft;
- unsupported media is rejected or clearly marked according to the approved capability boundary;
- HTML-sensitive and Markdown-sensitive user text renders literally and stays within Telegram limits;
- missing effective message/user/chat/query data returns safely.

### Identity, ordering, and concurrency

- two drafts by one user in two chats never share messages, photos, status messages, or responses;
- two batches by one user can analyze out of order without overwriting one another;
- two users can work concurrently without a global state collision;
- an old preview's toggle/edit/cancel/confirm targets only its bound revision;
- double confirm, duplicated update delivery, and callback replay create at most one Jira issue;
- a callback from the wrong user, chat, thread, or message produces no mutation;
- a callback racing expiry/cancel receives a deterministic conflict response;
- cancellation and process shutdown during analysis persist a recoverable state.

Use deterministic barriers and a fake clock rather than timing sleeps. Run selected schedules repeatedly and add property/state-machine tests that generate legal and illegal event sequences.

### Jira failure and recovery

- definite 400/401/403/404/409/429/5xx classifications;
- connect failure before dispatch versus read timeout with unknown mutation outcome;
- timeout after the fake Jira server commits create, followed by successful reconciliation and no second POST;
- malformed JSON, missing issue key, invalid field types, oversized error body, and unexpected content type;
- no silent type fallback and no unapproved field truncation;
- update conflict with a human Jira edit preserves both versions and requests review;
- complete create/update field parity;
- partial attachment success, retry, duplicate attachment input, oversized file, and Telegram download failure;
- `Retry-After`, retry budget, backoff, and circuit-breaker behavior with a fake clock.

### Persistence and restart

- write failure leaves both memory and disk at the previous credential state;
- corrupt, truncated, wrong-shaped, symlinked, or permission-unsafe credential storage fails safely;
- crash/restart in every nonterminal workflow state has a documented recovery transition;
- schema migrations are transactional and idempotent;
- two attempts to claim the same draft result in one winner;
- expiry and retention remove only eligible terminal records;
- database locked/full/read-only/corrupt conditions become typed storage failures without state fabrication.

### Authentication and parsing

- auth conversation timeout and late ordinary text are not treated as credentials;
- credential-message deletion failure produces a security warning and safe next action;
- PAT-only parsing rejects password/cookie shapes if PAT-only is approved;
- whitespace-only, extremely long, Unicode, duplicated header, unknown header, empty optional field, and full-width-colon editor inputs;
- all draft origins receive identical model/business validation.

### Performance and boundedness

- unchanged rules do not reread file content;
- a logical Jira operation performs one VPN check/start sequence;
- multiple Jira calls reuse one connection pool without credential crossover;
- batching uses one scheduled deadline and no polling loop;
- measured concurrency never exceeds configured Gemini, Jira, or attachment limits;
- queue overflow rejects work promptly and does not grow memory;
- maximum-size permitted batches, prompts, previews, and attachment sets remain within declared budgets;
- a synthetic burst of independent workflows makes progress while a slow workflow cannot block unrelated keys.

## Delivery sequence

### Phase 0: characterize current behavior

Add handler fakes and failing regression tests for stale callbacks, cross-chat batching, ordinary-photo contamination, batch-cap attachment leakage, reply-keyboard confirm, double confirm, lost draft on Jira failure, published-edit failure, and unmanaged shutdown tasks. Decide the intended reply-keyboard confirm behavior where audit and code disagree.

Exit criterion: every known defect has a deterministic failing test, and existing behavior that must be preserved is characterized.

### Phase 1: domain model, errors, and repository

Add typed entities, transition table, callback-token model, typed errors, clock/ID interfaces, SQLite schema, migrations, and repository tests. Fix credential copy-on-write and corrupt-store handling independently if credential migration is deferred.

Exit criterion: transition and repository invariants pass under conflicts, simulated write failures, and restart cases.

### Phase 2: safe handler routing and task lifecycle

Migrate batching, manual editing, previews, callback authorization, expiry, and rendering to the repository. Temporarily set update concurrency to one, then introduce keyed serialization. Replace polling debounce and raw task creation.

Exit criterion: stale, duplicated, cross-chat, out-of-order, and shutdown tests pass; no handler mutation depends on “latest” user state.

### Phase 3: Jira gateway and mutation recovery

Add the shared client, strict Jira response models, unified payload builder, metadata validation, safe error taxonomy, attempt records, idempotency marker, reconciliation, retry controls, published-update diffing, and attachment status/retry.

Exit criterion: fault-injection tests prove at-most-one create per draft, ambiguous-outcome recovery, field parity, and retained retry context.

### Phase 4: bounded performance and operability

Add Gemini/Jira/attachment limits, queues, deadlines, backoff, circuit breakers, VPN positive cache, rules signature cache, retention jobs, and privacy-safe metrics. Measure before tuning concurrency defaults.

Exit criterion: boundedness tests pass, synthetic bursts show isolation, and resource/latency metrics provide evidence for production limits.

### Phase 5: modularization and enforced quality gates

Complete the `core.py` split, remove legacy `user_data` workflow keys, enable strict static checks across migrated modules, reconcile documentation, and stage supervised external validation.

Exit criterion: CI enforces the chosen type/lint/test gates, operational documents match code, and live validation is performed only with explicit authorization.

## Performance targets to validate, not assume

Final numeric limits should be chosen from target-server measurements, but the implementation should initially enforce these qualitative budgets:

- constant number of scheduled tasks per open batch or draft;
- no O(number of users) file rewrite for ordinary workflow mutations;
- no repeated rules content read when unchanged;
- one reusable Jira connection pool per process;
- bounded concurrent Gemini requests and attachment transfers;
- bounded work queue with prompt overload feedback;
- bounded prompt, response, error body, attachment count, attachment bytes, and retained history;
- independent workflow keys continue while another waits on Gemini, Jira, Telegram, or VPN;
- at most one create dispatch and one reconciliation owner per draft.

Record p50/p95 external latency, queue delay, provider attempt count, timeout category, and workflow completion outcome using opaque workflow IDs. Do not log message or credential content. Performance changes should be accepted only when benchmarks show improvement without weakening correctness invariants.

## Decisions required before implementation

1. Whether mutating workflows are private-chat-only or must support groups and message threads.
2. Whether Jira authentication becomes PAT-only.
3. Approval to use SQLite for durable workflow state and the separate credential-storage protection design.
4. The Jira-supported idempotency marker mechanism and retention period.
5. Exact supported issue types, priorities, projects, and required Jira fields from live metadata.
6. Maximum batch characters, attachment count/bytes, per-user in-flight work, global concurrency, queue size, and retention periods.
7. Whether published Jira edits require a second confirmation after displaying a diff; this proposal recommends yes.
8. Whether unsupported media should be rejected immediately or retained as attachment-only; actual AI analysis must not be implied when bytes are not sent.

## Deferred scope

- Full multimodal Gemini processing until media privacy, size, format, and cost policies are approved.
- Horizontal/multi-instance deployment; the proposed SQLite and keyed-lock design targets the current single-host service.
- Jira issue creation in automated tests against the real server.
- Live Telegram, Gemini, Jira, VPN, systemd, or remote-server claims without explicit access and authorization.
- New Jira features or administrator workflows unrelated to correctness, performance, or recovery.

## Definition of done

This hardening effort is complete only when:

- every callback is entity-, actor-, chat-, message-, state-, and revision-bound;
- drafts and submission attempts survive restart and failures without unsafe duplication;
- the known photo, batch-cap, confirm, published-edit, and store-consistency bugs are covered and fixed;
- no generic latest-draft/latest-published state remains in mutation paths;
- Jira create/update never silently changes approved fields;
- external and persisted data is strictly validated and converted to typed safe errors;
- network clients and background tasks have explicit lifecycle ownership;
- queues, timeouts, retries, prompts, files, and retention are bounded;
- tests cover handler journeys, concurrent schedules, stale actions, storage faults, and ambiguous mutation outcomes;
- strict type/lint/branch-coverage gates pass for the new architecture;
- offline verification is clearly separated from any later supervised live validation.

## Cross-Audit Critiques & Rebuttals

### Overall assessment

All three plans converge on the correct architectural center: uniquely identified workflows, object-level callback authorization, explicit state transitions, transactional persistence, thin Telegram handlers, failure-preserving Jira orchestration, and handler-level tests. That consensus is strong enough to define the remediation direction.

The plans are not interchangeable, however. Antigravity supplies the clearest package/layer decomposition but its sample identity and timeout-recovery mechanics are unsafe. Grok supplies the strongest security and developer-experience framing but overstates encryption as an early control, leaves a dangerous transition out of an unknown mutation outcome, and occasionally turns undecided product policy into a proposed default. Neither peer plan gives enough attention to the concrete performance defects in the current network, batching, rules, and credential-store implementations.

### Critique of `plan_antigravity.md`

#### Recommendations worth adopting

- The presentation/application/domain/infrastructure separation is a useful dependency rule, especially keeping Telegram objects out of business logic.
- Extracting formatters and keyboard construction is necessary to remove the current duplicated callback and rendering code.
- Repository and adapter boundaries will make concurrency and provider-failure tests far simpler.
- Moving VPN orchestration out of the low-level Jira transport is sensible: a Jira gateway should receive connectivity as an orchestration concern rather than discover it through an `object` with `hasattr`.
- The sequence diagrams make the intended use cases reviewable, even though the failure semantics need correction below.

#### Technical flaws and regression risks

1. **The proposed eight-character UUID prefix is not an adequate workflow identifier.** `str(uuid.uuid4())[:8]` retains only 32 random bits. Collision probability becomes material far sooner than a durable business workflow store should tolerate, and the short identifier is guessable enough that it must never be treated as authorization. Use at least 128 bits of cryptographic randomness internally. Telegram callback data should carry a compact, opaque server-side token, not a truncated database identifier.

2. **The callback envelope is under-validated and under-bound.** `dft:<draft_id>:<action>` binds only an entity name. It has no version, revision, expiry, preview message, thread, actor, or one-shot consumption semantics. Its decoder accepts arbitrary prefixes/actions/payload lengths and raises an exception containing attacker-controlled callback text. The correct design is a strict versioned grammar plus an opaque token resolving to owner, chat, optional thread, preview message, expected revision/state, action, and expiry. Authorization happens after lookup; possession of a token is not authorization.

3. **The timeout sequence reintroduces duplicate creation.** The diagram converts a Jira timeout directly to `FAILED_RETRYABLE` and offers Retry. A read timeout or connection loss after dispatch has an unknown outcome: Jira may already have committed. This must transition to `SUBMISSION_UNKNOWN`, suppress another create, and reconcile through a verified external marker or supervised process. A state lock prevents concurrent double-clicks only; it does not provide idempotency across timeout, restart, or later retry.

4. **`InMemoryDraftRepository` is not a production alternative.** The audit explicitly requires restart recovery for reviews, attempts, published associations, and ambiguous outcomes. An in-memory implementation is valuable only as a test fake. The production repository must be transactional and durable before Jira mutation is considered safe.

5. **The sample domain model permits invalid entities.** Mutable dataclasses with `owner_id=0`, `chat_id=0`, optional templates in broad states, raw `last_error`, and no revision allow illegal states to be constructed. `acceptance_criteria` is also changed from the current `list[str]` to `str`, which would lose structure. Use validated value objects, frozen entities/snapshots, state-specific requirements, and a privacy-safe typed error code rather than provider text.

6. **Persisting `source_texts` and Telegram `file_id` values needs a data policy.** The example retains raw forwarded content and provider identifiers without size bounds, encryption decision, expiry, or deletion rules. Persist only what recovery requires, cap it, assign retention by state, and keep sensitive content out of logs and long-lived records where possible.

7. **`JobQueue` is not automatically available in the current installation.** Python Telegram Bot's job queue requires its optional job-queue dependencies, which are not in `requirements.txt`. Adding it is possible but should be an explicit dependency decision. For a 2.5-second debounce, an application-owned task plus a cancellable event-loop deadline may be smaller and easier to test. Raw `asyncio.create_task` must still be removed.

8. **“Sequential per chat/user” is not a precise concurrency key.** Per-user processing still couples different chats; per-chat processing can interleave two users mutating one shared workflow. Collection may be keyed by owner/chat/thread, while post-creation transitions must be serialized by workflow ID and enforced transactionally by revision.

9. **The plan does not address provider and storage performance.** It omits reusable `httpx` connection pooling, repeated VPN checks, sequential per-file client creation, rules-file rereads on every analysis, total Gemini deadlines, bounded queues, prompt limits, and copy-on-write credential failures. These are not premature micro-optimizations; they are current scalability and consistency defects.

10. **The 15 KB file-size acceptance criterion is a vanity metric.** File size neither proves cohesion nor prevents a god object split across several coupled files. Enforce dependency direction, cyclomatic/branch complexity where useful, narrow public interfaces, and testability instead.

11. **`pip check == 0` needs an environment qualifier.** The current Windows environment has two known platform-support reports while the supported deployment target is Ubuntu 24.04. Dependency health should be checked in a clean, reproducible Ubuntu/Python 3.12 environment and in any explicitly supported contributor environment, not used as an unqualified architectural acceptance gate.

12. **The plan includes a personal absolute path and `file://` reference.** A durable plan intended for Git and cross-device use should use repository-relative references and never depend on one contributor's machine path.

### Critique of `plan_grok.md`

#### Recommendations worth adopting

- The plan correctly frames stale callbacks as object-level authorization failures, not merely UX defects.
- PAT-only authentication, an auth TTL, an explicit warning on credential-message deletion failure, private-only administration, and custom-CA preference are valuable hardening measures.
- `reconciliation_required` and a searchable Jira marker correctly recognize that POST timeout outcomes differ from ordinary retryable failures.
- The L1-L14 invariant list is a strong basis for test names and merge gates.
- Shared fakes, a fake clock, standard-library `unittest` continuity, an offline-only developer loop, and characterization-before-refactor are practical improvements.
- The plan correctly preserves the human confirmation gate, lazy VPN behavior, Ubuntu 24.04 boundary, and prohibition on live Jira calls in CI.

#### Technical flaws and regression risks

1. **The state diagram permits an unsafe terminal escape from uncertainty.** It shows `reconciliation_required -> cancelled`. Once a create outcome is unknown, “cancelled” can falsely imply that no Jira issue exists. The workflow may be marked `ABANDONED_UNKNOWN` only with an explicit unresolved warning and retained reconciliation record; preferably it remains nonterminal until reconciled to created or definitely not created.

2. **The failure categories are still too coarse.** `pending | success | failed | ambiguous_timeout` does not distinguish definite rejection, safe-to-retry transport failure, outcome-unknown mutation, authorization failure, validation conflict, and provider-contract failure. Retryability and mutation certainty must be separate typed dimensions. Not every HTTP 5xx is automatically safe to retry after a create.

3. **Workflow ID plus owner/chat is not the full callback binding.** Add the preview message ID, optional Telegram message-thread ID, expected revision/state, expiry, and one-shot action record. Otherwise a copied/replayed token within the same chat can still act against a later rendering of the entity.

4. **Credential encryption is proposed too early and too generically.** Encryption at rest protects copied disks/backups only if the deployment key is stored separately; it does not reduce exposure to a compromised running process that can read both ciphertext and key. A new `crypto.py` risks home-grown cryptography, nonce misuse, irreversible migrations, and startup outages. First fix store atomicity, strict schema/corruption behavior, permissions, backup, and PAT-only scope. Then adopt a vetted AEAD/envelope format with key versioning, rotation, recovery, and rollback based on an approved threat model and root-managed key source.

5. **A bot-wide allowlist and private-only mutations are product decisions, not substitutes for object authorization.** They are useful defense-in-depth defaults for a private pilot, but the required group/private scope remains an explicit user decision. The workflow model should remain safe even if authorized group and message-thread use is later enabled.

6. **The proposed structured logs include raw `telegram_user_id`.** Stable user IDs can be personal identifiers and are unnecessary for most performance metrics. Prefer an opaque workflow correlation ID and fixed event/outcome fields. If actor correlation is operationally required, define access, retention, and possibly a keyed pseudonym rather than logging raw IDs by default.

7. **Characterization tests must not canonize known vulnerabilities.** Tests that merely freeze generic callback names or current insecure behavior can obstruct the fix. Characterize unaffected parsing/rendering and external contracts, while known defects should receive target-invariant regression tests that initially fail (or are explicitly marked pending) until the remediation lands.

8. **Property-style and fuzz testing need a concrete dependency policy.** The repository currently uses only `unittest`. Random loops can miss schedules and produce irreproducible failures; Hypothesis would be a new development dependency. Start with table-driven grammar tests and deterministic interleavings using barriers/fake clocks, then approve a property-testing tool with fixed reproduction output if it adds material value.

9. **The fake-analyzer environment switch could leak into production behavior.** `DZTGBot_DEV_FIXTURES=1` expands runtime configuration and creates a dangerous alternate provider path. Prefer dependency injection in tests or a separate local harness that production wiring cannot select accidentally.

10. **The media recommendations conflict slightly.** One section suggests optionally adding photo inputs to `analysis.py`, while early non-goals defer full multimodal processing. The first hardening release should reject or label unsupported content honestly; multimodal transfer should be a separately approved privacy/cost project.

11. **The credential-store proposal misses a verified current consistency bug.** In-memory credentials are changed before persistence succeeds. Encryption alone would preserve that defect. Copy-on-write/transactional commit and corrupt-store recovery must precede format encryption.

12. **The current ordinary-photo and confirm-path defects are missed.** The plan catches the batch-cap photo issue but not that any ordinary photo can contaminate a later draft, nor that reply-keyboard confirmation currently turns editing off and exits silently. Both need first-batch regression tests.

13. **The final file contains a stray closing parenthesis.** This is editorial rather than architectural, but it illustrates the value of Markdown/lint checks for durable planning artifacts.

### Conflicting suggestions and resolved positions

| Topic | Antigravity | Grok | Refined Codex position |
|---|---|---|---|
| Production draft repository | In-memory or SQLite | SQLite WAL recommended | SQLite (or another approved transactional durable store) is mandatory for mutation workflows; in-memory is test-only. Verify WAL against the target local filesystem. |
| Domain validation | Pure dataclasses, avoid Pydantic where possible | Workflow models plus boundary validation | Use standard-library frozen domain value objects and transition logic, with strict Pydantic DTOs at Gemini/Jira/persistence boundaries. Map explicitly so provider schemas cannot become domain authority. |
| Callback identity | Truncated UUID in callback | Workflow ID in callback | Use a full-strength opaque random token mapped server-side to entity, actor, chat/thread, preview message, action, revision/state, and expiry. |
| Batching timer | PTB JobQueue | Application lifecycle task | Prefer `Application.create_task` plus an injectable cancellable deadline for this short debounce; use JobQueue only if its optional dependency and shutdown semantics are deliberately adopted. |
| Jira timeout | Retryable failure | Reconciliation required | Unknown outcome; never issue a second create until reconciled. Concurrent locking is necessary but not idempotency. |
| Exit from unknown create | Not modeled | Can cancel | Do not claim cancellation. Retain an unresolved attempt, or explicitly mark `ABANDONED_UNKNOWN` with continued duplicate-warning/reconciliation capability. |
| Credential hardening | Atomic file permissions | Encrypt early | PAT-only, timeout, deletion warning, strict schema, copy-on-write, backup/recovery first; vetted encryption second after threat-model/key-lifecycle approval. |
| Group behavior | Supports owner/chat binding | Private-only by default | Private-only is the safest interim mode, but final group/thread support is a human product decision. Core authorization must be safe in either mode. |
| Test sequencing | Architecture first, tests in final phase | Characterization first | Tests and deterministic fakes precede structural moves; known defects get target-invariant regressions, not frozen insecure expectations. |
| Code-quality metric | Maximum file size | Modular layout/DX | Enforce dependency and behavior contracts, strict typing for new modules, complexity limits where justified, and invariant coverage—not byte counts. |

### Refinements to the original Codex recommendations

The peer plans improve the original proposal in several ways, so the following refinements supersede any ambiguity in earlier sections:

1. **Choose a sharper domain/boundary split.** Domain state and transitions should use frozen standard-library types and explicit constructors. Pydantic remains the strict validation layer for Gemini, Jira, configuration, callback payloads, and persisted DTOs. This avoids binding business logic to provider validation while retaining one canonical set of domain constraints.

2. **Make test doubles a first-class architecture deliverable.** Add shared `FakeClock`, deterministic ID/token generator, `FakeTelegramPort`, `FakeAnalyzer`, `FakeJiraGateway`, `FakeVpnManager`, and in-memory repository. The production repository remains SQLite; the in-memory implementation is explicitly a test adapter.

3. **Add private-admin and auth controls to the early security batch.** Private-only `/rules`, `/setrules`, `/vpn`, and `/vpnstart`, PAT-only input, auth timeout, and credential deletion-failure warning are bounded changes with high security value. Whether all mutation workflows become private-only remains a product decision.

4. **Adopt custom-CA-first TLS policy.** Keep `JIRA_VERIFY_SSL` only as an exceptional compatibility escape hatch, emit a privacy-safe startup warning when disabled, and document migration to a root-managed custom CA bundle.

5. **Separate correlation from identity.** Generate an opaque correlation ID distinct from the database primary key and callback token. Do not log Telegram user IDs, callback tokens, issue details, file IDs, raw errors, or content by default.

6. **Refine the unknown-outcome lifecycle.** `SUBMISSION_UNKNOWN` is not retryable, cancellable, or expirable in the ordinary sense. Retention cleanup must never delete it automatically. Only verified reconciliation may produce `CREATED` or a definite-not-created retry state; supervised abandonment must preserve the unresolved warning and marker.

7. **Require a callback action record rather than trusting encoded fields.** Callback data stays below 64 bytes by carrying version, short action code, and opaque token. Mutable authorization data lives in the repository and one-shot actions are consumed transactionally.

8. **Avoid a production fake-mode environment flag.** Offline UI demonstrations should use a separate development entry point or test composition root with explicit fake dependencies, never a secret runtime switch in the production application.

9. **Qualify SQLite WAL and encryption advice.** The supported server's local `/var/lib` filesystem is the intended database location; do not place the runtime database in a synchronized checkout. Verify WAL, backup, restore, permissions, disk-full behavior, and migration rollback there. Use only a vetted encryption library/format with an external versioned key if encryption is approved.

10. **Keep performance work in the foundation, not a distant polish phase.** Shared Jira connection pooling, one VPN check per logical operation, event-driven debounce, bounded Gemini concurrency, total deadlines, attachment budgets, and rules signature caching should land alongside their refactored adapters because their interfaces determine lifecycle and error behavior.

### Consolidated first implementation batch

Subject to the explicit approvals already listed in this plan, the cross-audit supports this order:

1. Add deterministic handler fakes and target-invariant regressions for stale callbacks, cross-chat batching, out-of-order analysis, ordinary-photo contamination, batch-cap attachment leakage, reply-keyboard confirm, lost draft, double confirm, and unknown Jira outcome.
2. Add domain entities, legal transition table, typed error/mutation-certainty model, full-strength token generator, callback action records, and repository protocols.
3. Add the durable SQLite workflow/attempt repository with revision compare-and-swap, migrations, retention rules, restart recovery, and test-only in-memory adapter.
4. Route Telegram callbacks through actor/chat/thread/message/revision authorization and temporarily set stateful update concurrency to one.
5. Replace polling batching and raw tasks with application-owned, workflow-keyed deadlines and tasks; keep all external I/O outside locks/transactions.
6. Add Jira payload parity, strict response models, reusable connection pooling, metadata validation, no silent fallback, attempt persistence, external idempotency marker, and unknown-outcome reconciliation.
7. Add attachment records and independently retryable bounded transfers without re-creating the issue.
8. Add PAT-only auth, timeout, deletion warning, private-only admin commands, credential copy-on-write/corruption safety, and custom-CA-first TLS behavior.
9. Enable keyed concurrency only after deterministic conflict/replay/shutdown tests prove the invariants, then add quotas, circuit breakers, privacy-safe metrics, and measured tuning.

The performance and security tracks should not be implemented as separate rewrites. Repository transactions, callback authorization, task ownership, reusable transports, and typed error certainty form one correctness boundary and should be reviewed together at their interfaces.
