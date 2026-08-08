# DZTGBot Provider Orchestration Boundaries & Dependency Isolation

**Document Owner**: Antigravity (Software Architecture & Structural Cleanliness Lead)  
**Reference**: `MASTER_PLAN.md` (Phase 4 Task P4-A)  
**Date**: August 2026  

---

## 1. Executive Summary & Purpose

This document establishes the authoritative provider orchestration boundaries for DZTGBot Phase 4. It defines how external services (Jira Server/Data Center REST API v2, Google Gemini AI API, NetworkManager L2TP/IPsec VPN, Telegram Bot API, SQLite persistence) are isolated behind pure Python `Protocol` ports defined in `dztgbot.domain.ports`.

### Non-Negotiable Boundary Rules:
1. **Infrastructure Adapters Never Call Telegram**: `dztgbot.infrastructure` modules encapsulate provider SDKs (httpx, google-genai, sqlite3) and must NEVER import `telegram`, `telegram.ext`, or present user-facing UI.
2. **Domain & Services Never Import Provider SDKs or Telegram**: `dztgbot.domain` and `dztgbot.services` modules must NEVER import `telegram`, `telegram.ext`, `httpx`, `google`, `google.genai`, `sqlite3`, or `pydantic`.
3. **Exception Leak Containment**: Provider-specific exceptions (`httpx.HTTPError`, `google.genai.errors.APIError`, `sqlite3.Error`, `ValidationError`) stop inside infrastructure adapters and MUST be caught and mapped to `ClassifiedOperationError` / `DomainError` domain taxonomy.
4. **Pure Protocol Dependency Injection**: Application services depend exclusively on abstract `Protocol` ports defined in `dztgbot.domain.ports` or service-local protocols, enabling full offline testability and isolated component replacement.

---

## 2. Layered Component & Orchestration Map

```
+-----------------------------------------------------------------------------------+
|                              PRESENTATION LAYER (ui)                              |
|   dztgbot.ui (Handlers, HTML Formatters, Inline Keyboards, Reply Keyboard cleanup) |
+-----------------------------------------------------------------------------------+
                                          |
                                          v  (Calls Application Use-Cases)
+-----------------------------------------------------------------------------------+
|                             APPLICATION SERVICES (services)                        |
|   dztgbot.services (WorkflowService, IntakeService, CallbackService,              |
|                      SubmissionService, AttachmentService, ConnectivityService)   |
+-----------------------------------------------------------------------------------+
                     |                                         |
                     v (Uses Domain Types & FSM)               v (Invocations via Protocols)
+-------------------------------------------------+   +-----------------------------+
|               DOMAIN LAYER (domain)             |   |        DOMAIN PORTS         |
|   dztgbot.domain (Models, FSM, Protocols,       |   |    dztgbot.domain.ports     |
|                   Errors, Callbacks, Policy)    |   |  (JiraGatewayPort,          |
+-------------------------------------------------+   |   AIAnalyzerPort,           |
                         ^                            |   DraftRepositoryPort,      |
                         | (Implements Ports)         |   VpnManagerPort, etc.)     |
+-------------------------------------------------+   +-----------------------------+
|           INFRASTRUCTURE LAYER (infrastructure)                  ^
|   dztgbot.infrastructure.jira_gateway (JiraGatewayPort / httpx) |
|   dztgbot.infrastructure.gemini_gateway (AIAnalyzerPort / genai) |
|   dztgbot.infrastructure.persistence (SQLiteWorkflowRepository)  |
|   dztgbot.user_store (Copy-on-write 0600 UserStore)              |
|   dztgbot.vpn (NetworkManager L2TP/IPsec VPN)                    |
+------------------------------------------------------------------+
                                  ^
                                  | (Wires concrete implementations into ports)
+-----------------------------------------------------------------------------------+
|                           COMPOSITION ROOT (__main__)                             |
|   dztgbot.__main__ (Process lifecycle, config validation, container wiring)       |
+-----------------------------------------------------------------------------------+
```

---

## 3. Exact Orchestration Boundaries

### 3.1 Presentation Layer (`dztgbot.ui`)
- **Responsibility**: Parses incoming Telegram updates/callbacks, invokes application use-cases, and renders user-visible HTML responses.
- **Allowed Inbound**: Receives `telegram.Update` and `telegram.ext.ContextTypes` from python-telegram-bot framework.
- **Allowed Outbound**: Calls application services (`WorkflowService`, `IntakeService`, `CallbackService`, etc.) and domain formatters.
- **Forbidden Inbound/Outbound**: Must NEVER import `httpx`, `google`, `sqlite3`, `dztgbot.infrastructure`, or directly call Jira, Gemini, or SQLite databases.

