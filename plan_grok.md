# Grok proposal: Security, logic verification, and developer experience

**Author role:** Security / Logic Verification / Developer Experience (multi-agent track)  
**Source of truth:** `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md` plus current tree under `src/dztgbot/`  
**Scope of this document:** Proposal only. No application code, config, or other docs are modified by this write-up.  
**Date:** 2026-08-07  

---

## 1. Executive summary

DZTGBot is a credible **private, serial-use pilot**: human confirmation before Jira create, hardened systemd packaging, atomic local stores, and privacy-conscious global error logging. It is **not yet production-safe** for multi-chat, multi-user, or business-critical Jira mutation.

From a security and correctness lens, the highest-risk defects are not missing features—they are **authorization and identity gaps in mutable workflow state**:

1. Generic Telegram callbacks act on the caller’s latest in-memory draft / `last_published`, not the object the button displayed.
2. Draft and batch state is scoped per Telegram user, not per chat or per workflow.
3. Concurrent update processing is enabled while `ConversationHandler` and shared `user_data` assume sequential mutation.
4. Jira create **drops the draft before the network call**, so failures lose retry context and timeouts can produce duplicates.
5. Credentials accept passwords and session cookies, store secrets in a single plaintext JSON file, and auth has no timeout.

This proposal prioritizes **security boundaries**, **logic invariants with tests**, and **developer experience** that makes those invariants enforceable. It is designed to complement parallel tracks (workflow redesign, UX, ops) without requiring them to re-litigate the audit.

**Recommended stance:** Treat remediation as a **workflow-identity redesign**, not seventeen isolated patches to `core.py`. Ship characterization tests first, then durable workflow IDs + FSM + fail-closed callbacks, then mutation recovery, then auth/credential hardening.

---

## 2. Method and evidence boundary

| Evidence | Status |
|---|---|
| Audit report (2026-08-07) | Primary product/risk inventory |
| Current source (`core.py`, `jira_auth.py`, `jira_client.py`, `user_store.py`, `admin.py`, `__main__.py`, tests) | Confirms audit claims still apply |
| Offline unit tests (~30) | Cover settings, forwards filter helpers, rules/store, VPN CLI construction, preview/parser, model fallback mock, privacy error handler—not full handler journeys |
| Live Telegram / Gemini / Jira / VPN / systemd | **Not** revalidated in this proposal |

When README or the end-to-end test plan disagree with source (e.g. “Jira not created yet”), **source wins**. Documentation drift is a DX and ops risk, not a substitute for security fixes.

---

## 3. Security proposal

### 3.1 Critical: object-level authorization for mutations

**Problem.** Inline buttons use generic `callback_data` values (`jira_confirm`, `jira_edit`, `jira_editpublished`, etc.). Handlers resolve targets from `context.user_data["pending_template"]` or `last_published`. An older button can create, cancel, or edit the **wrong** draft or Jira issue. There is no binding of actor + chat + draft/issue identity on the callback.

**Impact.** Unauthorized or unintended Jira create/update relative to user intent; cross-chat leakage for the same Telegram user; stale UI that still mutates production Jira.

**Proposal.**

- Assign a unique `workflow_id` (and later `issue_key` for published edits) to every draft and submission.
- Encode `workflow_id` (and short action) in every `callback_data`.
- On every callback: load workflow → verify `owner_user_id` and `chat_id` → verify allowed status transition → apply change transactionally.
- Reject foreign, stale, or wrong-state callbacks with a clear, non-leaky message (no stack traces, no draft contents in logs).
- Prefer optimistic concurrency (`revision`) so two concurrent handlers cannot both “win” silently.

**Security control class:** object-level authorization + CSRF-like binding for UI actions (Telegram-equivalent of “don’t trust the button label alone”).

### 3.2 Critical: credential surface reduction (PAT-only, timeout, delete integrity)

**Problem (verified in code).**

- `jira_auth.py` invites PAT, `user:password`, and `JSESSIONID` cookies.
- `jira_client._make_client` implements Basic, Bearer, and Cookie auth.
- Credential message deletion failures are swallowed (`except Exception: pass`).
- No conversation timeout: later ordinary text (including menu presses or forwards while stuck in auth) can be treated as a secret and deleted.
- Concurrent updates + `ConversationHandler` increase mis-routing risk during auth.

**Impact.** Broader credential types increase phishing surface and logging/exfil risk; passwords/cookies have different lifecycle and revocation semantics; silent delete failure leaves secrets in chat history; stuck auth can eat unrelated messages.

**Proposal.**

- **PAT-only** parse path in auth and client; reject Basic and Cookie formats with a safe error.
- **Auth TTL** (e.g. 2–5 minutes); on expiry, leave conversation and tell the user.
- If Telegram message delete fails: **warn the user** to delete the message manually; log event type only (not content).
- Restrict credential collection to private chat (already partly done); add optional `TELEGRAM_ALLOWED_USER_IDS` allowlist for defense-in-depth.
- `/logout` removes local store only—document that Jira-side revocation is the user’s responsibility; do not claim remote revoke.

### 3.3 High: credential storage encryption and secret hygiene

**Problem.** `UserStore` persists all users’ tokens in one JSON file (`jira_pat` plaintext) with mode `0600` and atomic replace. Good host permissions, weak application-layer secrecy: disk backup, mis-copied state dir, or process memory dumps expose every PAT.

**Proposal.**

- Encrypt secret fields at rest with a **deployment key outside Git** (env / root-only file).
- Keep file mode `0600`, non-symlink, service-account-only access (existing deploy model).
- Schema version + migration from plaintext JSON if present.
- Never log PATs, Basic headers, cookies, forwarded message text, generated descriptions, or VPN endpoints (partially already policy; attachment path currently logs Telegram `file_id` and exception text—tighten).
- Tests and handoff scripts must continue to reject secret-like tracked content.

