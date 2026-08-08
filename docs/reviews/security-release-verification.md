# DZTGBot Phase 9 Task P9-G — Security and release-readiness verification

**Document owner:** Grok (security / release-readiness verification)  
**Reference:** `MASTER_PLAN.md` Phase 9 Task P9-G  
**Date:** 2026-08-08  
**Evidence type:** offline local verification and documentation cross-check only  

This report records security and release-readiness evidence for the current remediation working tree. It does **not** authorize production go-live, claim live penetration testing, or claim external Telegram / Gemini / Jira / VPN / systemd / Ubuntu host validation.

---

## 1. Shared evidence identity and boundary

| Field | Value |
| --- | --- |
| Base HEAD | `03499594a4f8975ae046fa513c9aada7e1c836b6` |
| Remediation state | Uncommitted working tree layered on that base HEAD (not a remediation commit) |
| Verification date | 2026-08-08 |
| Runtime | Local Windows (`AMD64`) with project `.venv`, **CPython 3.12.13** |
| External evidence | Telegram polling, Gemini generation, Jira Server/Data Center REST, NetworkManager L2TP/IPsec, systemd unit lifecycle, and Ubuntu 24.04 host deployment remain **unverified** |

Identity was confirmed with `git rev-parse HEAD` and `git status` before evidence collection. HEAD matched the shared base; remediation changes remained uncommitted.

**Agreement with peer Phase 9 reports:** this identity matches `docs/reviews/architecture-remediation-verification.md` and `docs/reviews/performance-recovery-verification.md` (base HEAD, uncommitted working-tree remediation, date 2026-08-08, CPython 3.12.13 Windows, external services unverified). Any residual wording differences are enumerated in §8.

---

## 2. Result (offline security gate)

The current uncommitted remediation tree **passes** the offline security and release-readiness checks in this record:

- Integrated authz, bound callbacks, PAT-only/private policy, credential store hardening, request-local Jira PAT isolation, TLS/CA configuration, privacy-safe logs/metrics, provider safe-error mapping, abuse/resource bounds, and operator security documentation are present and covered by offline tests.
- Focused security-related suites and the complete offline suite completed with **zero failures**.
- Safe secret/path/diff scans found no tracked production secrets and only test-fixture secret placeholders in tests.
- Credential field encryption remains an **explicit documented deferral** (`docs/security/credential-threat-model.md` §6), not an improvised implementation.
- Release remains **pilot-only** until supervised target-environment validation is separately authorized and completed.

---

## 3. Verification method

### 3.1 Source and documentation surfaces inspected

| Area | Primary paths |
| --- | --- |
| Authz / policy | `src/dztgbot/domain/policy.py`, `src/dztgbot/domain/callbacks.py`, `src/dztgbot/services/callback_service.py` |
| Auth / admin / UI gates | `src/dztgbot/jira_auth.py`, `src/dztgbot/admin.py`, `src/dztgbot/ui/handlers/` |
| Config / TLS / allowlist | `src/dztgbot/config.py`, `.env.example` |
| Credential store | `src/dztgbot/user_store.py` |
| Workflow token persistence | `src/dztgbot/infrastructure/persistence/workflow_sqlite.py`, migrations under `src/dztgbot/infrastructure/persistence/migrations/` |
| Provider isolation / redaction | `src/dztgbot/infrastructure/jira_gateway.py`, `src/dztgbot/infrastructure/gemini_gateway.py`, `src/dztgbot/domain/errors.py` |
| Observability privacy | `src/dztgbot/services/observability.py`, `src/dztgbot/services/limits.py`, `src/dztgbot/infrastructure/keyed_processor.py` |
| Deploy / host confinement | `deploy/systemd/dztgbot.service`, `scripts/deploy.sh` |
| Operator / threat docs | `docs/security/credential-threat-model.md`, `docs/operations/workflow-db-runbook.md`, `docs/end-to-end-test-plan.md`, `README.md` |
| Original audit | `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md` |
| Peer Phase 9 reports | `docs/reviews/architecture-remediation-verification.md`, `docs/reviews/performance-recovery-verification.md` |