### 3.2 Application Services (`dztgbot.services`)
- **Modules**:
  - `WorkflowService`: Manages `Draft` aggregate root creation, edits, manual state transitions, and revision validation.
  - `IntakeService`: Batching, deduplication, content budget validation, and automated AI analysis trigger.
  - `CallbackService`: Validates 128-bit cryptographic callback tokens, checks user authorization & state guards, and executes one-shot actions.
  - `SubmissionService`: Pre-dispatch attempt persistence, failure-preserving Jira REST API create/update mutations, canonical hash generation, and unknown-outcome reconciliation.
  - `AttachmentService`: Idempotent attachment staging, budget checks, deduplication, and single-attachment retry without recreating issues.
  - `ConnectivityService`: Handles lazy VPN checks, single-flight locking, and positive TTL caching.
- **Boundary Contract**: Services depend purely on `dztgbot.domain` models, `dztgbot.domain.fsm`, `dztgbot.domain.errors`, and `typing.Protocol` ports.
- **Forbidden Inbound/Outbound**: Must NEVER import `telegram`, `telegram.ext`, `httpx`, `google.genai`, `sqlite3`, or concrete infrastructure implementations (`jira_gateway.py`, `workflow_sqlite.py`).

### 3.3 Domain Model & Ports (`dztgbot.domain`)
- **Modules**: `models.py`, `fsm.py`, `ports.py`, `errors.py`, `callbacks.py`, `policy.py`.
- **Boundary Contract**: Contains 100% pure Python standard-library types (`dataclass`, `Enum`, `StrEnum`, `datetime`, `Protocol`, `uuid`). Zero external library or framework dependencies.
- **Port Protocols Defined**:
  - `JiraGatewayPort`: Abstraction for Jira Server/Data Center REST API v2.
  - `AIAnalyzerPort`: Abstraction for Gemini AI draft extraction.
  - `DraftRepositoryPort`: Abstraction for SQLite durable workflow storage.
  - `VpnManagerPort`: Abstraction for NetworkManager VPN operations.
  - `ClockPort`, `IdGeneratorPort`, `TaskSchedulerPort`, `UserRepositoryPort`, `RulesRepositoryPort`, `RendererPort`.

### 3.4 Jira Gateway Adapter (`dztgbot.infrastructure.jira_gateway`)
- **Responsibility**: Implements `JiraGatewayPort` for Jira Data Center / Server REST API v2 operations.
- **Encapsulation**:
  - Maintains single process-lifecycle `httpx.AsyncClient` connection pool with bounded limits.
  - Injects Bearer token per-request (`Authorization: Bearer <PAT>`); never sets auth headers globally on client sessions.
  - Formats canonical Jira payloads (`JiraIssueFields`) for issue creation, updating, and diffing.
  - Enforces distinct connect, read, write, and pool timeouts.
  - Limits error response body extraction to 16 KiB (`MAX_ERROR_BODY_BYTES`) to prevent memory exhaustion attacks.
  - Caches project/field metadata per-scope with a bounded TTL (`DEFAULT_METADATA_TTL_SECONDS`).
  - Maps all `httpx.HTTPError`, `httpx.TimeoutException`, and HTTP 4xx/5xx status codes to `ClassifiedOperationError`.
- **Forbidden**: Must NEVER call Telegram APIs, import Telegram types, or leak `httpx` exceptions to callers.

### 3.5 Gemini Gateway Adapter (`dztgbot.infrastructure.gemini_gateway`)
- **Responsibility**: Implements `AIAnalyzerPort` for natural language conversion into canonical `JiraTaskTemplate`.
- **Encapsulation**:
  - Wraps Google Gemini API client SDK (`google.genai`).
  - Validates Gemini JSON outputs against strict Pydantic model `GeminiResponse` (`extra="forbid", strict=True`).
  - Enforces message character budgets, prompt size limits, and single end-to-end timeout deadlines.
  - Detects rate limits (HTTP 429 / `RESOURCE_EXHAUSTED`) and falls back gracefully across model candidates.
  - Enforces media boundaries (unsupported binary attachment types are rejected before calling Gemini).
  - Translates `ValidationError`, `google.genai.errors.APIError`, and timeouts to `ClassifiedOperationError` / `GeminiGatewayError`.
- **Forbidden**: Must NEVER call Telegram APIs, import Telegram types, or leak `google.genai` SDK exceptions.

### 3.6 Persistence Adapters (`dztgbot.infrastructure.persistence`, `dztgbot.user_store`)
- **Responsibility**:
  - `SQLiteWorkflowRepository`: Implements `DraftRepositoryPort` using SQLite WAL mode, foreign keys, schema migrations, SHA-256 token hashing, atomic revision compare-and-swap (CAS), and attempt claims.
  - `UserStore`: Implements copy-on-write atomic JSON persistence for encrypted/protected Jira credentials on disk with restricted permissions (`0600`).