### 3.4 High: Jira mutation integrity (idempotency and silent substitution)

**Problem (verified).**

- On confirm, `pending_template` is **popped before** `create_issue`; failure loses draft and offers no Retry.
- Timeouts are ambiguous: Jira may have committed while the bot returns failure; user retry can duplicate.
- On rejected issue type, client **silently retries with `Task`** without new human approval.
- Update path removes publish target state before success; partial field PUT; no reload/diff of live Jira.

**Impact.** Lost work; duplicate production issues; issue type diverges from user-approved preview; concurrent human edits on Jira can be overwritten.

**Proposal.**

- Persist **submission attempts** before the HTTP call: status `pending | success | failed | ambiguous_timeout`.
- Retain draft on failure → status `retryable_failure` with Retry/Cancel.
- On timeout: enter `reconciliation_required`; **never blind re-create**. Prefer a searchable external marker (custom field / issue property) if available; otherwise require human resolve after probe.
- **Never** silently change issue type, project, or priority after user confirmation; surface Jira error and keep draft.
- Published edit: issue key in callback payload; reload issue; field-level diff; confirm; durable retry.

### 3.5 High: group and admin information disclosure

**Problem.**

- `/start` can show Jira display name/username in a group.
- Admin `/rules` and `/setrules` work in groups and can dump full runtime rules (classification policy, project keys) into a multi-user chat.
- Reply keyboards are not chat-selective; group members may see controls not meant for them.
- No bot-wide allowlist: any Telegram user who can message the bot may start drafts (create gated by auth, but Gemini cost and data exfil to Google still apply).

**Proposal.**

- Default **mutating and admin commands to private-chat-only**.
- Optional allowlist for who may invoke the bot at all.
- Avoid PII (Jira names) in group replies.
- Rules view/replace only in private admin chat; add later: versioning, diff preview, rollback (audit item 16).

### 3.6 Medium: media and data-minimization honesty

**Problem.** Intake normalizes media types; Gemini receives labels only (no bytes). Only photos upload to Jira. Captionless images produce empty analysis input while UX implies media is handled. Forwarded content is sent to Gemini under runtime rules—users are not given a privacy/retention explanation.

**Proposal.**

- Either send photo bytes/parts to Gemini for supported types, or **reject unsupported / empty-media** before “Analyzing…”.
- Document data flow: Telegram → Gemini → (on confirm) Jira via VPN.
- Avoid logging file IDs and exception strings that embed user content; log exception **type** and workflow correlation id only.
- Do not expand media types until security review of size limits and virus/upload abuse.

### 3.7 Medium: concurrency as a security and safety property

**Problem.** `TELEGRAM_CONCURRENT_UPDATES` defaults to 4. Combined with mutable `user_data` and `ConversationHandler`, this is a race condition class, not only a UX bug: two confirms, auth vs forward interleaving, batch worker vs cancel.

**Proposal.**

- Process stateful updates **sequentially per user or per workflow key**.
- Register background batch/Gemini work on the Telegram application lifecycle (not orphan `asyncio.create_task`).
- Cap concurrent analyses per user/global early (abuse + cost control).

### 3.8 Medium: TLS and network trust

**Problem.** `JIRA_VERIFY_SSL` can be disabled for self-signed certs behind VPN. Combined with PAT storage, MITM on a compromised host path is high impact.

**Proposal.**

- Prefer pinned/custom CA over `verify_ssl=false`.
- If verify is disabled, log a startup **WARNING** once (no secrets); document risk in ops runbook.
- Keep lazy VPN; do not auto-start full tunnel from unattended paths without existing `VPN_ALLOW_START` controls.

### 3.9 Lower but real: host-side strengths to preserve

Do **not** regress:

- Root-owned `0600` environment file outside checkout.
- Non-root systemd service, hardened unit, journald-only logging model.
- Narrow VPN sudoers generated only when `VPN_ALLOW_START=true`.
- Placeholder rejection for `TODO_` secrets at startup.
- Handoff/secret-boundary validation before Git sync.

These are strong. Application-layer workflow identity must rise to match them.

### 3.10 Security backlog (ranked)

| Rank | Item | Severity | Audit ref |
|---|---|---|---|
| S1 | Workflow IDs + callback binding + actor/chat checks | Critical | 1, 4 |
| S2 | Fail-closed stale buttons; revision concurrency | Critical | 1, 2 |
| S3 | Auth TTL + PAT-only + delete-failure warning | High | 5 |
| S4 | Encrypt credentials at rest | High | 10 |
| S5 | Submission attempts + no silent issue-type swap + reconciliation | High | 3, 6 |
| S6 | Private-only admin/mutations; optional user allowlist | High | 5, 8, 16 |
| S7 | Sequential/keyed concurrency; lifecycle tasks | Medium–High | 4 |
| S8 | Media honesty + log redaction for attachments | Medium | 7 |
| S9 | TLS verify / CA discipline | Medium | ops |
| S10 | Quotas / backpressure | Medium | 9 |
| S11 | Privacy/retention/support commands | Low–Medium | 17 |

---

## 4. Logic verification proposal

### 4.1 Invariants that must become true

These are the **logic contracts** the codebase should enforce and tests should prove. Today most are false or untested.

| ID | Invariant |
|---|---|
| L1 | Two drafts from the same user cannot overwrite each other’s workflow state. |
| L2 | Different chats never share draft/batch/publish state. |
| L3 | A callback cannot mutate a workflow owned by another user or chat. |
| L4 | A callback for a terminal or mismatched status is a no-op with user feedback. |
| L5 | Restart preserves recoverable workflows (review, retryable_failure, reconciliation_required). |
| L6 | Failed Jira create retains draft and attempt history. |
| L7 | Ambiguous create timeout cannot produce an automatic second create. |
| L8 | Attachment failures are visible and independently retryable without re-creating the issue. |
| L9 | Auth conversation expires and does not consume unrelated later messages as credentials. |
| L10 | Unsupported or empty media is rejected before analysis claims success. |
| L11 | Published edit targets the issue key on the button, not `last_published` alone. |
| L12 | Rate limits / queue full fail gracefully without corrupting workflow status. |
| L13 | Logs, metrics, and test fixtures never contain message text, PATs, or VPN secrets. |
| L14 | User-confirmed issue type/project/priority are not silently replaced on create. |

