# DZTGBot Incremental Migration Record & Quality Standards

**Document Owner**: Antigravity (Software Architecture & Structural Cleanliness Lead)  
**Reference**: `MASTER_PLAN.md` (Phase 8 Task P8-A)  
**Date**: August 2026  

---

## 1. Incremental Architectural Migration

DZTGBot underwent a multi-phase incremental remediation to transition from a pilot prototype into a production-grade single-host application. The migration eliminated implicit state mutations, unowned background tasks, global user storage, and direct external library dependencies inside business logic.

### 1.1 Summary of Migration Phases

| Phase | Milestone | Key Structural Changes | Primary Verification Suite |
| :--- | :--- | :--- | :--- |
| **Phase 0** | Safety Baseline | Characterized existing behavior; added deterministic fakes (`tests/support/`) and frozen architecture contracts (`docs/architecture/`). | `tests/test_handler_characterization.py` |
| **Phase 1** | Domain & FSM Foundation | Extracted frozen domain models (`src/dztgbot/domain/models.py`), FSM states (`domain/fsm.py`), callback grammar (`domain/callbacks.py`), and pure ports (`domain/ports.py`). | `tests/test_domain_models.py`, `tests/test_workflow_fsm.py` |
| **Phase 2** | Durable Workflow Persistence | Replaced in-memory dictionaries with `SQLiteWorkflowRepository` (`src/dztgbot/infrastructure/persistence/workflow_sqlite.py`) and atomic `0600` copy-on-write `UserStore` (`src/dztgbot/user_store.py`). | `tests/test_workflow_repository.py`, `tests/test_user_store_permissions.py` |
| **Phase 3** | Pure Application Services | Created isolated services (`WorkflowService`, `IntakeService`, `CallbackService`, `SubmissionService`, `AttachmentService`, `ConnectivityService`) with zero Telegram or HTTP dependencies. | `tests/test_workflow_service.py`, `tests/test_intake_service.py` |
| **Phase 4** | Provider Gateways | Encapsulated `httpx` in `JiraGateway` (`src/dztgbot/infrastructure/jira_gateway.py`) and `google.genai` in `GeminiGateway` (`src/dztgbot/infrastructure/gemini_gateway.py`). Preserved unknown-outcome recovery. | `tests/test_jira_gateway.py`, `tests/test_gemini_gateway.py` |
| **Phase 5** | Telegram UI Cutover | Extracted presentation logic into `src/dztgbot/ui/`. Implemented strict `j1:` callback authorization and HTML card rendering. | `tests/test_ui_rendering.py`, `tests/test_draft_handler_journeys.py` |
| **Phase 6** | Composition Root Cutover | Rewrote `src/dztgbot/__main__.py` as sole composition root. Made SQLite sole workflow authority. Retired legacy mutation paths. | `tests/test_application_wiring.py` |
| **Phase 7** | Keyed Concurrency & Limits | Added `KeyedProcessor` (`src/dztgbot/infrastructure/keyed_processor.py`), `ResourceLimiter` (`services/limits.py`), and `SafeMetrics` (`services/observability.py`). | `tests/test_keyed_processor.py`, `tests/test_resource_bounds.py` |
| **Phase 8** | Quality & Governance | Implemented Ruff py312 correctness rules, Mypy strict mode, branch coverage gates, and architecture documentation. | `tests/test_quality_configuration.py`, `tests/test_architecture_dependencies.py` |

---

## 2. Compatibility Facades & Non-Authoritative Status

To preserve backwards compatibility during component cutovers without creating dual sources of truth, select legacy modules were retained strictly as non-authoritative facades:

### 2.1 Active Compatibility Facades

1. **`src/dztgbot/core.py`**:
   - **Status**: Non-authoritative delegation facade.
   - **Purpose**: Temporary re-export bridge during Phase 5/6 transition for legacy handlers or imports.
   - **Authority**: Zero workflow, state, or persistence authority. All operations delegate directly to `WorkflowService` and `SQLiteWorkflowRepository`.

2. **`src/dztgbot/analysis.py`**:
   - **Status**: Data contract facade.
   - **Purpose**: Re-exports `JiraTaskTemplate` from `src/dztgbot/domain/models.py`.
   - **Authority**: Prevents field drift between legacy intake structures and canonical domain models. Zero business logic or mutation authority. Tested in `tests/test_legacy_facades.py`.

3. **`src/dztgbot/jira_client.py`**:
   - **Status**: Adapter facade.
   - **Purpose**: Exports `JiraClient` class delegating network operations to `JiraGateway` (`src/dztgbot/infrastructure/jira_gateway.py`).
   - **Authority**: Maps raw provider errors to safe typed errors (`SafeErrorCode.OUTCOME_UNKNOWN`, `VALIDATION_FAILED`). Contains zero state or credential caching. Tested in `tests/test_legacy_facades.py`.