### 3.2 Commands executed (repository-relative)

```powershell
git rev-parse HEAD
git status --short
.venv\Scripts\python.exe --version
$env:PYTHONPATH='src'
# Focused security/authz/config/privacy/abuse/user-store/callback/gateway suite
.venv\Scripts\python.exe -m unittest `
  tests.test_integrated_security `
  tests.test_integrated_authz_matrix `
  tests.test_config_security `
  tests.test_config_paths `
  tests.test_auth_handlers `
  tests.test_admin_private_only `
  tests.test_observability_privacy `
  tests.test_abuse_controls `
  tests.test_jira_credential_isolation `
  tests.test_user_store_failures `
  tests.test_user_store_permissions `
  tests.test_callback_authorization `
  tests.test_callback_grammar `
  tests.test_callback_replay `
  tests.test_security_policy `
  tests.test_security_contracts `
  tests.test_privacy_logging_contracts `
  tests.test_known_workflow_defects `
  tests.test_jira_gateway `
  tests.test_gemini_gateway `
  tests.test_resource_bounds -q
# Complete offline suite (independent confirmation of shared count)
.venv\Scripts\python.exe -m unittest discover -s tests -q
# Skip identity
.venv\Scripts\python.exe -m unittest `
  tests.test_user_store_permissions.UserStorePermissionTests.test_symlink_rejection_when_nofollow_supported -v
```

Safe secret/path/diff scans (no secret values retained in this report):

- `git ls-files` for secret-shaped tracked filenames
- `git status --porcelain` for untracked secret-shaped paths
- `git diff --stat HEAD` (aggregate only)
- `.gitignore` secret/VPN coverage check
- SQL migration scan for callback token column name (`token_hash` only)
- Pattern scan of `src` / `tests` / `docs` / `scripts` / `deploy` for hardcoded assignment shapes (values redacted in tooling output)

No live Telegram, Gemini, Jira, VPN, systemd, or Ubuntu mutation was performed.

---

## 4. Test evidence

### 4.1 Complete offline suite (shared identity)

| Metric | This run |
| --- | --- |
| Command | `$env:PYTHONPATH='src'; .venv\Scripts\python.exe -m unittest discover -s tests -q` |
| Summary | `Ran 449 tests in 5.424s` / `OK (skipped=1)` |
| Failures | 0 |
| Errors | 0 |
| Platform skip | `tests.test_user_store_permissions.UserStorePermissionTests.test_symlink_rejection_when_nofollow_supported` — reason: `O_NOFOLLOW not available on this platform` |

**Suite identity interpretation (reconciled):**

- Unittest reports **449 tests ran** with **1 skip** and **0 failures** (shared suite size = 449).
- Architecture report enumerates **Passed: 448**, **Skipped: 1**, **Executed: 449** — same outcome under different wording.
- Performance report states **449 tests ran** with **one platform-specific skip** — same outcome.
- Task coordination label “449 passed, 1 platform skip” refers to this shared suite identity; precise accounting is **448 pass + 1 skip = 449 collected/ran**, zero fail.

### 4.2 Focused security-related suites

Combined focused modules used for this security record (including gateway/resource privacy-adjacent modules):