### 4.2 Explicit state machine (logic model)

Introduce a single durable status field with **allowed transitions only**:

```text
collecting → analyzing → review → submitting → created
                         ↘ retryable_failure ⇄ submitting   (explicit Retry)
                         ↘ cancelled | expired
submitting → reconciliation_required → created | retryable_failure | cancelled
created → editing_published → update_review → updating → created
```

Every handler path:

1. Resolve `workflow_id` (message context or callback).
2. Authorize actor + chat.
3. Assert transition legal for current status.
4. Commit state change in one transaction (SQLite WAL recommended).
5. Perform side effects (Gemini/Jira) with attempt rows recorded **before** external I/O where mutations occur.

### 4.3 Current logic gaps mapped to code

| Gap | Current behavior (summary) | Desired |
|---|---|---|
| Single-slot draft | `pending_template` one per user | Many workflows with IDs |
| Batch race | `pending_batch` + lock in `user_data`, raw `create_task` | Workflow-scoped batch + app task |
| Confirm drops draft | `pop` then `create_issue` | Persist attempt; pop only on success or move to failed with payload retained |
| Silent type fallback | `jira_client` retries as `Task` | Return error; keep `review` |
| Publish slot | `last_published` one per user | Issue key on callback + durable association |
| Auth open-ended | `AWAITING_PAT` until cancel/success | TTL + cancel |
| Help vs behavior | Help claims free-form text intake | Align help or implement guided-only consistently |
| Manual project hardcode | Audit notes `NGSSA3` in manual path | Use rules/default project config consistently |
| Validation split | AI drafts skip fields validation used for manual edits | One validator for all create paths |
| Photo before cap | Photo appended before 20-message check | Cap then attach |
| Partial attachments | Upload loop logs and continues | Record per-attachment status; report to user |

### 4.4 Verification strategy (logic, not live Jira)

**Phase V0 — Characterization (no behavior change)**

- Handler-level tests with mocked `Application` / `Update` that freeze current behavior for:
  - forward vs reply-to-forward vs ordinary ignore
  - callback names present
  - unauthenticated preview buttons
  - admin reject for non-admin
- Purpose: refactor safety net (audit item 13).

**Phase V1 — Invariant suite (must gate merges)**

| Test module (proposed) | Proves |
|---|---|
| `tests/test_workflow_fsm.py` | Illegal transitions raise; matrix complete |
| `tests/test_workflow_repository.py` | Isolation, revision conflict, restart load |
| `tests/test_callback_authz.py` | Foreign/stale/wrong-state callbacks rejected |
| `tests/test_jira_orchestration.py` | Retain draft on fail; no double create on timeout; no silent type swap |
| `tests/test_auth_security.py` | PAT-only; TTL; delete-failure warning path |
| `tests/test_media_gates.py` | Unsupported media rejected; photo path mocked |
| `tests/test_privacy_logging.py` | Extend existing: no file content, no PAT in logs |

**Mocks only:** Jira, Gemini, Telegram network, VPN nmcli. CI must never create real issues.

**Phase V2 — Property-style checks**

- Fuzz callback_data strings (wrong length, foreign ids, replay after cancel).
- Concurrent simulated updates on same `workflow_id` (asyncio tasks) → one winner, consistent status.
- Crash mid-submit: recovery leaves `reconciliation_required` or `retryable_failure`, never “lost draft with unknown Jira”.

### 4.5 Missing features from the audit (logic/product completeness)

These are audit gaps that double as security/logic requirements; not pure UX polish.

| Audit # | Missing feature | Verification need |
|---|---|---|
| 1 | Durable unique drafts | L1–L4 |
| 2 | Explicit FSM | Transition tests |
| 3 | Mutation recovery + idempotency | L6–L7 |
| 4 | PTB-compatible concurrency + task lifecycle | Race tests |
| 5 | Auth timeout, PAT-only, delete warning | L9 |
| 6 | Live Jira metadata / no silent substitution | L14 + createmeta mocks |
| 7 | Real multimodal or honest reject | L10 |
| 8 | Accurate help, keyboards, private interactions | Snapshot/assert UX strings in private-only mode |
| 9 | Quotas / backpressure | L12 |
| 10 | Transactional store + stronger secrets | L5 + encryption round-trip tests |
| 11 | Correct published-issue editing | L11 |
| 12 | Observability (correlation ids) | Assert ids present without content |
| 13 | Handler-level tests | This entire section |
| 14 | Split `core.py` | Import graph / unit boundaries for testability |
| 15 | Doc reconciliation | Doc-test or checklist in CI optional |
| 16 | Admin governance | Private-only + version tests later |
| 17 | Privacy/support paths | Command presence tests |

### 4.6 Recommended first logic ship set

Do not expand product features before:

1. Characterization tests (V0).
2. Workflow ID + repository + FSM.
3. Callback authorization (L3–L4).
4. Create path retain-on-failure + timeout reconciliation (L6–L7).
5. Handler tests for L1–L7.

This matches the audit’s recommended first batch and is the minimum **logic floor** for continued multi-agent work.

---

## 5. Developer experience proposal

### 5.1 Pain points today

