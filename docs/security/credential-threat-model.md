# Credential threat model (DZTGBot)

**Scope:** first-release single-host Ubuntu 24.04 deployment.  
**Task:** Phase 8 P8-G documentation only — no cryptography implementation.  
**Related:** `src/dztgbot/user_store.py`, `src/dztgbot/config.py`, `scripts/deploy.sh`, `deploy/systemd/dztgbot.service`, `docs/operations/workflow-db-runbook.md`.

This document describes threats to secrets and identity material. It does not authorize production go-live by itself and does not claim live penetration testing.

## 1. Assets

| Asset | Description | Typical location |
| --- | --- | --- |
| Telegram bot token | Full bot API authority | Root-owned `0600` environment file; process environment after systemd load |
| Gemini API key | Paid/quota-bearing generative API access | Same environment file / process env |
| Per-user Jira PATs | Create/update/attach as that Jira user | Service-owned `0600` JSON (`USER_CREDENTIALS_PATH`) |
| Jira base URL | Internal topology hint | Environment file |
| Runtime Jira rules | Classification/policy text (sensitive ops content) | Service-owned `0600` rules file |
| Workflow SQLite | Draft text, templates, attempt metadata, token **hashes**, published keys | Service-owned `0600` DB under `/var/lib/dztgbot` |
| Callback opaque tokens | Bearer capability for a single draft action until expiry/consume | Telegram client messages; only SHA-256 hashes at rest |
| VPN profile (optional) | L2TP/IPsec identity material | Root-owned `0600` profile outside checkout |
| VPN sudoers fragment | Narrow privilege to load/up one connection | `/etc/sudoers.d/` root-owned |
| Service account | Non-login UID running the bot | Host account database |

**Explicitly out of asset scope for encryption discussion:** operator SSH keys, host disk encryption keys, Telegram account sessions on phones, and Jira server-side secret stores.

## 2. Adversaries and assumptions

| Adversary | Capability assumed | Out of scope / not assumed for first release |
| --- | --- | --- |
| Internet anonymous | Probe public Telegram surface; cannot read host disk | Breaking Telegram platform crypto |
| Malicious Telegram user | Sends messages/callbacks; not in admin allowlist | Kernel exploit |
| Compromised admin Telegram account | Issues `/rules`, `/setrules`, `/vpn*` | Physical data-centre access (separate track) |
| Co-tenant unprivileged host user | Standard Linux DAC | Root equivalence |
| Backup operator / shared storage admin | Reads backup media if mis-placed | Malicious firmware |
| Malicious process as service UID | Reads all service-readable files; memory | — (this is near full app compromise) |
| Root on host | Full control | — (trusted for env file and package install) |
| Supply-chain dependency attacker | Malicious PyPI content if pins bypassed | Full SLSA provenance program |

**Trust assumptions:**

- Single dedicated host; not a multi-tenant shared app server.
- Operators use `sudoedit` and do not commit `.env` or VPN profiles.
- Jira and Gemini are reached only from this host’s network path (often via L2TP/IPsec).
- First release is private-chat-only and PAT-only.

## 3. Trust boundaries

```text
[Telegram clients]
        |  TLS (platform)
        v
[Telegram cloud] ---- polling ----> [dztgbot non-root process]
                                         | env (bot token, gemini key)
                                         | read/write /var/lib/dztgbot/*
                                         | optional sudo nmcli (narrow)
                                         v
                              [Jira HTTPS] [Gemini HTTPS]
                                         ^
[Root operator] --0600 EnvironmentFile--/  (systemd reads, then drops UID)
```

| Boundary | Control |
| --- | --- |
| Git checkout → secrets | Secrets forbidden in Git; deploy refuses env file inside checkout |
| Root env file → service UID | systemd `EnvironmentFile` + non-root `User=`; file remains root `0600` |
| Service UID → host | `ProtectSystem=strict`, `ReadWritePaths=/var/lib/dztgbot`, `UMask=0077` |
| Service UID → VPN | Optional exact-path sudoers only when `VPN_ALLOW_START=true` |
| App → Jira | Per-request Bearer PAT; no shared static Jira password in env |
| App → logs | Privacy-safe logging; no PAT/body/provider secret text |
| Callback → mutation | `j1:` grammar, hash lookup, owner/chat/state/revision checks, one-shot actions |