| Module | Tests | Result |
| --- | --- | ---: |
| `tests/test_integrated_security.py` | 13 | Pass |
| `tests/test_integrated_authz_matrix.py` | 9 | Pass |
| `tests/test_config_security.py` | 10 | Pass |
| `tests/test_config_paths.py` | 14 | Pass |
| `tests/test_auth_handlers.py` | 31 | Pass |
| `tests/test_admin_private_only.py` | 10 | Pass |
| `tests/test_observability_privacy.py` | 10 | Pass |
| `tests/test_abuse_controls.py` | 13 | Pass |
| `tests/test_jira_credential_isolation.py` | 1 | Pass |
| `tests/test_user_store_failures.py` | 13 | Pass |
| `tests/test_user_store_permissions.py` | 4 | Pass (1 skip) |
| `tests/test_callback_authorization.py` | 14 | Pass |
| `tests/test_callback_grammar.py` | 24 | Pass |
| `tests/test_callback_replay.py` | 6 | Pass |
| `tests/test_security_policy.py` | 33 | Pass |
| `tests/test_security_contracts.py` | 10 | Pass |
| `tests/test_privacy_logging_contracts.py` | 4 | Pass |
| `tests/test_known_workflow_defects.py` | 8 | Pass |
| `tests/test_jira_gateway.py` | 3 | Pass |
| `tests/test_gemini_gateway.py` | 4 | Pass |
| `tests/test_resource_bounds.py` | 9 | Pass |
| **Combined focused run** | **243** | **OK (skipped=1)** |

All tests used deterministic local fakes, temporary files/databases, or mocked transports. No provider credentials or live network mutations were required for pass.

---

## 5. Security control verification

### 5.1 Integrated authorization and private-only scope

| Control | Implementation evidence | Offline verification |
| --- | --- | --- |
| Private-chat-only for workflows, auth, admin | `require_private_chat` / `require_private_admin` in `domain/policy.py`; enforced in `jira_auth.py`, `admin.py`, callback service, UI handlers | `test_admin_private_only`, `test_auth_handlers`, `test_integrated_security`, `test_security_policy` |
| Optional user allowlist | `TELEGRAM_ALLOWED_USER_IDS` → `Settings.telegram_allowed_user_ids`; `require_allowed_user` | Config + auth handler tests |
| Numeric admin gate | `TELEGRAM_ADMIN_USER_IDS` + `require_admin` / `require_private_admin` | Admin private-only suite |
| Non-leaky denial codes | `DenialCode` + fixed user messages; no raw tokens/payloads | `test_privacy_logging_contracts`, policy suite |
| First-release invariants non-disablable | `AUTH_PAT_ONLY` and `PRIVATE_CHAT_ONLY` cannot be set false in `config.py` | `test_config_security` |

### 5.2 Strict bound callbacks (hash, replay, expiry, actor/chat/message/revision)

| Control | Implementation evidence | Offline verification |
| --- | --- | --- |
| Wire grammar `j1:<action>:<opaque_token>` | `domain/callbacks.py`; 128-bit hex tokens; 64-byte Telegram limit | `test_callback_grammar`, known-defect bound-grammar test |
| Only SHA-256 hashes at rest | `hash_opaque_token`; SQLite `callback_tokens.token_hash` PRIMARY KEY (64 hex) | Migration check + repository/callback tests |
| Possession of token alone insufficient | `authorize_callback` requires actor, chat, optional thread/preview message, action, expiry, consume, revision, state | `test_callback_authorization`, `test_integrated_authz_matrix` |
| Replay / one-shot consume | Atomic `consume_callback` CAS; one-shot actions include confirm/cancel/retry/reconcile | `test_callback_replay`, restart/recovery suites (peer P9-C) |
| Expiry | `expires_at` timezone-aware; default preview TTL 1h in `CallbackService` | Authz matrix + policy tests |
| Stale revision / illegal state | `STALE_REVISION`, `ILLEGAL_STATE`, `ALREADY_PROCESSING` | Integrated authz matrix |
| Legacy unbound callbacks rejected | e.g. `jira_confirm` / raw draft-id forms rejected by grammar | Known-workflow-defects suite |

### 5.3 PAT-only authentication