| Pain | Why it hurts multi-agent work |
|---|---|
| `core.py` is a god-module (intake, UI, state, Jira, attachments) | Conflicts, hard reviews, hard unit tests |
| State lives in informal `user_data` keys | No schema, no migration, no discoverability |
| README / E2E plan / deploy notes lag source | Agents and humans follow wrong procedures |
| Tests stop at helpers | Refactors are fear-driven; regressions slip |
| Windows venv `pip check` noise | False “environment broken” signal for contributors |
| Secrets and private VPN paths are correctly out of Git | Good—but onboarding needs a clear “local loop without Jira” story |
| No workflow correlation id in logs | Debugging production issues is guesswork |

### 5.2 Target modular layout (for testability and ownership)

Suggested split (names illustrative):

```text
src/dztgbot/
  intake.py              # forwards, batching, media normalize
  workflow/
    models.py
    fsm.py
    repository.py        # SQLite WAL
    service.py
  rendering.py           # previews, keyboards, HTML
  jira_orchestration.py  # create/update/reconcile
  attachments.py
  jira_auth.py           # PAT-only, TTL
  jira_client.py         # thin HTTP
  user_store.py          # encrypted secrets
  analysis.py
  admin.py / vpn.py / config.py / __main__.py
```

**DX rule:** handlers stay thin; business rules live in pure-ish services with injectable clocks, stores, and HTTP clients.

### 5.3 Local developer loop (offline-first)

Document and support a path that needs **no** real Telegram/Gemini/Jira:

```text
python -m venv .venv
pip install -r requirements.txt
PYTHONPATH=src python -m unittest discover -s tests -v
```

Add (proposal):

- `make test` / `scripts/dev_test.ps1` for Windows and Linux parity.
- Optional `DZTGBot_DEV_FIXTURES=1` for deterministic fake analyzer (if introduced).
- Explicit note: real `.env` is gitignored; use `.env.example` keys only.
- Recreate venv guidance when `pip check` fails on Windows platform markers.

### 5.4 Testing DX

- Prefer **stdlib unittest** (already project convention) unless team standardizes on pytest later—do not mix without decision.
- Provide shared fakes under `tests/fakes/` (`FakeJira`, `FakeGemini`, `FakeUserStore`, in-memory workflow repo).
- One command must run the invariant suite in CI and on deploy script preflight (deploy already runs offline tests—keep that).
- Characterization tests should be marked or named so agents know they freeze legacy behavior until deleted after cutover.

### 5.5 Configuration and operational DX

| Improvement | Rationale |
|---|---|
| Reconcile `GEMINI_MODEL` in deploy vs app auto model queue | Stops false deploy requirements |
| Document `WORKFLOW_DB_PATH`, encryption key, auth TTL | New surfaces need names before code lands |
| Startup checklist in README: required vs optional env | Reduces misconfiguration |
| Structured log fields: `workflow_id`, `telegram_user_id`, `event`, `exception_type` only | Operable without privacy regression |
| Migration/rollback runbook for SQLite | Multi-agent deploys need a shared recovery story |
| “Do not claim live verification” rule in AGENTS.md | Already present—keep enforced |

### 5.6 Documentation DX (truthfulness)

Priority fixes (content-only later; not in this file’s edit scope beyond naming them):

1. README: Jira create/update **is** implemented with human confirmation (remove obsolete “not implemented”).
2. End-to-end test plan: include `/auth`, `/new`, confirm/retry paths, private-chat policy once adopted.
3. PROJECT_CONTEXT: point at workflow redesign when implemented; keep evidence boundaries.
4. Single “Current architecture” section agents must read first (already AGENTS.md order—keep current).

### 5.7 Multi-agent collaboration hygiene

For Grok / Antigravity / Codex working in parallel:

| Rule | Reason |
|---|---|
| Do not expand features before S1–S5 / L1–L7 | Avoid building on sand |
| Land characterization tests before large `core.py` moves | Merge conflict reduction |
| One agent owns workflow package; one owns auth/secrets; one owns docs | Clear boundaries |
| No secrets in plans, PRs, or handoff narratives | Existing project law |
| Prefer interfaces (`WorkflowRepository` protocol) early | Parallel implementation without blocking |
| Feature flags only if temporary dual-write needs them | Prefer short cutover window |

### 5.8 Developer experience backlog (ranked)

| Rank | Item |
|---|---|
| D1 | Characterization + invariant test harness and fakes |
| D2 | Split `core.py`; workflow package with clear APIs |
| D3 | Fix README/test-plan contradictions |
| D4 | Cross-platform test script + venv repair notes |
| D5 | Privacy-safe structured logging with correlation ids |
| D6 | Migration/rollback developer runbook |
| D7 | Optional local fake analyzer for UI dry-runs |
| D8 | Admin rule versioning UX for operators (later) |

---

## 6. Integrated remediation map (security × logic × DX)

Aligned with a phased redesign (compatible with a 9-phase product plan; this map is the **control-focused** view).

| Phase | Security outcome | Logic outcome | DX outcome |
|---|---|---|---|
| 1. Safety baseline | No behavior change | Characterization tests lock behavior | Safe refactor runway |
| 2. Workflow foundation | Durable identity for authz | FSM + SQLite + models | Split modules; repository API |
| 3. Telegram correctness | Fail-closed callbacks; keyed concurrency | L1–L4 true | Callback contract documented |
| 4. Reliable mutations | No lost draft; no blind duplicate; no silent type swap | L6–L8, L14 | Orchestration module isolated |
| 5. Auth + capability | PAT-only, TTL, encryption, allowlist | L9 | Clear auth error catalog |
| 6. Media + interaction | Honest media; private mutations | L10 | Help matches code |
| 7. Published edits | Correct issue targeting | L11 | Diff preview testable |
| 8. Production controls | Quotas, metrics without content | L12–L13 | Health/readiness for ops |
| 9. Docs + rollout | Verified only when live-tested | Runbook matches reality | Agents stop rediscovering |

