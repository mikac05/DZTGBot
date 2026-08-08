# DZTGBot Documentation Index

Welcome to the **DZTGBot** documentation hub. This index provides a clean, structured directory of all architectural specifications, operational runbooks, AI context handoffs, and verification reports.

---

## 🗂️ Documentation Directory

### 🏗️ Architecture & Specifications (`docs/architecture/`)
Core design specifications, state machine contracts, and dependency rules:
- **[`current-architecture.md`](architecture/current-architecture.md):** 4-Layer Domain Architecture specification (`ui -> services -> domain <- infrastructure`).
- **[`workflow-contracts.md`](architecture/workflow-contracts.md):** Finite State Machine (FSM) state lifecycle & cryptographic callback token contracts.
- **[`dependency-rules.md`](architecture/dependency-rules.md):** Layer purity rules and forbidden import boundaries.
- **[`provider-boundaries.md`](architecture/provider-boundaries.md):** External integration boundaries for Jira REST API v2 and Gemini AI.
- **[`migration-record.md`](architecture/migration-record.md):** SQLite database schema migration history (v1 through v4).

---

### 🔧 Operations & Deployment Runbooks (`docs/operations/`)
Target server installation, database management, and end-to-end testing:
- **[`end-to-end-test-plan.md`](operations/end-to-end-test-plan.md):** Supervised Ubuntu 24.04 target deployment & live integration testing sequence.
- **[`workflow-db-runbook.md`](operations/workflow-db-runbook.md):** SQLite WAL database administration, backup, migration, and recovery runbook.

---

### 🤖 AI Context & Handoff (`docs/context/`)
Durable cross-agent, cross-account, and cross-computer project context:
- **[`PROJECT_CONTEXT.md`](context/PROJECT_CONTEXT.md):** Stable architecture, settled decisions, and private input definitions.
- **[`CONTINUE_HERE.md`](context/CONTINUE_HERE.md):** Active checkpoint state and immediate next action.
- **[`HANDOFF.md`](context/HANDOFF.md):** Operational snapshot refreshed automatically on Git handoff.

---

### 🛡️ Security & Threat Modeling (`docs/security/`)
Security architecture and threat models:
- **[`credential-threat-model.md`](security/credential-threat-model.md):** Credential isolation, host confinement (`0600` permissions), and encryption deferral rationale.

---

### 🔍 Verification & Audit Reports (`docs/reviews/`)
Automated quality gate verification and historical audit reports:
- **[`architecture-remediation-verification.md`](reviews/architecture-remediation-verification.md):** Architectural compliance & layer purity report.
- **[`performance-recovery-verification.md`](reviews/performance-recovery-verification.md):** Keyed concurrency, resource bounds, and recovery test results.
- **[`security-release-verification.md`](reviews/security-release-verification.md):** Security release verification & credential safety audit.
- **[`telegram-bot-end-to-end-review-2026-08-07.md`](reviews/telegram-bot-end-to-end-review-2026-08-07.md):** Original baseline code audit.

---

## 📌 Root Project Files
- **[`README.md`](../README.md):** Main project introduction, feature summary, and quick-start deployment guide.
- **[`AGENTS.md`](../AGENTS.md):** Non-negotiable AI safety boundaries and Git handoff command rules.
- **[`GEMINI.md`](../GEMINI.md):** Agent entry point instructions.
- **[`MASTER_PLAN.md`](../MASTER_PLAN.md):** Core remediation master plan.