| Control | Implementation evidence | Offline verification |
| --- | --- | --- |
| PAT-only classification | `classify_credential_input` / `normalize_pat_input` rejects Basic, cookie, `user:pass` shapes | Policy + auth handler suites |
| Auth conversation TTL | Default 180s (`DEFAULT_AUTH_TTL_SECONDS` / `AUTH_TTL_SECONDS`); domain `AUTH_CONVERSATION_TTL` 3 minutes | Auth handler expiry tests |
| Credential message delete + failure warning | Best-effort delete; fixed `CREDENTIAL_DELETE_FAILED` user warning | Integrated security + auth handlers |
| Logout is local-only | `logout_revokes_remote_pat() -> False`; docs/threat model state Jira-side revoke is separate | Policy + integrated security |
| `/myself` validation path | Auth flow uses credential test via gateway/client without storing rejected shapes | Auth handlers (mocked transport) |

### 5.4 Credential store (copy-on-write, corruption, permissions)

| Control | Implementation evidence | Offline verification |
| --- | --- | --- |
| Copy-on-write snapshot writes | Memory updated only after validated durable write (`UserStore`) | `test_user_store_failures` |
| Corruption quarantine + `.prev` recovery | Quarantine to `.corrupt`, recover previous snapshot | User-store failure suite + integrated security |
| Mode `0600` on POSIX | Write path enforces restrictive mode | `test_user_store_permissions` (POSIX asserts; Windows best-effort regular file) |
| Symlink / non-regular file refusal | `O_NOFOLLOW` when available; directory path refused | Permissions suite (symlink test **skipped on Windows**) |
| PAT never in `repr` / logs | `JiraCredentials.__repr__` redacts PAT; store logs user id only | Privacy contracts + store code review |
| Size cap | `MAX_STORE_BYTES` 1 MiB | Failure suite |

**Residual:** application-layer PAT field encryption is **deferred** with an approved design gate (`docs/security/credential-threat-model.md` §6). Host confinement (`0600`, non-root service UID, state dir) is the selected first-release boundary.

### 5.5 Request-local Jira PAT isolation

| Control | Implementation evidence | Offline verification |
| --- | --- | --- |
| No shared client `Authorization` header | Shared `httpx.AsyncClient` built without default auth; per-call headers from PAT argument | `test_jira_credential_isolation` |
| Concurrent PATs remain request-local | Two concurrent `test_credential` calls observe distinct `Bearer` headers; client default header stays empty after | Same test |
| Raw PAT required (no Basic/Bearer prefix stored as header source) | `JiraGateway._headers` rejects empty/prefixed tokens | Gateway unit tests |

### 5.6 TLS and custom CA configuration

| Control | Implementation evidence | Offline verification |
| --- | --- | --- |
| HTTPS-only Jira URL | `_parse_jira_url` rejects `http://`, credentials-in-URL, query, fragment | `test_config_security` |
| Verify SSL default true | `JIRA_VERIFY_SSL` default true; `jira_tls_verify` property for httpx | TLS default tests |
| Custom CA bundle | `JIRA_CA_BUNDLE_PATH` absolute path; incompatible with verify-disable | Config security tests |
| Verify-disable escape hatch | Explicit warning string without host/URL/token leakage | Logged warning assertion tests |
| Workflow DB path safety | Absolute, outside checkout, rejects known cloud-sync markers | `test_config_paths` |

### 5.7 Privacy-safe logs, metrics, and provider redaction

| Control | Implementation evidence | Offline verification |
| --- | --- | --- |
| Global error handler | Logs exception **type** only; does not serialize update or exception message | `test_privacy_logging_contracts` |
| Callback parse errors | Fixed `code` only; raw payload never in exception message | Same |
| Denial user text | Fixed Traditional Chinese/English codes; no PAT/`j1:`/Bearer echo | Same |
| Provider mapping | Jira/Gemini adapters map to `ClassifiedOperationError` / safe codes; no provider body as user text | Gateway + integrated security redaction paths |
| Safe metrics | Fixed event/outcome codes; opaque `c1_…` correlation IDs; bounded history; rejects arbitrary labels | `test_observability_privacy` |
| Overload/deadline feedback | Fixed safe copy from keyed processor and resource limiter | Observability privacy + abuse/resource suites |
| Credential `repr` | `jira_pat=<redacted>` | Code + privacy tests |