**Preserve always:** human confirmation before create; lazy VPN; no real Jira in automated tests; Ubuntu 24.04 deploy gate; secret non-commitment.

---

## 7. Concrete code-change inventory (proposal only — do not implement from this file alone)

Listed for planning and comparison with other agents. **Not an authorization to edit.**

### 7.1 New

- `src/dztgbot/workflow/` (`models`, `fsm`, `repository`, `service`, migrations)
- `src/dztgbot/intake.py`, `rendering.py`, `jira_orchestration.py`, `attachments.py`
- `src/dztgbot/crypto.py` (credential field encryption)
- `src/dztgbot/quota.py`, `observability.py` (later phases)
- `tests/fakes/`, `tests/test_workflow_*.py`, `tests/test_callback_authz.py`, `tests/test_jira_orchestration.py`, `tests/test_auth_security.py`
- Ops notes: migration/rollback (future doc)

### 7.2 Modify

- `core.py` — hollow out / replace with thin handlers
- `__main__.py` — wire services, concurrency policy, lifecycle tasks
- `config.py` / `.env.example` — DB path, encryption key, TTL, allowlist, quotas
- `jira_auth.py` — PAT-only, TTL, delete warning, private-only
- `user_store.py` — encryption, schema version
- `jira_client.py` — remove silent Task fallback; metadata/probe helpers; safer errors
- `analysis.py` — shared validation; optional photo inputs
- `admin.py` — private-only; later governance
- `scripts/deploy.sh` / systemd unit — DB dir, key requirement, drop obsolete env checks
- `tests/test_bot.py` — keep pure unit tests; avoid bloating further

### 7.3 Explicit non-goals (early)

- WireGuard or VPN redesign
- Creating Jira issues without confirmation
- Full multimodal for all media types
- Real Jira calls in CI
- Force-push / secret migration into Git
- Claiming live validation without environment proof

---

## 8. Success criteria for this track

This multi-agent track is **done** when:

1. **Security:** Callbacks cannot mutate foreign/stale workflows; credentials are PAT-only, time-bounded, encrypted at rest; create path cannot silently alter approved fields or blind-duplicate on timeout.
2. **Logic:** Invariants L1–L14 are encoded in automated tests that pass offline; FSM is the only legal transition table.
3. **DX:** New contributors and agents can run the full offline suite in one command; architecture is modular enough that a single PR need not touch a 1k-line handler file; docs match create/auth reality.

Until then, treat the bot as **pilot-grade**: trusted serial private use only.

---

## 9. Immediate next action (for human / orchestrator)

1. Compare this file with peer agent plans (workflow redesign, UX, ops).
2. Lock product assumptions: private-chat-only mutations, PAT-only, SQLite WAL, encrypted credentials.
3. Authorize **Phase 1 only** (characterization tests) or the full foundation sequence.
4. Do not implement application changes until that approval is explicit.

---

## 10. References

- `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md` — full audit
- `docs/context/PROJECT_CONTEXT.md` — architecture and constraints
- `docs/context/CONTINUE_HERE.md` — recorded next action post-audit
- `AGENTS.md` — handoff/continue and non-negotiable boundaries
- Primary code: `src/dztgbot/core.py`, `jira_auth.py`, `jira_client.py`, `user_store.py`, `admin.py`, `__main__.py`

---

## Cross-Audit Critiques & Rebuttals

**Peers reviewed:** `plan_antigravity.md` (Architecture & Structural Cleanliness), `plan_codex.md` (Code quality, performance, edge cases).  
**Method:** Compare each plan against the 2026-08-07 audit, current source behavior, and this track’s security / logic / DX priorities.  
**Scope:** Critique and refinement only. Peer plan files and application source were not modified.

### Summary judgment

| Plan | Strength | Primary risk if adopted alone |
|---|---|---|
| **Antigravity** | Clear layered target architecture, DI wiring, presentation/domain split, sequence diagrams | Big-bang package rewrite; incomplete FSM for ambiguous Jira outcomes; weak security defaults; in-memory drafts as acceptable path |
| **Codex** | Deepest defect inventory beyond the audit; strongest invariants, recovery model, and test matrix | Scope and tooling bar may delay the security floor; product security decisions left open rather than locked |
| **Grok (this file)** | Security ranking, authz invariants, credential threat model, phased control gates | Thinner on package layout detail and some concrete resource bugs Codex found |

**Consensus that holds across all three (retain):**

1. Workflow/draft identity with owner + chat binding.  
2. Explicit FSM instead of ad-hoc `user_data`.  
3. Callbacks must name a specific entity, not “latest draft.”  
4. Do not discard drafts before confirmed Jira outcomes; retain retry context.  
5. Split the `core.py` monolith; keep domain logic free of Telegram types.  
6. Human confirmation and lazy VPN stay.  
7. Automated tests must not create real Jira issues.

The disagreements are mostly about **depth of the first cut**, **persistence defaults**, **callback token design**, and **whether security policy is assumed or optional**.

---

### Critiques of `plan_antigravity.md`

#### What is strong

- **Layered package map** (`domain` / `services` / `infrastructure` / `ui`) is a good long-term DX target and maps cleanly onto multi-agent ownership.
- **CallbackEnvelope** concept is right; encoding action + entity id is the minimum fix for stale-button risk.
- **Repository + adapter patterns** and constructor DI in `__main__.py` improve testability without inventing a heavy framework.
- **SUBMITTING as an idempotency lock** is a correct, simple double-click control.
- **JobQueue / owned timers** instead of bare `asyncio.create_task` matches PTB lifecycle needs.
- Sequence diagrams make the intended create/failure path reviewable by non-authors.

#### Technical flaws and missed requirements

1. **Truncated draft IDs (`uuid4()[:8]`) are a security and collision defect.**  
   Eight hex characters (~32 bits) are too short for unguessable UI tokens and will collide under volume or adversarial probing. Prefer full UUID, 128-bit opaque tokens, or a server-side lookup token (see Codex). Do not put guessable short ids alone in `callback_data` without server-side authz.