## 4. Threat scenarios

### 4.1 At rest

| Threat | Impact | Current mitigation | Residual risk |
| --- | --- | --- | --- |
| World-readable env file | Bot token + Gemini key theft | Deploy enforces root `0600`, non-symlink, root-managed parent | Operator chmod regression |
| World-readable credentials JSON | All user PATs stolen | `UserStore` writes `0600`; deploy chown/chmod | Same |
| Workflow DB copied off host | Draft contents + issue keys + token hashes | Local path only; reject checkout/sync markers; service `0600` | Hashes are not PATs but drafts may be sensitive |
| Backup on USB/NAS without access control | Bulk secret disclosure | Runbook requires `0600` backups under state dir by default | Operator exporting backups unsafely |
| Cloud-sync folder (OneDrive etc.) | Extra cloud copies of DB/PATs | Config and deploy refuse known sync path markers for `WORKFLOW_DB_PATH` | Novel sync path names |
| Unencrypted disk theft | Full secret disclosure | Optional LUKS/host full-disk encryption (ops, outside app) | No app-level field encryption |

### 4.2 In memory / process

| Threat | Impact | Current mitigation | Residual risk |
| --- | --- | --- | --- |
| Core dump / crash dump | Token material in dump | Production hardening; avoid debug dump enablement | Host misconfiguration |
| `ptrace` / debugger as same UID | Read process memory | Service non-login; standard Linux DAC | Root or same-UID compromise |
| Swap | Secrets paged out | Prefer encrypted swap at host level | Not enforced by app |
| Exception string logging | Accidental secret echo | Typed errors; global handler logs exception **type** | Bug introducing secret interpolation |

### 4.3 Logs and chat surfaces

| Threat | Impact | Current mitigation | Residual risk |
| --- | --- | --- | --- |
| journald retains PAT | Long-term secret storage in logs | Code and docs forbid logging PATs/bodies | Future regression — tests cover privacy contracts |
| Credential message remains in Telegram history | PAT visible on user device / Telegram storage | Immediate delete attempt + warning on failure | Telegram may refuse delete; user device compromise |
| Group disclosure of rules/auth | Internal policy leak | Private-only admin and workflows | Mis-set bot privacy mode still needs ops awareness |
| Callback replay from chat history | Stale UI presses | Token expiry, one-shot consume, state/revision checks | Clock skew extremes |

### 4.4 Backup and restore

| Threat | Impact | Current mitigation | Residual risk |
| --- | --- | --- | --- |
| Backup contains plaintext PATs + drafts | Equivalent to live state theft | Treat backups as secret; mode `0600`; local path | Off-host copy policy is human |
| Restore of old DB after new creates | Confusion / reconciliation debt | Runbook restore steps; service stopped during restore | Operator error |
| Partial copy of WAL without main DB | Corruption on restore | SQLite backup API in deploy; runbook uses consistent backup | Manual `cp` while running |

### 4.5 Runtime abuse

| Threat | Impact | Current mitigation | Residual risk |
| --- | --- | --- | --- |
| Unauthorised Jira create | Fraudulent issues | Human confirm; PAT is caller’s; private chat; optional allowlist | Stolen admin Telegram session |
| Blind duplicate create after timeout | Duplicate Jira issues | `SUBMISSION_UNKNOWN` + reconcile-before-retry | Imperfect Jira search by request hash on some servers |
| Cross-user callback | Mutate victim draft | Owner/chat binding on authorize | Critical bugs would be security defects |
| Password/cookie phishing via bot | Broader credential theft | PAT-only input policy | Social engineering for PATs remains |

## 5. Selected security boundary: host confinement (`0600`)