### 5.8 Abuse and resource bounds (security-adjacent)

Offline suites confirm admission/queue/concurrency/cooldown/deadline controls return fixed feedback without disclosing other users’ workflow state, tokens, or provider bodies (`test_abuse_controls`, `test_resource_bounds`, observability privacy). These are invariant tests, not production capacity claims.

### 5.9 Secret boundaries (repository and deploy)

| Boundary | Evidence |
| --- | --- |
| Git ignore | `.gitignore` ignores `.env`, `secrets/`, `credentials/`, `*.nmconnection` (except example), `vpn/`, `src/ref/` |
| Tracked secret-shaped files | No tracked `.env`, private credentials JSON, or private VPN XML; only `config/l2tp-ipsec.example.nmconnection` example template and `.env.example` placeholders |
| `.env.example` | Placeholder `TODO_…` values only; documents PAT-only, private-chat, TLS verify default, optional CA, allowlist |
| Hardcoded assignment scan | Hits only in **tests** with synthetic fixtures (redacted in tooling); **zero** secret-ish assignments under `src/` |
| SQL at rest | Callback table stores `token_hash` only (not opaque token) |
| systemd unit | Root-owned `EnvironmentFile` placeholder; `ProtectSystem=strict`; journald without secret logging contract noted in unit comments |
| Threat model | Documents assets, adversaries, `0600` boundary, encryption deferral, operator checklist |
| Runbook | Workflow DB under `/var/lib/dztgbot`, backup `0600`, no secret pasting |

Working-tree note: remediation includes modified `.env.example` and many uncommitted source/test/docs files; none of the scans above indicated a new tracked production secret file in this evidence pass.

### 5.10 Operator documentation and deployment constraints

| Document | Security-relevant content verified present |
| --- | --- |
| `docs/security/credential-threat-model.md` | Assets, trust boundaries, threats, `0600` selection, encryption deferral, audit mapping, non-claims |
| `docs/operations/workflow-db-runbook.md` | State dir layout, permissions, backup/restore, secret-handling cautions |
| `docs/end-to-end-test-plan.md` | Evidence boundary, private PAT auth steps, supervised live gates, no unattended Jira mutation |
| `README.md` (remediation tree) | Operational/security defaults as updated in working tree (not re-asserted as live-validated) |
| `scripts/deploy.sh` / `deploy/systemd/dztgbot.service` | Ubuntu 24.04 gate (deploy script), non-root service, hardened unit, env file outside checkout intent |

---

## 6. Original audit security / UX risk cross-map

Source: `docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md` (2026-08-07 end-to-end review). Status is relative to this offline remediation tree.

