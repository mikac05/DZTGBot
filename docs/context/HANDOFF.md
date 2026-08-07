# DZTGBot handoff

## Current objective

Complete the DZTGBot multi-agent architectural remediation program ([`MASTER_PLAN.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/MASTER_PLAN.md)) to transition the bot from a serial-use pilot to a production-safe, single-host Telegram bot for Taiwan team Jira management.

Success means domain isolation, explicit FSM transitions, cryptographic callback tokens, SQLite WAL workflow persistence, copy-on-write credential storage, pure application services, failure-preserving Jira mutations, and comprehensive unit test suites are fully implemented and verified without secrets or unverified live claims.

## Completed

- **Phase 0 (Contracts & Characterization)**: Created system architecture contracts ([`docs/architecture/workflow-contracts.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/docs/architecture/workflow-contracts.md), [`docs/architecture/dependency-rules.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/docs/architecture/dependency-rules.md)), characterization harness, and security baseline tests.
- **Phase 1 (Domain, FSM & Callback Policy)**: Implemented canonical domain entities ([`src/dztgbot/domain/models.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/models.py)), state machine FSM ([`src/dztgbot/domain/fsm.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/fsm.py)), callback token grammar ([`src/dztgbot/domain/callbacks.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/callbacks.py)), security policy ([`src/dztgbot/domain/policy.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/policy.py)), domain error taxonomy ([`src/dztgbot/domain/errors.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/errors.py)), and pure domain protocols ([`src/dztgbot/domain/ports.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/domain/ports.py)).
- **Phase 2 (Durable Persistence & Credential Hardening)**: Implemented SQLite workflow repository ([`src/dztgbot/infrastructure/persistence/workflow_sqlite.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/infrastructure/persistence/workflow_sqlite.py)) with WAL mode, versioned migrations, SHA-256 token hashing, one-winner CAS transitions, and attempt claims. Implemented copy-on-write persistence and corruption recovery in `UserStore` ([`src/dztgbot/user_store.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/user_store.py)).
- **Phase 3 (Pure Application Services)**: Implemented `WorkflowService` ([`src/dztgbot/services/workflow_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/workflow_service.py)), `ConnectivityService` ([`src/dztgbot/services/connectivity_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/connectivity_service.py)), `IntakeService` ([`src/dztgbot/services/intake_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/intake_service.py)), and `CallbackService` ([`src/dztgbot/services/callback_service.py`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/src/dztgbot/services/callback_service.py)).

## Decisions

- Master plan [`MASTER_PLAN.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/MASTER_PLAN.md) ratified as the authoritative multi-agent remediation specification.
- First release is private-chat-only for authentication, drafts, callbacks, Jira mutations, and admin commands.
- Authentication is Jira PAT-only. Passwords and session cookies are rejected.
- Workflow, callback, attempt, attachment, and published issue state are persisted in SQLite WAL database outside Git checkout.
- Callback tokens use `j1:<action>:<token>` format with 128-bit random tokens; only SHA-256 token hashes are stored in SQLite.
- Ambiguous create timeouts transition drafts to `SUBMISSION_UNKNOWN` state, requiring human reconciliation before any retry.

## Open items

- Phase 4 (Provider Gateways and Mutation Recovery) is ready for execution.
- Phase 5 (Telegram UI & Presentation Cutover) to follow Phase 4.
- Phase 6 (Composition-Root Cutover) to wire new architecture into `__main__.py` and retire legacy authority in `core.py`.
- Live service verification against Telegram BotFather, Gemini API, self-hosted Jira Data Center, and NetworkManager L2TP/IPsec VPN remains to be authorized and performed in the target environment.

## Exact next action

On the target computer, execute `DZTGBot continue`, verify 212 tests pass, and begin **Phase 4: Provider Gateways and Mutation Recovery** following the prompts and run order detailed in [`docs/context/CONTINUE_HERE.md`](file:///c:/Users/mikal/OneDrive/Others/DZTGBot/docs/context/CONTINUE_HERE.md).

## Verification

- 212/212 offline unit tests PASSED in 0.858s.
- Zero file collisions across multi-agent ownership maps.
- Secret safety and boundary validation passed before synchronization.

## Git snapshot metadata

<!-- HANDOFF-METADATA:START -->
- Generated UTC: `2026-08-07T16:47:50Z`
- Branch: `main`
- Upstream: `origin/main`
- Base commit before this handoff: `353973a7ccc6`
- Working-tree entries before metadata refresh: `32`
- The handoff commit is the commit containing this file.
<!-- HANDOFF-METADATA:END -->