---

## 3. Removed Legacy Constructs & Revision-CAS Semantics

### 3.1 Removed Legacy Constructs

- **`context.user_data["latest_draft"]` & `context.user_data["latest_issue"]`**: Retired. Draft and issue state are strictly referenced by unique `workflow_id` UUIDs in SQLite, eliminating cross-user or cross-chat draft corruption.
- **Unbound Callback Strings**: Retired legacy callback payloads (e.g. `cb_<draft_id>` or plain string actions). Replaced by strict `j1:<short_action>:<opaque_token>` token hashes.
- **Raw `asyncio.create_task`**: Retired all unowned background task dispatches. Replaced by `AsyncTaskScheduler` with injectable clocks, explicit exception logging, and cancellation cleanup.

### 3.2 Monotonic Revision CAS Semantics

Workflow updates use optimistic concurrency control via monotonic integer revisions:

```sql
UPDATE drafts
SET state = :new_state,
    revision = revision + 1,
    summary = :summary,
    description = :description,
    updated_at = :updated_at
WHERE workflow_id = :workflow_id
  AND revision = :expected_revision;
```

- If `revision` matches `:expected_revision`, the update succeeds and increments `revision`.
- If `revision` does not match (due to concurrent modification), SQLite returns 0 affected rows, raising `RevisionConflictError`.
- Eliminates database locks during external HTTP calls while preventing lost updates or race conditions.

---

## 4. Architectural Completion Rules vs. File Size

### 4.1 Why File Size is NOT a Completion Metric
Arbitrary file line count limits (e.g. strict 300-line caps) are explicitly rejected as completion criteria for DZTGBot. Forcing arbitrary file splits introduces:
- Fragile artificial module boundaries that obscure domain cohesion.
- Increased circular import risk across split helper files.
- Fragmented state machine and repository transaction logic.

### 4.2 Enforceable Architectural Rules
Instead of cosmetic file size limits, DZTGBot enforces strict quality and architectural rules:

1. **Import Layer Isolation**: AST-analyzed in `tests/test_architecture_dependencies.py`. `domain` imports zero external packages; `services` imports only `domain`; `infrastructure` never imports `ui` or `telegram`.
2. **Protocol Inversion of Control**: All service dependencies are bound to `Protocol` interfaces in `src/dztgbot/domain/ports.py`.
3. **Deterministic Lifecycle**: Resources and HTTP client pools are managed explicitly in `src/dztgbot/__main__.py` with reverse-order teardown.
4. **Static Quality Gates**:
   - Configured in `pyproject.toml` and verified in `.github/workflows/quality.yml`.
   - **Ruff**: Python 3.12 target with correctness rules enabled (`E9`, `F`, `B`, `ASYNC`).
   - **Mypy**: Strict mode (`strict = true`) on all new core packages (`src/dztgbot/domain`, `services`, `infrastructure`, `ui`, `__main__.py`).
   - **Coverage**: Branch-aware coverage (`branch = true`) with a 90% floor on critical workflow modules (`domain/fsm.py`, `domain/callbacks.py`, `domain/policy.py`, `services/callback_service.py`, `services/submission_service.py`, `infrastructure/persistence/workflow_sqlite.py`) and a 75% overall repository floor.

---

## 5. Rollback & Migration Considerations

### 5.1 SQLite Schema Migration & Backward Compatibility
- Schema versioning is managed via SQL migration scripts in `src/dztgbot/infrastructure/persistence/migrations/` (`001_initial.sql`, `002_indexes.sql`).
- SQLite WAL mode ensures transactional safety and concurrent read access.
- In the event of an application rollback, the database schema remains forward-compatible. Draft records in `SUBMISSION_UNKNOWN` state remain preserved on disk and protected against deletion or automatic retry.

---

## 6. Explicit Offline / Live Evidence Boundary

- **Offline Test Execution**: All 449 unit and integration tests run 100% offline using synthetic mocks and fakes (`tests/support/workflow_fakes.py`, `tests/support/security_fakes.py`).
- **No Unverified Live Claims**:
  - No live Telegram Bot API updates are polled in automated CI.
  - No live Gemini API calls or token billing occur during tests.
  - No real Jira Server/Data Center REST API issues are created or modified in CI.
  - No live VPN subprocess (`nmcli`) connections are initiated during automated testing.
- **Sanity Review & Cleanliness**:
  - Zero `file://` links in documentation.
  - Zero personal workstation file paths (`C:\Users\...` or `/home/...`).
  - Zero live API tokens, passwords, or PATs in checked-in test fixtures or source files.
  - All documentation cross-references use clean repo-relative paths.