| Audit theme (ranked / section) | Original risk (summary) | Status after remediation | Evidence / residual |
| --- | --- | --- | --- |
| #1 Durable draft identity + callbacks | Unbound action-only callbacks; cross-chat draft collisions | **Implemented** (offline) | SQLite drafts; `j1:` bound tokens; authz matrix |
| #2 Explicit FSM | Implicit/mutable handler state | **Implemented** (offline) | `domain/fsm.py` + repository CAS |
| #3 Mutation recovery / idempotency | Lost drafts; ambiguous create duplicates | **Implemented** (offline) | Submission/unknown reconcile suites (peer P9-C); not live-Jira proven |
| #4 Concurrency / task lifecycle | Concurrent updates + raw `create_task` | **Implemented** (offline) | Keyed processor + scheduler; architecture report |
| #5 Auth private, TTL, PAT-only, allowlist, delete warn | Open auth, passwords/cookies, silent delete fail | **Implemented** (offline) | Policy + auth handlers + integrated security |
| #6 Live Jira metadata discovery | Silent type coercion; weak preflight | **Partially implemented / residual** | Gateway metadata path exists; full live metadata correctness **unverified** |
| #7 Multimodal media honesty | Photos not analyzed; non-photo not attached | **Documented product boundary** | E2E plan states photo-only attach; capability honesty is product/docs residual for UX |
| #8 Telegram interaction polish | Markdown/keyboards/copy UX | **Partially implemented** | UI rendering tests exist; live UX **unverified** |
| #9 Quotas / backpressure / rate limits | No limiter | **Implemented** (offline synthetic) | Resource limiter + abuse controls; live capacity **unverified** |
| #10 Transactional store + stronger secrets | JSON-only state; weak credential boundary | **Implemented store; encryption deferred** | SQLite WAL + COW `0600` store; encryption explicitly deferred |
| #11 Diff-based published updates | Stale overwrite; incomplete fields | **Implemented** (offline) | Published-update conflict tests (peer P9-C); live Jira **unverified** |
| #12 Observability | No privacy-safe metrics | **Implemented** (offline) | `SafeMetrics` + privacy suite |
| #13 Handler integration / E2E tests | Missing stale-callback / recovery tests | **Implemented offline** | Large offline suite; supervised live plan remains |
| #14 Split `core.py` | Monolith ownership risk | **Largely implemented** | Composition root + services/UI; legacy facades remain (architecture residual) |
| #15 Ops docs reconciliation | Stale claims | **Improved in remediation docs** | Threat model, runbook, E2E plan present; live claims still forbidden until supervised run |
| #16 Admin governance private-only | Admin in groups; weak rules governance | **Private-only admin implemented**; rules schema/approval still **deferred residual** | Admin suite; rules versioning/approval not full product governance |
| #17 Privacy / retention / support paths | Weak support/retention story | **Partial** | Logging privacy + retention notes in runbook/threat model; human support channel still ops residual |
| Auth password/cookie surface | Higher phishing/expiry risk | **Mitigated** (PAT-only) | Policy rejection |
| Group privacy of rules/auth | Group disclosure | **Mitigated** for first release | Private-only gates |
| Attachment privacy log leakage | File IDs / exception text | **Addressed in contracts** | Privacy/observability tests; residual risk if future log statements regress |
| Plaintext multi-user credential file | Single JSON of PATs | **Hardened not eliminated** | `0600` + COW + quarantine; encryption deferred; service-UID read remains full compromise |
| No structured monitoring | Ops blind spot | **Mitigated offline** | In-process safe metrics; external alerting transport still ops residual |

---

## 7. Supervised live test plan only (no execution in this task)

The following is a **plan**. No step below was executed as part of P9-G. Do not run without separate explicit operator approval. Prefer disposable test bot/project credentials. Never paste secrets into chat, tickets, or Git.

### 7.1 Preconditions (operator)

1. Clean **Ubuntu 24.04** host with Python 3.12 and NetworkManager as required.
2. Root-owned `0600` environment file **outside** checkout; unresolved `TODO_` keys removed for required settings only.
3. `WORKFLOW_DB_PATH` and credentials under `/var/lib/dztgbot` (`0700` dir / `0600` files), not cloud-synced.
4. `JIRA_VERIFY_SSL=true` (or documented break-glass with CA plan); prefer custom CA via `JIRA_CA_BUNDLE_PATH` if needed.
5. `VPN_ALLOW_START=false` unless a supervised VPN window is explicitly approved.
6. Pass offline suite and CI quality workflow (including ShellCheck) before live traffic.

### 7.2 Host / deploy checks (no provider mutation)

1. Run `scripts/deploy.sh` per README; confirm Ubuntu gate, env file mode, service user, DB mode `600`, unit `active`.
2. `journalctl -u dztgbot.service` sample: running banner present; **no** tokens, PATs, message bodies, private URLs, VPN endpoints.
3. Confirm `stat` modes on env file, rules, credentials JSON, workflow DB, backups.

### 7.3 Authz and privacy (supervised Telegram)

