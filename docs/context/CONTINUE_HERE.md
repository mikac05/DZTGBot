# Continue here

## Current status

Phases 0, 1, 2, and 3 of the multi-agent remediation program ([`MASTER_PLAN.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/MASTER_PLAN.md)) are 100% complete and verified:

- **Phase 0 (Contracts & Characterization)**: Frozen system contracts ([`docs/architecture/workflow-contracts.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/docs/architecture/workflow-contracts.md), [`docs/architecture/dependency-rules.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/docs/architecture/dependency-rules.md)), characterization test harness, and security contract baseline.
- **Phase 1 (Domain, FSM & Callback Policy)**: Canonical domain models ([`src/dztgbot/domain/models.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/models.py)), state machine FSM ([`src/dztgbot/domain/fsm.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/fsm.py)), callback token grammar ([`src/dztgbot/domain/callbacks.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/callbacks.py)), security policy ([`src/dztgbot/domain/policy.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/policy.py)), domain error taxonomy ([`src/dztgbot/domain/errors.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/errors.py)), and pure Python domain protocols ([`src/dztgbot/domain/ports.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/ports.py)).
- **Phase 2 (Durable Persistence & Credential Hardening)**: Production SQLite workflow repository ([`src/dztgbot/infrastructure/persistence/workflow_sqlite.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/infrastructure/persistence/workflow_sqlite.py)) with WAL mode, schema migrations, SHA-256 token hashing, one-winner CAS state transitions, and attempt claims. `UserStore` copy-on-write persistence and corruption quarantine ([`src/dztgbot/user_store.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/user_store.py)).
- **Phase 3 (Pure Application Services)**:
  - Task P3-A: `WorkflowService` ([`src/dztgbot/services/workflow_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/workflow_service.py)) & `ConnectivityService` ([`src/dztgbot/services/connectivity_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/connectivity_service.py)).
  - Task P3-C: `IntakeService` ([`src/dztgbot/services/intake_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/intake_service.py)).
  - Task P3-G: `CallbackService` ([`src/dztgbot/services/callback_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/callback_service.py)).

Local test verification:
- **212/212 offline unit tests PASSED** in 0.858s.
- Zero file collisions across agent tracks.

## Exact next action

Upon resuming work (or running `DZTGBot continue`), execute **Phase 4: Provider Gateways and Mutation Recovery** according to `MASTER_PLAN.md`.

### Recommended Run Order for Phase 4:

1. **Step 1: Codex (Task P4-C1 & P4-C3)** — Jira Gateway, canonical payloads, shared transport (`infrastructure/jira_gateway.py`) and Gemini Gateway (`infrastructure/gemini_gateway.py`).
2. **Step 2: Codex (Task P4-C2)** — Submission, reconciliation, and attachment services (`services/submission_service.py`, `services/attachment_service.py`).
3. **Step 3: Antigravity (Task P4-A)** — Provider orchestration boundaries & architecture dependency tests (`docs/architecture/provider-boundaries.md`, `tests/test_architecture_dependencies.py`).
4. **Step 4: Grok (Task P4-G)** — Strict configuration & security defaults (`config.py`, `.env.example`).

### Prompts for Phase 4 Execution:

#### Prompt for Codex (Task P4-C1, P4-C2, P4-C3):
```markdown
Execute Phase 4 Tasks P4-C1, P4-C2, P4-C3 in DZTGBot repository according to MASTER_PLAN.md:

Scope & Files (Exclusive to Codex):
- src/dztgbot/infrastructure/jira_gateway.py
- src/dztgbot/infrastructure/gemini_gateway.py
- src/dztgbot/services/submission_service.py
- src/dztgbot/services/attachment_service.py
- tests/test_jira_gateway.py
- tests/test_jira_credential_isolation.py
- tests/test_submission_recovery.py
- tests/test_ambiguous_create.py
- tests/test_attachment_service.py
- tests/test_gemini_gateway.py

Requirements:
1. Build Jira Gateway (JiraGatewayPort) with strict request/response DTOs, single httpx.AsyncClient connection pool, per-request Bearer auth headers, bounded timeouts, and metadata caching.
2. Implement SubmissionService for failure-preserving Jira create/update attempts, canonical request hashing, SUBMISSION_UNKNOWN timeout handling, and published edit diffing.
3. Implement AttachmentService for bounded, deduplicated photo uploads without recreating issues.
4. Implement Gemini Gateway (AIAnalyzerPort) with prompt character budgets, rate limit classification, and deadline handling.
5. Add unit tests for all gateway and submission recovery logic.
```

#### Prompt for Antigravity (Task P4-A):
```markdown
Execute Phase 4 Task P4-A in DZTGBot repository according to MASTER_PLAN.md:

Scope & Files (Exclusive to Antigravity):
- docs/architecture/provider-boundaries.md
- tests/test_architecture_dependencies.py

Requirements:
1. Document provider orchestration boundaries in docs/architecture/provider-boundaries.md (adapters wrap third-party SDKs, domain/services import no provider exceptions or Telegram API objects).
2. Add automated architectural dependency tests in tests/test_architecture_dependencies.py checking import direction and cycle prevention.
```

#### Prompt for Grok (Task P4-G):
```markdown
Execute Phase 4 Task P4-G in DZTGBot repository according to MASTER_PLAN.md:

Scope & Files (Exclusive to Grok):
- src/dztgbot/config.py
- .env.example
- tests/test_config_security.py
- tests/test_config_paths.py

Requirements:
1. Add configuration validation for WORKFLOW_DB_PATH, auth TTL, queue/size/concurrency limits, and optional allowed-user policy.
2. Validate Jira URL formatting, represent absent paths as None, and enforce PAT-only/private-chat defaults.
```

## Inputs still required from the user or target environment

- Fresh target-environment access only if the user asks to verify or redeploy the live service.
- Confirmation of target Jira server custom field / idempotency property capabilities during Phase 4 integration.

## Do not redo

- Do not repeat completed Phase 0, Phase 1, Phase 2, or Phase 3 implementation.
- Do not bypass master plan boundaries or file-ownership mappings.
- Do not reconfigure NetworkManager, VPN routing, systemd, Telegram BotFather, Jira, or Gemini without explicit authorization.

## Required verification on resume

- Confirm the handoff commit is checked out `HEAD` after `python scripts/handoff.py continue`.
- Run `$env:PYTHONPATH="src"; .venv\Scripts\python.exe -m unittest discover -s tests -v` to verify 212 tests pass.
- Execute Phase 4 tasks in the specified run order.
