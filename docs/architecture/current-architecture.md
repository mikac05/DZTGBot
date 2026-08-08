# DZTGBot Current Architecture Specification

**Document Owner**: Antigravity (Software Architecture & Structural Cleanliness Lead)  
**Reference**: `MASTER_PLAN.md` (Phase 8 Task P8-A)  
**Date**: August 2026  

---

## 1. Architectural Overview & Dependency Graph

DZTGBot follows strict Clean Architecture and layered dependency inversion principles. All workflow state, authentication boundaries, external provider interactions, and Telegram UI interactions adhere to unidirectional dependency rules.

### 1.1 Layer Isolation Hierarchy

```text
+-----------------------------------------------------------------------------------+
|                              PRESENTATION LAYER (ui)                              |
|   src/dztgbot/ui/ (Handlers, Keyboards, Formatters, Commands)                     |
+-----------------------------------------------------------------------------------+
                                          |
                                          v  (Calls Application Services)
+-----------------------------------------------------------------------------------+
|                             APPLICATION SERVICES (services)                        |
|   src/dztgbot/services/ (WorkflowService, IntakeService, CallbackService,        |
|                          SubmissionService, AttachmentService, Connectivity,      |
|                          Limits, Observability)                                   |
+-----------------------------------------------------------------------------------+
                     |                                         |
                     v (Uses Domain Models & FSM)              v (Invocations via Protocols)
+-------------------------------------------------+   +-----------------------------+
|               DOMAIN LAYER (domain)             |   |        DOMAIN PORTS         |
|   src/dztgbot/domain/ (Models, FSM, Protocols,  |   |    src/dztgbot/domain/ports |
|                       Errors, Callbacks, Policy)|   |  (JiraGatewayPort,          |
+-------------------------------------------------+   |   AIAnalyzerPort,           |
                         ^                            |   DraftRepositoryPort, etc.)|
                         | (Implements Ports)         +-----------------------------+
+------------------------------------------------------------------+       ^
|           INFRASTRUCTURE LAYER (infrastructure)                  |_______|
|   src/dztgbot/infrastructure/jira_gateway (JiraGatewayPort / httpx)|
|   src/dztgbot/infrastructure/gemini_gateway (AIAnalyzerPort)       |
|   src/dztgbot/infrastructure/persistence (SQLiteWorkflowRepo)      |
|   src/dztgbot/infrastructure/keyed_processor (KeyedProcessor)       |
|   src/dztgbot/user_store (0600 JSON UserStore)                       |
|   src/dztgbot/vpn (NetworkManager L2TP/IPsec)                        |
+------------------------------------------------------------------+
                                  ^
                                  | (Wires concrete implementations into ports)
+-----------------------------------------------------------------------------------+
|                           COMPOSITION ROOT (__main__)                             |
|   src/dztgbot/__main__.py (Process lifecycle, config validation, DI container)    |
+-----------------------------------------------------------------------------------+
```

### 1.2 Dependency Rules
- `ui -> services -> domain`
- `infrastructure` implements domain/service `Protocol` ports defined in `src/dztgbot/domain/ports.py`.
- `domain` contains zero external dependencies (pure Python standard library only).
- `services` imports only `domain` and standard library modules. It never imports `telegram`, `httpx`, `google.genai`, `sqlite3`, or `pydantic`.
- `infrastructure` encapsulates external provider SDKs and sqlite3. SDK exceptions stop inside infrastructure adapters and are caught and mapped to `ClassifiedOperationError` / `DomainError`.
- Architecture dependency constraints are continuously enforced by AST static analysis in `tests/test_architecture_dependencies.py`.

---

## 2. Composition Root & Lifecycle Ownership

`src/dztgbot/__main__.py` serves as the sole, long-running asynchronous composition root and process lifecycle entry point.