1. **Group rejection:** from a group, attempt `/auth`, draft intake, callback, `/rules` — expect private-only denial; no rules text; no admin membership oracle beyond fixed copy.
2. **Allowlist (if configured):** non-allowlisted private user denied safely.
3. **Admin private-only:** non-admin and group admin attempts denied without leaking rules.
4. **PAT happy path:** private `/auth` with disposable PAT; message delete attempt; success without PAT echo.
5. **Non-PAT rejection:** synthetic `user:password` / cookie-shaped strings rejected; store unchanged.
6. **Auth TTL:** start `/auth`, wait beyond TTL, send input — expired path, no store.
7. **Logout:** local credential cleared; document that Jira-side revoke is separate.

### 7.4 Callback binding (supervised)

1. Create a private draft preview; confirm bound buttons.
2. Replay confirm after success — one-shot denial.
3. Use another user/chat against captured callback data — foreign/wrong-chat denial.
4. Edit draft then press old preview button — stale revision path.
5. Restart service mid-review; consumed/expired tokens remain invalid (pairs with DB runbook).

### 7.5 Jira mutation window (explicit per-issue approval)

1. Confirm create only after human inline confirm.
2. Induce/observe timeout only under controlled test project; verify **no blind re-create** without reconcile (`SUBMISSION_UNKNOWN` path).
3. Attachment failure/retry does not recreate issue.
4. Confirm journal and user messages contain no PAT/provider body dumps.

### 7.6 TLS / VPN (if in scope)

1. With valid CA path, Jira calls succeed; with deliberately wrong verify settings, fail closed (break-glass only in non-prod).
2. VPN status commands only; start only if `VPN_ALLOW_START` deliberately enabled for that window.

### 7.7 Stop criteria / non-goals

- Stop on any secret appearing in journald, Telegram echo, or screenshots.
- No unattended bulk Jira creates; no production data; no credential commits.
- Passing this plan is required for go-live confidence; **this P9-G report does not mark it complete**.

Canonical expanded operator steps also live in `docs/end-to-end-test-plan.md`.

---

## 8. Cross-report reconciliation (Phase 9 exit gate inputs)

| Topic | Architecture (P9-A) | Performance (P9-C) | Security (this P9-G) | Reconciliation |
| --- | --- | --- | --- | --- |
| Base HEAD | `03499594a4f8975ae046fa513c9aada7e1c836b6` | Same | Same | **Agree** |
| Remediation form | Uncommitted working tree on base HEAD | Same | Same | **Agree** |
| Date | 2026-08-08 | 2026-08-08 | 2026-08-08 | **Agree** |
| Environment | Windows, Python 3.12.13 `.venv` | CPython 3.12.13 Windows 11 AMD64 | CPython 3.12.13 Windows AMD64 | **Agree** (minor OS string detail only) |
| Full suite | 449 executed; 448 pass; 1 skip | 449 ran; 1 platform skip | `Ran 449` / `OK (skipped=1)` | **Agree on outcome**; wording differs as noted in §4.1 |
| Skip identity | `test_symlink_rejection_when_nofollow_supported` / `O_NOFOLLOW` | Same class | Same test + skip reason confirmed this run | **Agree** |
| ShellCheck | Not the P9-A focus | **Pending** locally; required via Ubuntu CI | **Pending** (not run on Windows; not claimed) | **Agree — ShellCheck still pending** |
| External boundary | Telegram/Gemini/Jira/VPN/systemd/Ubuntu unverified | Same | Same | **Agree** |
| Residual risks | Windows POSIX perms; legacy facade typing; live latency | Coverage floors; soak; multi-instance out of scope | Encryption deferral; live authz/TLS/VPN; rules governance; support path | **Compatible complementary residuals** (no contradiction) |
| Security report status | N/A | Noted security report not yet present at P9-C time | Now present | **Closes** P9-C follow-up item #5 for report existence; all three must still be read together for exit |

**Discrepancies:** none material. The only explicit wording difference is “448 passed + 1 skipped” versus “449 ran with 1 skip” versus coordination phrase “449 passed, 1 platform skip”; all describe the same unittest result class.

---

## 9. Residual risks (security-specific)

