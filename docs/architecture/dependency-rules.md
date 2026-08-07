# DZTGBot Dependency Rules & Code Organization

**Document Owner**: Antigravity (Software Architecture & Structural Cleanliness Lead)  
**Reference**: `MASTER_PLAN.md` (Phase 0 Task P0-A)  
**Date**: August 2026  

---

## 1. Architectural Layer Isolation Rules

To maintain high structural cleanliness, testability, and maintainability across multi-agent development tracks, DZTGBot enforces strict import and dependency boundaries:

```
[ Presentation Layer: dztgbot.ui ]
        |
        v  (Imports services & domain ports only)
[ Application Services: dztgbot.services ]
        |
        v  (Imports domain models, FSM & ports only)
[ Domain Model Layer: dztgbot.domain ]
        ^
        |  (Implements domain protocols)
[ Infrastructure Layer: dztgbot.infrastructure ]
```

---

## 2. Package Import Constraints Matrix

| Package Module | Allowed Imports | Forbidden Imports |
| :--- | :--- | :--- |
| `dztgbot.domain` | Python Standard Library (`dataclasses`, `enum`, `datetime`, `typing`, `uuid`) | `telegram`, `telegram.ext`, `httpx`, `google.genai`, `pydantic`, `sqlite3`, `dztgbot.services`, `dztgbot.infrastructure`, `dztgbot.ui` |
| `dztgbot.services` | `dztgbot.domain`, Python Standard Library | `telegram`, `telegram.ext`, `httpx`, `google.genai`, `dztgbot.ui`, `dztgbot.infrastructure` (except via DI protocols) |
| `dztgbot.infrastructure` | `dztgbot.domain`, Python Standard Library, `httpx`, `google.genai`, `sqlite3` | `telegram`, `telegram.ext`, `dztgbot.ui` |
| `dztgbot.ui` | `dztgbot.domain`, `dztgbot.services`, `telegram`, `telegram.ext`, Python Standard Library | `httpx`, `google.genai`, `sqlite3`, direct DB access |
| `dztgbot.__main__` | Composition root: imports all layers to construct dependency injection graph | None (Entry point only) |

---

## 3. Port & Protocol Conventions

1. **Protocol-Based Inversion of Control**: Infrastructure components must implement Python `typing.Protocol` interfaces defined in `dztgbot.domain.ports`.
2. **No Framework Abstract Classes**: Avoid subclassing `abc.ABC` or custom base classes. Protocols allow structural subtyping and clean mock injection during testing.
3. **Domain Exceptions Only**: Adapters in `dztgbot.infrastructure` catch external library exceptions (e.g. `httpx.HTTPError`, `google.genai.errors.APIError`, `sqlite3.Error`) and translate them into domain exceptions defined in `dztgbot.domain.errors`.
4. **Single `DraftState` ownership**: The complete lifecycle enum is defined in `domain/fsm.py`. `domain/models.py` and `domain/ports.py` import that enum; package `__init__` re-exports it. Never define a second `DraftState` in models or services.

---

## 4. Multi-Agent Exclusive File Ownership

To prevent merge conflicts and file collisions during concurrent multi-agent implementation:

- **Antigravity Scope**:
  - `src/dztgbot/domain/models.py`
  - `src/dztgbot/domain/ports.py`
  - All package `__init__.py` files
  - `src/dztgbot/services/workflow_service.py`
  - `src/dztgbot/services/connectivity_service.py`
  - `src/dztgbot/ui/**`
  - `src/dztgbot/core.py`
  - `src/dztgbot/__main__.py`
  - `src/dztgbot/vpn.py`
  - `docs/architecture/**`

- **Codex Scope**:
  - `domain/fsm.py`, `domain/errors.py`
  - `infrastructure/persistence/**`, `infrastructure/jira_gateway.py`, `infrastructure/gemini_gateway.py`, `infrastructure/keyed_processor.py`
  - `services/intake_service.py`, `services/submission_service.py`, `services/attachment_service.py`, `services/limits.py`, `services/observability.py`
  - `analysis.py`, `jira_client.py`, `rules.py`

- **Grok Scope**:
  - `domain/callbacks.py`, `domain/policy.py`
  - `services/callback_service.py`
  - `jira_auth.py`, `admin.py`, `user_store.py`, `config.py`, `.env.example`
  - Deployment scripts and user-facing documentation

---
