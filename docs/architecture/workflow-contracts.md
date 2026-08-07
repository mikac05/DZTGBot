# DZTGBot Workflow Contracts & System Architecture

**Document Owner**: Antigravity (Software Architecture & Structural Cleanliness Lead)  
**Reference**: `MASTER_PLAN.md` (Phase 0 Task P0-A)  
**Date**: August 2026  

---

## 1. Architectural Principles & Boundaries

DZTGBot is an asynchronous Telegram bot for Taiwan team Jira management. It converts forwarded Telegram messages into structured Jira task drafts via Gemini AI, provides interactive preview rendering for human approval, and submits confirmed tasks to Jira Server / Data Center REST API v2 over NetworkManager L2TP/IPsec VPN.

### Core Non-Negotiable Boundaries:
1. **Private-Only First Release**: Authentication, draft intake, inline callbacks, Jira issue creation/updates, and administrative commands are strictly private-chat-only.
2. **Explicit Human Confirmation**: No Jira issue is created or updated automatically without explicit inline human approval (`[✅ Confirm Submit]`).
3. **Local-Disk SQLite Authority**: Production workflow state, draft revisions, callback tokens, submission attempts, attachment status, and published issue mappings reside in a local SQLite WAL database outside the Git checkout.
4. **Lazy VPN Operation**: VPN connection checks and startup occur lazily before Jira operations. VPN connections are not automatically established at bot startup.
5. **Offline-Only CI & Test Safety**: Automated unit and integration tests run entirely offline with synthetic mocks and fakes. Live Telegram, Gemini, Jira, or VPN mutations are strictly forbidden in CI.

---

## 2. Layered Architecture & Dependency Rules

DZTGBot enforces strict unidirectional Clean Architecture layer dependencies:

```
+-----------------------------------------------------------------------+
|                         Presentation Layer                            |
|        dztgbot.ui (Handlers, Formatters, Keyboards, Commands)         |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
|                         Application Layer                             |
|    dztgbot.services (WorkflowService, IntakeService, Submission)      |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
|                            Domain Layer                               |
|       dztgbot.domain (Models, FSM, Protocols, Callback Envelopes)     |
+-----------------------------------------------------------------------+
                                   ^
                                   |
+-----------------------------------------------------------------------+
|                        Infrastructure Layer                           |
|      dztgbot.infrastructure (SQLite, JiraAdapter, GeminiAdapter)      |
+-----------------------------------------------------------------------+
```

### Dependency Rules:
- `ui -> services -> domain`
- `infrastructure` implements domain & service ports (`Protocol` interfaces).
- **Domain Layer (`dztgbot.domain`)**: Must contain pure Python logic only. **Zero external framework dependencies** (no `telegram`, `httpx`, or provider SDK imports).
- **Services Layer (`dztgbot.services`)**: Orchestrates domain workflows. Must not import `telegram` or `telegram.ext`.
- **Infrastructure Layer (`dztgbot.infrastructure`)**: Encapsulates external APIs (httpx, google-genai, sqlite3, nmcli). SDK exceptions are caught and wrapped in domain exceptions before propagating upward.

---

## 3. Aggregate Root & Callback Token Lifecycle

### 3.1 `Draft` Aggregate Root
Each forwarded message batch or manual draft creates a unique `Draft` aggregate root identified by a 128-bit random `workflow_id` (UUIDv4).

- **Identity**: `workflow_id`, `owner_id`, `chat_id`.
- **State**: Canonical `DraftState` lives **only** in `domain/fsm.py` (re-exported from `domain` and used by `models.Draft`). Values include `collecting`, `analyzing`, `analysis_failed`, `review`, `editing`, `submitting`, `submission_retryable`, `submission_unknown`, `created`, `attaching`, `attachment_partial`, `complete`, published-update states, `cancelled`, `expired`, and `abandoned_unknown`. Do **not** reintroduce a second reduced enum (legacy names `reviewing` / `failed_retryable` are retired).
- **Revision Control**: Monotonic integer `revision` incremented on every legal state transition to enforce optimistic concurrency control.

### 3.2 Callback Token Security Schema
Inline button callbacks use compact, cryptographically secure tokens:

```
Format:  j1:<short_action>:<opaque_token>
Example: j1:cfm:a7f9b2c4e1d38560f1e2d3c4b5a67890
```

- **Token Construction**: `opaque_token` contains at least 128 bits of cryptographic randomness.
- **Hashing**: Only the SHA-256 hash of `opaque_token` is stored in the database.
- **Verification Matrix**: Before any callback action is executed, the system verifies:
  1. Actor `user_id` matches `owner_id`.
  2. Chat `chat_id` matches bound private chat.
  3. `opaque_token` hash exists in `WorkflowRepository` and is not expired or consumed.
  4. Current `DraftState` and `revision` match the target transition guard.

---

## 4. Mutation Recovery & Idempotency Rules

1. **Pre-Dispatch Attempt Persistence**: Before any Jira create or update HTTP request is sent, a `SubmissionAttempt` record is written to SQLite with state `SUBMITTING`.
2. **Failure Handling**:
   - **Definite Failure** (HTTP 4xx/500): Draft state transitions to `FAILED_RETRYABLE`. The draft payload remains preserved in SQLite, allowing the user to click `[Retry]` or `[Cancel]`.
   - **Ambiguous Timeout / Disconnect** (Network timeout, 502/503/504, process termination): Draft state transitions to `SUBMISSION_UNKNOWN`.
3. **`SUBMISSION_UNKNOWN` Invariants**:
   - Automatic re-creation is strictly forbidden.
   - `SUBMISSION_UNKNOWN` records are excluded from automatic expiry or cleanup tasks.
   - Requires explicit human or administrative reconciliation before any subsequent action.

---