### 2.1 Dependency Injection Container & Startup Sequence
1. **Configuration Validation**: Reads and validates `Settings.from_environment()` (`src/dztgbot/config.py`). Validates `WORKFLOW_DB_PATH`.
2. **Infrastructure Initialization**:
   - `RulesStore` (`src/dztgbot/rules.py`): Loads Markdown rules.
   - `UserStore` (`src/dztgbot/user_store.py`): Initializes copy-on-write `0600` credential store.
   - `NetworkManagerL2tpManager` (`src/dztgbot/vpn.py`): Probes initial VPN status (lazy operation).
   - `ConnectivityService` (`src/dztgbot/services/connectivity_service.py`): Wraps VPN manager with single-flight locks and positive TTL caching.
   - `SQLiteWorkflowRepository` (`src/dztgbot/infrastructure/persistence/workflow_sqlite.py`): Opens database, sets WAL mode (`PRAGMA journal_mode=WAL`), enforces foreign keys (`PRAGMA foreign_keys=ON`), and executes schema migrations (`src/dztgbot/infrastructure/persistence/migrations/001_initial.sql`, `002_indexes.sql`).
   - `AsyncTaskScheduler`, `SystemClock`, `UuidIdGenerator` (`src/dztgbot/infrastructure/__init__.py`).
   - `JiraGateway` (`src/dztgbot/infrastructure/jira_gateway.py`): Single process-lifecycle `httpx.AsyncClient` pool with bounded limits and per-request Bearer PAT authentication.
   - `GeminiGateway` (`src/dztgbot/infrastructure/gemini_gateway.py`): `google.genai` wrapper with Pydantic response validation and character/token budget enforcement.
3. **Application Services Instantiation**:
   - `WorkflowService`, `CallbackService`, `SubmissionService`, `AttachmentService`, `IntakeService`.
   - `ResourceLimiter` (`src/dztgbot/services/limits.py`): Bounded semaphores and circuit breakers for Gemini, Jira, and Attachment resources.
   - `SafeMetrics` (`src/dztgbot/services/observability.py`): Privacy-preserving observability counters and timers.
4. **Telegram Application Assembly**:
   - Builds python-telegram-bot `Application` instance.
   - Sets bot commands (`/start`, `/new`, `/auth`, `/logout`, `/help`) in `post_init`.
   - Attaches `KeyedUpdateProcessor` if `telegram_concurrent_updates > 1`, or sets fallback concurrency `1`.
   - Registers handlers: `auth_conv`, `start_h`, `logout_h`, `help_h`, `admin_handlers`, `ui_handlers`.

### 2.2 Graceful Shutdown & Teardown Order
Upon receiving `SIGINT` or `SIGTERM` signals:
1. Stop polling (`updater.stop()`) and stop application runtime (`application.stop()`).
2. Teardown infrastructure and resource managers in exact reverse initialization order:
   - `keyed_processor.close()`
   - `resource_limiter.close()`
   - `scheduler.close()`
   - `gemini_gateway.aclose()`
   - `jira_gateway.aclose()`
   - `workflow_repo.close()`

---

## 3. SQLite Durable Workflow Authority

`SQLiteWorkflowRepository` in `src/dztgbot/infrastructure/persistence/workflow_sqlite.py` is the single authoritative source of truth for workflow identity, callbacks, submission attempts, attachments, and published Jira issues.

### 3.1 Schema & Authorities
- **`drafts` table**: Stores `Draft` aggregate root (`workflow_id`, `owner_user_id`, `chat_id`, `message_thread_id`, `state`, `revision`, `raw_text`, `summary`, `description`, `issue_type`, `priority`, `project_key`, `created_at`, `updated_at`). State transitions are protected by optimistic Compare-And-Swap (CAS) on `revision`.
- **`callback_tokens` table**: Stores `CallbackTokenRecord`. Contains `token_hash` (SHA-256), `draft_id`, `owner_user_id`, `chat_id`, `preview_message_id`, `expected_revision`, `expected_state`, `action`, `expires_at`, `one_shot`, `consumed_at`.
- **`submission_attempts` table**: Persists `SubmissionAttempt` before dispatches to Jira REST API (`attempt_id`, `draft_id`, `revision`, `attempt_number`, `state`, `idempotency_key`, `dispatched_at`, `completed_at`, `outcome_code`).
- **`attachments` table**: Idempotent tracking of file downloads and uploads (`attachment_id`, `draft_id`, `file_id`, `sha256_hash`, `filename`, `mime_type`, `size_bytes`, `state`, `jira_attachment_id`).
- **`published_issues` table**: Tracks created issues (`draft_id`, `issue_key`, `issue_id`, `self_url`, `created_at`).

---

## 4. Provider Gateways & Exception Containment

### 4.1 Jira Gateway (`src/dztgbot/infrastructure/jira_gateway.py`)
- **Transport**: Maintains a single process-lifecycle `httpx.AsyncClient`. Injects Bearer PAT per-request (`Authorization: Bearer <PAT>`).
- **Resource Bounds**:
  - `JiraTimeouts`: connect=10s, read=10s, write=10s, pool=5s.
  - `MAX_ERROR_BODY_BYTES`: 16 KiB limit on error response body extraction.
  - Metadata TTL cache (`DEFAULT_METADATA_TTL_SECONDS`).