2. **FSM omits ambiguous-outcome states.**  
   Antigravity’s diagram maps Jira timeout → `FAILED_RETRYABLE` with Retry. That is **unsafe**: a timeout after Jira may have committed must become `SUBMISSION_UNKNOWN` / `reconciliation_required`, not a free second create. Codex and this plan treat blind retry as a critical logic failure. Antigravity’s state enum also lacks attachment-partial, editing-published, and analysis-failed paths the audit requires.

3. **In-memory draft repository as a first-class Phase 2 option undercuts restart recovery.**  
   The audit’s L5-class requirement is that recoverable workflows survive process restart. Shipping `InMemoryDraftRepository` as the default “done” path reintroduces pilot fragility. In-memory is fine as a **test double**, not as production persistence.

4. **No characterization / failing-test baseline before the rewrite.**  
   Phases start with domain packages, not with pinning current behavior. That raises regression risk on subtle bugs Codex lists (photo contamination, batch cap ordering, reply-keyboard no-op). Architecture-first rewrites without red tests are a known failure mode on 40 KB handler files.

5. **Security surface is under-specified.**  
   Missing or weak relative to audit + this plan: PAT-only auth, auth conversation TTL, credential-message delete-failure UX, encryption at rest, optional user allowlist, private-chat-only admin/mutations, group PII leakage on `/start`, silent issue-type → `Task` removal. Architecture alone does not close credential or disclosure threats.

6. **Callback envelope lacks revision / one-shot semantics.**  
   `dft:<id>:confirm` without revision (or opaque single-use token) still allows races: two parallel confirms, or confirm after edit revision N+1 while button still says N. Codex’s CAS on `(draft_id, revision, expected_state)` and optional opaque tokens are stricter and correct.

7. **No preview `message_id` / chat binding in the envelope path.**  
   Owner check alone is insufficient for group or multi-device edge cases. At minimum enforce `owner_user_id` + `chat_id`; ideally also bind the preview message for confirm/edit (Codex). Antigravity stores `chat_id` on Draft but does not spell enforcement on every callback.

8. **Domain model redefines `JiraTaskTemplate` with shape drift.**  
   Example: `acceptance_criteria` as `str` vs current list-of-strings Pydantic model; field renames (`issue_type` vs `issuetype`). Without an explicit migration from the existing validated model, adapters will lose fields or break tests. Prefer **keeping one validated template type** and wrapping it, not forking.

9. **VPN decoupling needs an explicit non-regression.**  
   Moving VPN out of `JiraClient` is good layering, but lazy ensure-connected **must** remain on the submission path. The plan implies this in the sequence diagram; it should be an acceptance criterion, not an incidental refactor.

10. **Global `concurrent_updates(False)` as the only concurrency story.**  
    Safe as a temporary brake; long-term it serializes unrelated users and hurts the private multi-user pilot. Keyed serialization (Codex/Grok) is the scalable correctness model.

11. **Acceptance metrics skew cosmetic.**  
    “No file > 15 KB” is a weak quality bar. Prefer invariant tests, no network-in-transaction, and authz coverage. File size is a smell signal, not a security gate.

12. **Big-bang package tree risks multi-agent thrash.**  
    Introducing `domain/`, `services/`, `infrastructure/`, `ui/` in one campaign while other agents land SQLite and tests invites conflict. Prefer **incremental extraction** that can later rename into that tree.

#### Potential regressions if Antigravity is implemented as written

- Timeouts retried as ordinary failures → **duplicate Jira issues**.  
- Short draft ids → **cross-user probe / collision**.  
- In-memory drafts in prod → **lost work and lost reconciliation state** on systemd restart.  
- Template field rename without migration → **broken create payloads**.  
- Architecture merge without characterization → **silent loss of edge-case behavior** (e.g. photo attach outside edit).

#### Rebuttal to possible counter-arguments

- *“Clean architecture first makes tests easy later.”*  
  True only if interfaces land with **tests in the same phase**. Domain-first without red tests still allows incorrect transition tables to ship green.
- *“In-memory is enough for a pilot.”*  
  This pilot already runs under systemd with restarts. Restart loss of `SUBMITTING`/`UNKNOWN` is exactly when duplicates happen.
- *“Eight-char ids fit Telegram’s 64-byte limit.”*  
  The limit is not the bottleneck; a full UUID (36 chars) plus short action still fits. Opacity and length are not in conflict.

---

### Critiques of `plan_codex.md`

#### What is strong

- **Best empirical defect list** beyond the audit: ordinary-photo contamination, batch-size-after-photo, batch worker inactive-before-Gemini, lock held across Telegram I/O, UserStore mutate-before-persist, rules full-file reread, per-request `httpx` client, untyped Jira JSON, global Gemini model selection races.
- **Correctness invariants** are precise enough to become test names; especially “no I/O inside workflow lock” and shutdown safety.
- **SUBMISSION_UNKNOWN + marker-based reconciliation** is the right model for ambiguous creates; refuses automatic blind retry.
- **Opaque callback tokens** resolved server-side reduce leakage and support one-shot consumption better than embedding full domain ids.
- **Revision + compare-and-swap** is the right concurrency primitive for dual confirms and stale edits.
- **Characterization Phase 0** (failing tests first) is the correct engineering order—this plan’s strongest process contribution.
- **Resource bounds** (prompt size, attachment pipeline, Gemini semaphore, rules signature cache, shared HTTP pool) prevent DoS/cost blowups that pure architecture plans ignore.
- **Shared payload builder** for create/update/preview closes a real field-parity bug.
- **Strict validation and typed errors** align with security logging goals (safe user messages, no provider body logging).

#### Technical flaws, overreach, and tensions