1. **No live security validation** of Telegram, Gemini, Jira, VPN, systemd, or Ubuntu DAC modes in this task.
2. **PAT plaintext at rest** for service UID: accepted first-release tradeoff; encryption deferred pending full key lifecycle design.
3. **Windows development gap:** `O_NOFOLLOW` symlink rejection and strict POSIX `0600` semantics require Ubuntu CI/target host.
4. **ShellCheck / deploy script static check** not executed locally; required in `.github/workflows/quality.yml` on Ubuntu 24.04.
5. **Optional allowlist empty = unrestricted users** (still private-chat and PAT-gated): operators must set `TELEGRAM_ALLOWED_USER_IDS` for tighter pilots.
6. **Rules admin governance** (schema, diff, dual approval) remains limited versus audit #16 full intent.
7. **Support/retention product path** still largely operational, not a full end-user privacy portal (audit #17 residual).
8. **Single-host trust model:** multi-instance SQLite and horizontal scale remain out of scope; co-tenant host users are out of first-release assumptions.
9. **Provider/preflight residual:** live Jira permission/project metadata correctness and TLS interception resistance depend on target CA/network path.
10. **Human factors:** stolen admin Telegram session, user device retaining undeleted PAT messages, and operator backup mishandling remain residual per threat model.

---

## 10. Every external item still unverified

The following were **not** validated by this P9-G evidence run (and remain unverified unless a future supervised record says otherwise):

1. Live Telegram Bot API long-polling and production update delivery.
2. Live Telegram credential-message deletion behavior for real clients/devices.
3. Live Google Gemini API authentication, quotas, and response content handling.
4. Live Jira Server/Data Center authentication with real PATs.
5. Live Jira issue create / update / attachment / search-reconcile against a real project.
6. Live Jira TLS verification against the deployment’s real CA chain or custom bundle.
7. Live NetworkManager L2TP/IPsec status, start authorization, and tunnel connectivity.
8. systemd unit install, hardening effectiveness, restart, and journald retention on a real host.
9. Ubuntu 24.04 deploy gate end-to-end (`scripts/deploy.sh`) on a clean target host.
10. Production filesystem DAC: root env `0600`, state dir `0700`, DB/credentials/rules `0600`, non-symlink guarantees.
11. POSIX `O_NOFOLLOW` credential symlink rejection on Linux.
12. ShellCheck of `scripts/deploy.sh` in the checked-in Ubuntu CI job (local Windows: not run).
13. CI workflow `.github/workflows/quality.yml` green run on GitHub-hosted Ubuntu 24.04.
14. Real backup/restore drill of workflow DB + credentials under failure (disk full, partial WAL, restore while stopped).
15. Supervised soak/load against real providers (capacity, timeout, rate-limit behavior).
16. VPN sudoers exact-path effectiveness and non-escalation on the target host.
17. Admin rules update governance under real operator workflow (beyond unit tests).
18. End-user support/retention process effectiveness outside documented offline controls.

---

## 11. Verification conclusion

For Phase 9 Task P9-G, the uncommitted remediation tree on base HEAD `03499594a4f8975ae046fa513c9aada7e1c836b6` **satisfies the offline security and release-readiness evidence bar**:

- Integrated authz and private/PAT-only product policy.
- Strict bound callbacks with hash-at-rest, replay/expiry, and actor-chat-message-revision binding.
- Credential COW/corruption/permission controls (with Windows platform skip noted).
- Request-local Jira PAT isolation and TLS/CA configuration defaults.
- Privacy-safe logging/metrics and provider safe-error boundaries.
- Secret-boundary hygiene in Git/examples and operator threat-model/runbook/deploy docs.
- Full offline suite identity **449 ran / 0 fail / 1 platform skip**, reconciled with P9-A and P9-C.
- Supervised live plan recorded only; **no external action performed**.

The service remains **pilot-only** until operators complete Ubuntu CI (including ShellCheck), host DAC verification, and the supervised live plan under separate explicit approval. This document makes **no** live-service readiness or penetration-test claim.