- **Exception Containment**: Translates `httpx.HTTPError`, `httpx.TimeoutException`, and HTTP 4xx/5xx responses into `ClassifiedOperationError` with explicit `ErrorKind`, `MutationCertainty`, and `Retryability`.

### 4.2 Gemini Gateway (`src/dztgbot/infrastructure/gemini_gateway.py`)
- **SDK & Validation**: Wraps `google.genai` client. Validates output against strict Pydantic model `GeminiResponse` (`extra="forbid", strict=True`).
- **Boundaries**: Character budget and prompt size validation before calling Gemini. Non-photo binary attachment bytes are rejected before Gemini dispatch.
- **Resilience**: Detects HTTP 429 / `RESOURCE_EXHAUSTED` rate limits and falls back across candidate models.
- **Exception Containment**: Maps `ValidationError`, `google.genai.errors.APIError`, and timeouts to `ClassifiedOperationError` / `GeminiGatewayError`.

---

## 5. UI, Auth, and Admin Boundaries

### 5.1 Presentation UI (`src/dztgbot/ui/`)
- `src/dztgbot/ui/handlers/callbacks.py`: Handles strict `j1:` callback updates.
- `src/dztgbot/ui/handlers/drafts.py`: Handles draft creation and editing.
- `src/dztgbot/ui/rendering.py`: HTML formatting for Telegram cards. Zero raw unescaped user text injection.
- `src/dztgbot/ui/keyboards.py`: Inline callback keyboards and reply keyboard cleanup.

### 5.2 Authentication Boundary (`src/dztgbot/jira_auth.py`, `src/dztgbot/user_store.py`)
- **Private-Only**: Restricted strictly to Telegram private chats.
- **PAT Only**: Rejects password/basic-auth inputs. Validates PAT shape before dispatch.
- **Security & Expiry**: Auth state expires after 3 minutes. Logs warning if Telegram cannot delete credential messages.
- **Storage**: `UserStore` maintains copy-on-write `0600` JSON file storage with atomic replaces (`os.replace`) and quarantine fallback for corrupted files.

### 5.3 Admin Boundary (`src/dztgbot/admin.py`)
- Private-chat-only, restricted to `telegram_admin_user_ids`.
- Controls rules reloading (`/reload_rules`) and VPN status checks (`/vpn_status`).

---

## 6. Keyed Processing & Resource Limits

### 6.1 Keyed Processor (`src/dztgbot/infrastructure/keyed_processor.py`)
- Serializes update processing per workflow (`workflow:<draft_id>`) or per collection actor/chat (`collection:<actor_id>:<chat_id>[:<thread_id>]`).
- Integrates into python-telegram-bot via `KeyedUpdateProcessor` in `src/dztgbot/__main__.py`.
- Guarantees no database transactions or locks are held across slow external network I/O.

### 6.2 Resource Limiter (`src/dztgbot/services/limits.py`)
- Semaphores for `ResourceKind.GEMINI`, `ResourceKind.JIRA`, and `ResourceKind.ATTACHMENT`.
- Configurable global limits, per-actor limits (default max 2), queue size limits, deadlines, zero retry budgets for mutations, and circuit breaker cooldowns (3 consecutive failures -> 5s cooldown).

### 6.3 Observability & Privacy (`src/dztgbot/services/observability.py`)
- Safe metrics collectors (`SafeMetrics`).
- Metrics and log events use opaque correlation IDs and fixed event codes. Excludes user IDs, callback tokens, file IDs, message content, PATs, Jira payloads, and VPN details.

---

## 7. Strict Callback & Credential Security

- **Callback Grammar**: `j1:<short_action>:<opaque_token>`.
- **Token Entropy & Hashing**: `opaque_token` contains 128+ bits of cryptographic randomness. Only SHA-256 hash is stored in SQLite.
- **Verification Chain**: Before executing action, verifies:
  1. `user_id == owner_id`
  2. `chat_id == private_chat_id`
  3. `token_hash` exists in DB, unexpired, and unconsumed.
  4. `DraftState` and `revision` match expected CAS transition.
- **Token Possession Insufficient**: Possession of a callback token hash alone does not grant authorization without matching actor, chat, and state guards.