1. **Scope can delay the security floor.**  
   Ruff + strict mypy + 90% branch coverage + property tests + circuit breakers + performance measurement, if treated as a single gate before ship, postpone PAT-only, encryption, and callback authz. Tooling should **track** migration, not block the first invariant fixes.

2. **Product security decisions are left optional.**  
   PAT-only, private-chat-only, and encrypted credentials appear under “decisions required” rather than **default locked assumptions**. From a security track view, those should be recommended defaults unless the user explicitly rejects them; open decisions invite the status quo (password/cookie auth, group admin rules dump).

3. **Credential encryption is deferred more than ideal.**  
   Codex correctly prioritizes copy-on-write consistency for JSON stores—adopt immediately—but encryption at rest should not wait for “approved design” indefinitely. A minimal design (env-held key, Fernet or equivalent, encrypt only `jira_pat`) is enough for Phase 5 security.

4. **`preview_message_id` binding can over-constrain UX.**  
   Binding confirm to the exact preview message is excellent against cross-message attacks; it can confuse users if the bot edits the same message in place (usually OK) or resends a new preview without invalidating old buttons (must invalidate). Implementation must **expire old tokens** when a new preview is rendered—Codex says this but it is easy to miss in a partial port.

5. **WAL on OneDrive / non-local filesystems.**  
   The user’s Windows checkout lives under OneDrive. SQLite WAL on synced folders is a known foot-gun. Production Ubuntu local disk is fine; **dev default** should prefer a local non-synced path or DELETE journal for portable dev, and document “do not put the DB on cloud sync.”

6. **Performance work mixed into the critical path narrative.**  
   Connection pools and rules mtime cache are high-value and low-risk; circuit breakers and latency SLOs are later. Sequencing is mostly correct (Phase 4), but the executive tone sometimes equates performance with correctness—reviewers must not prioritize pool tuning over S1 authz.

7. **Property-based / repeated schedule testing** is valuable but heavy for a small private bot. Prefer a **curated concurrency matrix** first; add Hypothesis-style generators only for FSM transition legality if cost allows.

8. **Admin / group disclosure and allowlisting** are thinner than this security plan. Codex’s handler tests mention wrong user/chat; they should also ban admin rules dump and credential prompts outside private chat as first-class cases.

9. **Silent type fallback removal** is correct; pairing it with live createmeta in the same phase can block progress if Jira is unreachable. Sequence as: (a) remove silent fallback immediately, (b) metadata validation when Jira reachable, (c) offline allow-list from rules as interim.

10. **Shared `httpx.AsyncClient` must not become a credential mixing bug.**  
    Codex warns to use per-request auth headers—good. Acceptance tests must prove User A’s PAT never appears on User B’s request if someone later sets default headers by mistake.

#### Potential regressions if Codex is implemented carelessly

- Over-strict message_id checks without token rotation → **valid users see “stale” after every preview refresh**.  
- Full quality gate before dual-write cutover → **long-lived hybrid `user_data` + SQLite** with two sources of truth.  
- Aggressive Gemini circuit breaker → **total analysis outage** after a brief 429 if cooldown is global and sticky.  
- SQLite on synced path during Windows dev → **mysterious database locks / corruption**.

#### Rebuttal to possible counter-arguments

- *“Without strict typing and 90% coverage the redesign is unsafe.”*  
  Unsafe is **untested authz and mutation recovery**. Coverage on transition/callback/orchestration modules should be high; whole-repo 90% and strict mypy on legacy in one go is optional polish.
- *“Opaque tokens are more complex than draft_id in callback_data.”*  
  Complexity is justified for one-shot confirm and avoiding id enumeration. A middle ground is acceptable: `j1:<action>:<draft_id>:<revision>` with server-side owner/chat/state checks—if draft_id is high-entropy UUID.
- *“Leaving PAT-only open preserves compatibility.”*  
  Compatibility with passwords/cookies is exactly the expanded attack surface the audit flagged. Opt-in legacy modes, if ever needed, must be explicit and off by default.

---

### Conflicts between Antigravity and Codex (and resolution)

| Topic | Antigravity | Codex | Resolution (security/logic track) |
|---|---|---|---|
| First step | Domain packages | Characterization tests | **Codex**: red tests first |
| Draft id in callback | Short id in clear | Opaque token | **High-entropy id or opaque token**; never 8-char |
| Timeout handling | Retryable failure | SUBMISSION_UNKNOWN + reconcile | **Codex** |
| Persistence | In-memory or SQLite | SQLite required for prod workflows | **SQLite for prod**; memory only in tests |
| Concurrency | Often global serial | Keyed serial + concurrent I/O | **Keyed** after temporary global serial |
| Architecture depth | Full clean architecture tree | Incremental split of `core.py` | **Incremental**, evolve toward Antigravity layout |
| Tooling | Light (tests pass) | Ruff/mypy/coverage CI | **Adopt gradually**; gate new modules first |
| Security policy | Implicit | Optional decisions | **Lock** private mutations, PAT-only, encrypt secrets |
| Performance | Secondary | Major section | **After** identity + recovery invariants |

No peer plan proposes removing human confirmation or lazy VPN—**no conflict** there. No peer proposes real Jira in CI—**aligned**.

---

### Critiques of this plan (`plan_grok.md`) after peer review

Self-critique so the refined recommendations stay honest:

1. **Under-specified package layout.** Antigravity’s tree is clearer for long-term ownership; this plan’s flat `workflow/` + `intake.py` is fine for Phase 2 but should name an evolution path to `domain`/`services`/`ui`.
2. **Missed several concrete bugs Codex found.** Especially: photo append outside edit mode; batch worker releasing active flag before analysis; UserStore memory/disk ordering; lock held across Telegram sends; rules reread cost.
3. **Callback design** said “include workflow_id” but did not require revision or one-shot tokens strongly enough.
4. **Did not stress “no network inside DB transaction”** as a hard invariant (Codex L8)—adopt it.
5. **Quality tooling** was light; some of Codex’s gates are worth adopting for *new* modules without boiling the ocean.