For the first release, **application-layer encryption of PAT fields is not implemented**. The selected control set is:

1. **Root-owned `0600` environment file** outside the Git checkout for bot/Gemini/service configuration.
2. **Non-root dedicated service account** with nologin shell.
3. **Service-owned `0600` files** for rules, user credentials JSON, workflow SQLite, and DB backups.
4. **State directory `0700`** at `/var/lib/dztgbot` with systemd `StateDirectory` / `ReadWritePaths` confinement.
5. **Hardened unit** (`ProtectSystem=strict`, private tmp, restricted address families, restart limits, etc.).
6. **PAT-only + private-chat-only** product policy (cannot be disabled).
7. **Callback token hashing** at rest (opaque tokens never stored raw in SQLite).
8. **Privacy-safe logging** contracts enforced in tests.
9. **No secrets in Git**; handoff tooling validates secret-shaped paths.

This boundary matches a single-operator, single-host trust model: **anyone who can read files as the service user or as root can read PATs**, and that is accepted only because that principal already controls outbound Jira actions and the bot process.

## 6. Encryption: explicit deferral

Field encryption (for example AEAD over `jira_pat` values) is **deferred** and must **not** be improvised in a drive-by change.

Encryption may be proposed as a **separate, approved** design only when all of the following exist:

| Requirement | Why |
| --- | --- |
| Root-managed key lifecycle | Key must not live in Git or in the credentials JSON beside ciphertext |
| Key storage design | e.g. root `0600` key file, hardware token, or host KMS — with access for the service UID only as intended |
| Vetted AEAD format | Explicit algorithm, version byte, nonce uniqueness, AAD binding (user id / schema version) |
| Rotation procedure | Re-encrypt all rows/entries without dual plaintext windows in backups |
| Backup recovery | Restore works when only ciphertext + key backup are available; document dual-loss failure mode |
| Rollback | Prior app version can open data or a documented one-way migration freeze exists |
| Threat model update | States which adversaries encryption defeats (e.g. backup media theft) vs does not (service UID compromise) |
| Test plan | Round-trip, wrong-key fail-closed, corruption, and migration tests without live PATs |

Until that package is approved, **do not** add ad-hoc Fernet/AES helpers, hard-coded keys, or partial encryption that still logs secrets.

**Rationale for deferral now:** host confinement already matches the residual risk class of “service UID compromise equals game over,” which encryption-at-rest does not solve. Encryption helps primarily against **offline backup/media disclosure** and some classes of broader filesystem misconfiguration; those benefits are real but require the lifecycle package above to avoid irreversible lockout or false security.

## 7. Operator controls (checklist)

- [ ] Environment file root `0600`, outside checkout, not a symlink
- [ ] `WORKFLOW_DB_PATH` and credentials under `/var/lib/dztgbot`, not cloud-synced
- [ ] VPN profile root `0600` when enabled; `VPN_ALLOW_START=false` until supervised
- [ ] Rotate Telegram/Gemini tokens by env edit + restart + revoke old values
- [ ] User `/logout` only clears local PAT copy — revoke at Jira separately when needed
- [ ] Treat `/var/lib/dztgbot/backups/` as secret material
- [ ] Never run production with `JIRA_VERIFY_SSL=false` except documented break-glass with CA plan

## 8. Mapping to 2026-08-07 audit items

| Audit theme | Threat-model position |
| --- | --- |
| #5 PAT-only / private / TTL | Adopted as product invariants |
| #10 Transactional store + stronger secrets | SQLite + `0600` adopted; encryption deferred per §6 |
| #12 Observability | Must remain privacy-safe (no secret metrics labels) |
| #15 Docs reconciliation | This document + README/runbook |
| #17 Privacy/retention/support | Logging + retention runbook; human support channel still product/ops |

## 9. Non-claims

- No claim of formal STRIDE sign-off by an external assessor.
- No claim that `0600` equals encryption.
- No claim that offline unit tests prove host DAC configuration on a given server until operators verify modes on that host.