- **Forbidden**: Must NEVER call Telegram APIs or leak `sqlite3.Error` or `OSError` without domain exception handling.

### 3.7 Connectivity & VPN Adapter (`dztgbot.services.connectivity_service`, `dztgbot.vpn`)
- **Responsibility**: Manages lazy NetworkManager L2TP/IPsec VPN connection checks via `NetworkManagerL2tpManager`.
- **Encapsulation**: Incorporates single-flight async locking and positive TTL caching to avoid subprocess storming under concurrent requests.
- **Forbidden**: Must NEVER import Telegram or expose raw shell/subprocess outputs to user UI.

### 3.8 Composition Root (`dztgbot.__main__`)
- **Responsibility**: Single application entry point responsible for:
  1. Validating runtime configuration (`dztgbot.config.Settings`).
  2. Initializing logging, database connection pools, and single process-lifecycle HTTP clients.
  3. Instantiating infrastructure adapters (`SQLiteWorkflowRepository`, `JiraGateway`, `GeminiGateway`, `NetworkManagerL2tpManager`).
  4. Injecting adapters into application services (`WorkflowService`, `IntakeService`, `SubmissionService`, `AttachmentService`, `ConnectivityService`).
  5. Wiring application services into Telegram UI handlers and starting python-telegram-bot application runtime.
- **Legacy Cutover Note**: During Phase 4/5 cutover, legacy `core.py` serves as a temporary compatibility facade until Phase 6 full composition-root migration.

---

## 4. Provider Exception Containment Taxonomy

Infrastructure adapters translate all raw library/SDK exceptions into domain exception classifications before propagating to application services:

| External Exception Source | Infrastructure Adapter | Domain Exception Taxonomy Mapping | Classification Metadata |
| :--- | :--- | :--- | :--- |
| `httpx.ConnectTimeout`, `httpx.ReadTimeout` | `JiraGateway` | `ClassifiedOperationError` | `kind=TIMEOUT`, `certainty=UNKNOWN`, `retryability=AFTER_RECONCILIATION` |
| `httpx.HTTPStatusError` (401/403) | `JiraGateway` | `ClassifiedOperationError` | `kind=AUTHENTICATION_FAILED`, `certainty=NOT_MUTATED`, `retryability=NEVER` |
| `httpx.HTTPStatusError` (400/422) | `JiraGateway` | `ClassifiedOperationError` | `kind=INVALID_PAYLOAD`, `certainty=NOT_MUTATED`, `retryability=NEVER` |
| `httpx.HTTPStatusError` (404) | `JiraGateway` | `ClassifiedOperationError` | `kind=NOT_FOUND`, `certainty=NOT_MUTATED`, `retryability=NEVER` |
| `httpx.HTTPStatusError` (409) | `JiraGateway` | `ClassifiedOperationError` | `kind=STATE_CONFLICT`, `certainty=NOT_MUTATED`, `retryability=POSSIBLE` |
| `httpx.HTTPStatusError` (500/502/503) | `JiraGateway` | `ClassifiedOperationError` | `kind=PROVIDER_ERROR`, `certainty=UNKNOWN`, `retryability=AFTER_RECONCILIATION` |
| `pydantic.ValidationError` | `GeminiGateway` | `GeminiGatewayError` | `kind=INVALID_PAYLOAD`, `certainty=NOT_MUTATED`, `retryability=NEVER` |
| `google.genai.errors.APIError` (429) | `GeminiGateway` | `GeminiGatewayError` | `kind=RATE_LIMITED`, `certainty=NOT_MUTATED`, `retryability=POSSIBLE` |
| `sqlite3.OperationalError` (busy) | `SQLiteWorkflowRepository` | `StateConflictError` / `RevisionConflictError` | `kind=LOCK_CONFLICT`, `certainty=NOT_MUTATED`, `retryability=POSSIBLE` |
| `sqlite3.IntegrityError` (unique constraint) | `SQLiteWorkflowRepository` | `RevisionConflictError` | `kind=STATE_CONFLICT`, `certainty=NOT_MUTATED`, `retryability=NEVER` |

---

## 5. Architectural Verification & Import Rules

Automated architecture tests in `tests/test_architecture_dependencies.py` continuously enforce these dependency rules via Abstract Syntax Tree (AST) analysis:

1. **Domain Isolation**: `dztgbot.domain` imports zero modules outside standard library Python.
2. **Services Isolation**: `dztgbot.services` imports only `dztgbot.domain` and standard library Python.
3. **Infrastructure Isolation**: `dztgbot.infrastructure` never imports `telegram` or `dztgbot.ui`.
4. **UI Isolation**: `dztgbot.ui` never imports `dztgbot.infrastructure`, `httpx`, `google`, or `sqlite3`.
5. **Cycle Detection**: Zero import cycles among `dztgbot.domain`, `dztgbot.services`, and `dztgbot.infrastructure`.