---

### Refined recommendations (update to original Grok stance)

These replace or extend the earlier phase guidance for this track. They do not authorize implementation by themselves.

#### R1. Locked assumptions (unless the user explicitly overrides)

- Mutating workflows and admin commands: **private-chat-only**.  
- Auth: **PAT-only**, conversation **TTL**, delete-failure **warning**.  
- Workflow store: **SQLite** on local disk (not cloud-sync paths); memory repo = tests only.  
- Credentials: **encrypt secret fields** with deployment key outside Git; fix **copy-on-write** ordering immediately even before encryption.  
- Human confirm + lazy VPN retained.  
- No real Jira/Gemini/Telegram in automated tests.

#### R2. Delivery order (merged)

| Order | Work | Source of truth |
|---|---|---|
| 0 | Characterization + failing regression tests for known defects (incl. Codex photo/batch/store bugs) | Codex Phase 0 + Grok V0 |
| 1 | Domain entities, **full** FSM including `SUBMISSION_UNKNOWN`, revision CAS, typed errors | Codex + Antigravity domain, fixed FSM |
| 2 | SQLite repository, migrations, retention; credential COW (+ encryption as soon as key config exists) | Codex storage + Grok S4 |
| 3 | Callback authz (opaque token **or** UUID+revision), owner/chat/(message) checks, token invalidation on new preview | Codex + Antigravity envelope |
| 4 | Temporary `concurrent_updates(1)` then keyed serialization; owned tasks/JobQueue; debounce batching (no poll loop) | Codex + Antigravity JobQueue |
| 5 | Submission orchestration: attempt rows, no silent type swap, reconcile-before-recreate, attachment status | All three; Codex recovery wins |
| 6 | Auth hardening + private-only admin + optional allowlist | Grok security |
| 7 | Incremental module split → evolve toward Antigravity package layout | Antigravity end-state, Codex pace |
| 8 | Bounds, pools, rules cache, metrics (privacy-safe), then docs | Codex perf + Grok ops |

#### R3. Invariants added/strengthened after cross-audit

Add to L1–L14:

| ID | Invariant |
|---|---|
| L15 | No Telegram/Gemini/Jira/VPN await while holding a workflow DB transaction or per-draft lock. |
| L16 | Credential memory and disk agree after every store/remove (no mutate-before-successful-persist). |
| L17 | Photos and attachments cannot join a draft unless that draft is in an accepting state for that chat. |
| L18 | Batch seal assigns attachments to that batch’s draft only; later batches never inherit them. |
| L19 | Confirm/retry never leaves `SUBMISSION_UNKNOWN` via a second create without reconciliation. |
| L20 | Callback tokens for superseded previews are unusable (consumed or revision-mismatched). |

#### R4. Callback design (refined)

**Minimum acceptable:**

```text
j1:<action>:<draft_uuid>:<revision>
```

with server checks: parse → load draft → owner + chat → revision match → state allows action → CAS transition.

**Preferred:**

```text
j1:<action>:<opaque_token>
```

where `opaque_token` maps to draft, revision, owner, chat, optional preview message id, expiry, one-shot flag.

Reject Antigravity’s 8-character ids.

#### R5. Architecture (refined)

- **Do not** require a full clean-architecture directory move in the first PR.  
- **Do** keep domain/services free of `telegram` imports as modules are extracted (Antigravity acceptance criterion—keep it).  
- Target end-state may match Antigravity’s tree; intermediate may match Codex’s flatter split.  
- Keep a single `JiraTaskTemplate` validation model; do not fork field names without a migration map.

#### R6. Tooling (refined)

- New workflow/orchestration modules: Ruff + strict typing + high branch coverage on FSM/authz/submission.  
- Legacy `core.py`: characterize, then shrink; no big-bang strictness requirement.  
- CI: offline tests on Python 3.12; deploy script keeps running tests.  
- Defer property-based generators until the curated concurrency matrix is green.

#### R7. Explicit “do not do” after cross-audit

- Do not map create timeout → ordinary retryable without reconciliation.  
- Do not ship production drafts as process-memory-only.  
- Do not optimize HTTP pools before callback identity works.  
- Do not silently coerce issue types.  
- Do not treat documentation or “15 KB file size” as security completion criteria.  
- Do not place SQLite databases on OneDrive/synced folders for dev or prod.

#### R8. Multi-agent coordination note

| Concern | Suggested owner lens |
|---|---|
| Package layout & DI | Antigravity |
| Edge cases, recovery, performance bounds, quality gates | Codex |
| Authz, secrets, private-chat policy, invariant gates, threat model | Grok (this plan) |

Merge rule: **Codex recovery/FSM completeness + Grok security locks + Antigravity layering as end-state**, delivered in **Codex/Grok phase order** (tests → model → store → callbacks → mutations → split).

---

### Final position for the orchestrator

1. **Accept** the shared core: durable drafts, FSM, bound callbacks, failure-preserving create, split monolith, no real Jira in CI.  
2. **Reject or amend** Antigravity items: 8-char ids, timeout→simple retry, production in-memory drafts, architecture-before-tests, missing auth security.  
3. **Adopt** Codex’s defect list, Phase 0 tests, `SUBMISSION_UNKNOWN`, CAS revisions, no-I/O-in-lock, resource bounds—**without** making full strict-tooling the gate for the first security fix.  
4. **Keep** this plan’s locked security assumptions (PAT-only, private mutations, encrypted credentials, allowlist option).  
5. **Authorize implementation only after** the user confirms R1 assumptions and Phase 0 (characterization) as the first code change set.

*End of cross-audit section.*
)
