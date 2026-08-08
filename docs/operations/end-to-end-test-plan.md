# End-to-end server test plan and operator reference

This plan supports **Ubuntu 24.04 only**. It contains no usable credentials, VPN endpoints, Jira hosts, or authenticated session data. Placeholders must be replaced only in private, root-managed locations on the target host.

## 0. Evidence boundary

| Activity | Allowed in this plan? |
| --- | --- |
| Offline unit tests / deploy preflight | Yes |
| `systemctl` status/restart on the already-managed host during a supervised window | Yes, when the operator explicitly runs the steps |
| Real Telegram messages with a private test bot | Yes, supervised |
| Real Gemini analysis with a private test key | Yes, supervised |
| Real Jira create/update/attachment | **Only** with explicit human confirmation per issue in a supervised window |
| Automated CI or unattended scripts mutating Jira | **Never** |
| Automatic full-tunnel VPN start | **Never** as part of install; optional `/vpnstart` only when deliberately enabled |

Do not claim live validation for Telegram, Gemini, Jira, VPN, or systemd unless those steps were actually performed in the target environment.

## 1. Test boundaries

- Untouched `TODO_...` values are intentionally rejected during startup and deploy validation.
- Supply developer-owned test tokens/keys only through the protected environment file (`sudoedit`).
- Never paste secrets into a command, terminal history, Telegram message, log, screenshot, or tracked file.
- First-release product boundaries:
  - **Private chat only** for auth, drafts, callbacks, Jira mutations, and admin.
  - **PAT-only** authentication (passwords/Basic/cookies rejected).
  - **Human confirmation** before every Jira create.
  - **Photo attachments only** after create; non-photo media is not uploaded and is not multimodally analyzed.
- Keep `VPN_ALLOW_START=false` for simulated VPN-down tests. Do not reconfigure production tunnels casually.
- `GEMINI_MODEL` is **not** an application setting. Do not require it in the environment file.

## 2. Server preflight

Run `scripts/deploy.sh` as documented in the README. Expected outcomes:

- Ubuntu 24.04 gate passes.
- Root-owned `0600` environment file validated without printing values.
- Non-root service account present.
- Runtime venv built from `requirements.txt`; offline unit tests pass.
- `WORKFLOW_DB_PATH` prepared under `/var/lib/dztgbot` with service ownership and mode `0600`.
- Migration preflight reports the current schema version.
- Existing DB (if any) backed up under `/var/lib/dztgbot/backups/`.
- `dztgbot.service` reaches `active (running)`.

Set local shell placeholders (not secrets):

```bash
DZTGBOT_PROJECT_DIR=/TODO_REPLACE_WITH_ABSOLUTE_PROJECT_DIRECTORY
DZTGBOT_VENV_PYTHON=/TODO_REPLACE_WITH_ABSOLUTE_VENV_PYTHON
DZTGBOT_ENV_FILE=/TODO_REPLACE_WITH_ABSOLUTE_ENVIRONMENT_FILE
DZTGBOT_SERVICE_USER=TODO_REPLACE_WITH_SERVICE_USER
DZTGBOT_WORKFLOW_DB=/var/lib/dztgbot/workflow.sqlite3
```

Verify without printing environment values:

```bash
cd "$DZTGBOT_PROJECT_DIR"
"$DZTGBOT_VENV_PYTHON" --version
"$DZTGBOT_VENV_PYTHON" -m pip check
test -r requirements.txt
test -r requirements-dev.txt
test -r config/jira_rules.example.txt
test -r docs/operations/workflow-db-runbook.md
test -r docs/security/credential-threat-model.md
sudo test -r "$DZTGBOT_ENV_FILE"
sudo test -f "$DZTGBOT_WORKFLOW_DB"
sudo stat -c '%a %U' "$DZTGBOT_WORKFLOW_DB"
sudo -u "$DZTGBOT_SERVICE_USER" test -r src/dztgbot/__main__.py
sudo -u "$DZTGBOT_SERVICE_USER" test -r "$DZTGBOT_WORKFLOW_DB"
```

Expected:

- Python 3.12.x
- `pip check` clean for the **runtime** venv
- Workflow DB mode `600` owned by the service user
- All `test` commands succeed silently

Check unresolved required placeholders without printing secrets:

```bash
sudo awk -F= '$2 ~ /^TODO_/ {print $1 "=<unresolved>"}' "$DZTGBOT_ENV_FILE"
```

Before a live Telegram/Jira window, these must **not** appear unresolved:

- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `TELEGRAM_ADMIN_USER_IDS`
- `JIRA_RULES_PATH`
- `JIRA_URL`
- `WORKFLOW_DB_PATH`

`GEMINI_MODEL` must not be required. If present as a leftover key, it is ignored by the application.

### Quality-tool truthfulness

| Gate | Where it runs | Command family |
| --- | --- | --- |
| Runtime deps + offline unittest | Deploy + local | `requirements.txt`, `python -m unittest discover` |
| Ruff / Mypy / coverage / ShellCheck | CI / developer workstation | `requirements-dev.txt`, `.github/workflows/quality.yml` |

Deploy does **not** install ruff/mypy/coverage into the production venv. CI does **not** run `scripts/deploy.sh` against a live host and does **not** mutate Jira.

## 3. Placeholder fail-fast

Uses the tracked example only; does not contact Telegram, Gemini, or Jira:

```bash
cd "$DZTGBOT_PROJECT_DIR"
cp .env.example .env
chmod 0600 .env
PYTHONPATH=src "$DZTGBOT_VENV_PYTHON" -m dztgbot
```

Expected:

- Startup fails because `TELEGRAM_BOT_TOKEN` (and/or other required values) remain placeholders.
- No network service remains running.
- `.env` stays Git-ignored.

Remove the local copy before systemd tests:

```bash
rm .env
```

## 4. Private test configuration checklist

```bash
sudoedit "$DZTGBOT_ENV_FILE"
```

Key-only checklist (replace values only inside the protected file):

```dotenv
TELEGRAM_BOT_TOKEN=TODO_SUPPLY_PRIVATE_TEST_BOT_TOKEN_ON_SERVER
GEMINI_API_KEY=TODO_SUPPLY_PRIVATE_TEST_GEMINI_KEY_ON_SERVER
GEMINI_TIMEOUT_SECONDS=30
TELEGRAM_ADMIN_USER_IDS=TODO_SUPPLY_AUTHORISED_NUMERIC_USER_IDS_ON_SERVER
TELEGRAM_CONCURRENT_UPDATES=4
# TELEGRAM_ALLOWED_USER_IDS=
JIRA_RULES_PATH=/var/lib/dztgbot/jira_rules.txt
WORKFLOW_DB_PATH=/var/lib/dztgbot/workflow.sqlite3
JIRA_URL=TODO_SUPPLY_HTTPS_JIRA_BASE_URL_ON_SERVER
JIRA_VERIFY_SSL=true
# JIRA_CA_BUNDLE_PATH=
# JIRA_DEFAULT_PROJECT_KEY=
# USER_CREDENTIALS_PATH=
AUTH_TTL_SECONDS=180
AUTH_PAT_ONLY=true
PRIVATE_CHAT_ONLY=true
MAX_BATCH_MESSAGES=20
MAX_MESSAGE_CHARACTERS=8000
MAX_PROMPT_CHARACTERS=32000
MAX_ATTACHMENT_BYTES=10485760
MAX_ATTACHMENT_COUNT=10
MAX_QUEUE_SIZE=100
MAX_CONCURRENT_GEMINI=2
MAX_CONCURRENT_JIRA=4
VPN_ENABLED=false
VPN_CONNECTION_NAME=TODO_SUPPLY_PRIVATE_CONNECTION_NAME_WHEN_VPN_IS_ENABLED
VPN_PROFILE_PATH=TODO_SUPPLY_PRIVATE_ABSOLUTE_PROFILE_PATH_WHEN_VPN_IS_ENABLED
VPN_ALLOW_START=false
VPN_NMCLI_BIN=/usr/bin/nmcli
VPN_SUDO_BIN=/usr/bin/sudo
VPN_COMMAND_TIMEOUT_SECONDS=10
LOG_LEVEL=INFO
```

Seed rules if needed:

```bash
sudo install \
  -o "$DZTGBOT_SERVICE_USER" \
  -g "$DZTGBOT_SERVICE_USER" \
  -m 0600 \
  "$DZTGBOT_PROJECT_DIR/config/jira_rules.example.txt" \
  /var/lib/dztgbot/jira_rules.txt
```

## 5. Start and observe the service

```bash
sudo systemctl restart dztgbot.service
sudo systemctl status dztgbot.service --no-pager
sudo journalctl -u dztgbot.service -n 80 --no-pager
```

Expected:

- State `active (running)`.
- Journal contains a running banner (composition-root startup).
- Journal may include initial VPN state enum/text without endpoints or secrets.
- No token, PAT, forwarded body, generated description, or private URL appears.

Follow logs:

```bash
sudo journalctl -u dztgbot.service -f
```

Workflow DB recovery and restart notes: [`docs/operations/workflow-db-runbook.md`](operations/workflow-db-runbook.md).

## 6. Auth, privacy, and allowlist

### 6.1 Private PAT auth happy path

1. Open a **private** chat with the test bot.
2. Send `/start` then `/auth`.
3. Send a disposable test PAT only if this host may contact the real Jira (supervised).
4. Confirm the bot deletes the credential message when permitted.
5. Confirm local success messaging without echoing the PAT.

### 6.2 Reject non-PAT shapes

In `/auth`, try non-secret synthetic shapes (not real passwords), for example an obvious `user:password` form. Expected: rejection, no store, conversation remains safe or ends per TTL rules.

### 6.3 Auth TTL

Start `/auth`, wait longer than `AUTH_TTL_SECONDS`, then send text. Expected: late input is not accepted as a credential; user is told to restart `/auth`.

### 6.4 Group / non-private refusal

From a group: `/auth`, `/new`, forward a message, press an old callback if available, `/rules`. Expected: private-only warnings or non-disclosing redirects; **no** rules body, auth status, or Jira mutation.

### 6.5 Unauthorised admin

From a non-admin private account, run `/rules`. Expected: fixed unauthorised message; no rules disclosure.

## 7. Intake, media, and analysis

### 7.1 Ordinary message

Send ordinary non-forward text outside edit mode. Expected: no analysis (or only documented help/menu behaviour); no Jira call.

### 7.2 Direct forward

Forward a harmless placeholder message. Expected progress toward analysis and a durable review preview (HTML), with draft controls bound to callback tokens—not bare action names.

### 7.3 Reply-to-forward

Reply to a forward; analysis uses the forward content, not the reply text alone.

### 7.4 Batching

Forward several messages quickly in the same private chat. Expected: bounded batch (config `MAX_BATCH_MESSAGES`) and one preview for the sealed batch.

### 7.5 Media boundaries

| Case | Expected |
| --- | --- |
| Photo with caption | Caption participates in analysis; photo eligible for post-create attach |
| Captionless photo | Limited analysis signal; user understands model did not “see” pixels |
| Document / video / voice | Not multimodal; **not** uploaded as Jira attachments in this release |
| Oversized photo | Rejected by configured byte bound |

### 7.6 Gemini failure (safe invalid key)

Temporarily set a non-secret invalid key:

```dotenv
GEMINI_API_KEY=TEST_ONLY_INVALID_GEMINI_KEY
```

Restart, forward once, expect a safe user-facing failure and journal error **type** without body/PAT. Restore the real test key afterward.

## 8. Draft controls (no Jira mutation required)

On a review preview:

1. Toggle issue type and priority — preview updates; state remains review.
2. Edit flow — returns to review with revised template fields.
3. Cancel — terminal cancelled state; buttons stop working (token one-shot / state checks).
4. Stale callback — old message button after cancel/new draft is denied without mutating another draft.

Restart the service mid-review and confirm the draft still exists (SQLite durability) per the runbook restart section.

## 9. Supervised Jira create / update / attach / reconcile

**Stop here unless a human explicitly approves live Jira mutations in a test project.**

### 9.1 Create with confirmation

1. Authenticate with a PAT that can create issues in the designated **test** project only.
2. Ensure VPN policy matches the environment (`VPN_ENABLED` and an already-validated tunnel if required). Do not auto-start full tunnel from deploy.
3. Produce a review preview from forward or `/new`.
4. Press **confirm** once.
5. Expected: submitting progress; on success, published card with issue key/title/link controls.
6. Confirm the draft is no longer freely re-creatable via the same token.

### 9.2 Attachment (photo only)

Include a small photo in the draft path. After create, expect attach state transitions; partial failure should surface without inventing a new issue.

### 9.3 Retryable failure

Induce a safe permission or validation failure against the test project. Expected: draft retained in a retryable state with retry/cancel—not silent loss.

### 9.4 Unknown create outcome (reconciliation)

Only in a controlled test: force an ambiguous timeout if operable in that environment, **or** rehearse operator steps using the runbook against a draft already in `SUBMISSION_UNKNOWN` from offline fixtures. Expected:

- No automatic second create.
- Reconcile control searches/binds existing issue or marks not-created before retry is allowed.

### 9.5 Published update

Edit a just-created test issue through the bot. Expected: diff-based update path; unknown update outcomes require reconciliation rather than blind re-PUT storms.

### 9.6 Keyed concurrency (supervised)

With `TELEGRAM_CONCURRENT_UPDATES>1`, operate two private workflows for the same user (or two users). Expected: progress remains isolated; global concurrency never exceeds configured Gemini/Jira limits (observe offline metrics tests + qualitative live behaviour).

## 10. Admin command reference

Only numeric IDs in `TELEGRAM_ADMIN_USER_IDS`, and only in **private** chat.

### `/rules`

- Shows current runtime rules (chunked).
- Never dumps rules in groups.

### `/setrules`

- Inline text or reply-to text/caption.
- Atomic replace with previous retention; failure keeps prior rules.

### `/vpn`

- Read-only status. Never prints endpoints, PSK, username, password, or command stderr.

### `/vpnstart`

- Disabled when `VPN_ALLOW_START=false`.
- When enabled, uses narrow sudoers for exact `nmcli connection load <profile>` and `nmcli connection up <name>` only.
- Never returns secret material.

## 11. VPN scenarios (non-destructive defaults)

### 11.1 VPN disabled

`VPN_ENABLED=false`. Admin `/vpn` reports support disabled. Preview analysis can still proceed; Jira may fail later without connectivity.

### 11.2 VPN-down simulation (no real disconnect)

Point `VPN_CONNECTION_NAME` at a nonexistent test name with `VPN_ALLOW_START=false`, restart, check `/vpn` and `/vpnstart` safe messages, restore real settings afterward.

### 11.3 Already-active tunnel

Only after console validation. `/vpn` reports up. Do not start full tunnel solely because deploy ran.

## 12. Workflow DB operator checks (no content dump)

```bash
# Integrity (read-only)
sudo -u "$DZTGBOT_SERVICE_USER" "$DZTGBOT_VENV_PYTHON" - <<'PY'
import os, sqlite3
from pathlib import Path
path = Path(os.environ.get("DZTGBOT_WORKFLOW_DB", "/var/lib/dztgbot/workflow.sqlite3"))
# Prefer exporting DZTGBOT_WORKFLOW_DB in the shell to the configured path.
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print(conn.execute("PRAGMA integrity_check").fetchone()[0])
print("journal_mode", conn.execute("PRAGMA journal_mode").fetchone()[0])
conn.close()
PY
```

If integrity is not `ok`, stop the service and follow [`docs/operations/workflow-db-runbook.md`](operations/workflow-db-runbook.md). Do not print row payloads (they may contain operational metadata).

## 13. Completion checklist

- [ ] Deploy active on Ubuntu 24.04; workflow DB `0600` under `/var/lib/dztgbot`
- [ ] Placeholder fail-fast confirmed
- [ ] Private PAT auth, TTL, and non-PAT rejection confirmed
- [ ] Group/non-private paths do not disclose rules or mutate Jira
- [ ] Forward / reply-to-forward / batch / media boundary behaviours match §7
- [ ] Draft controls + stale callbacks safe without Jira
- [ ] Supervised create/update/attach/reconcile executed only with human approval
- [ ] Unknown-outcome path never blind-duplicates creates
- [ ] Admin authz private-only
- [ ] VPN tests did not unsupervised-start full tunnel
- [ ] Journal shows no secrets or message bodies
- [ ] Temporary invalid Gemini / fake VPN values restored
- [ ] No `.env`, `.nmconnection`, VPN XML, PAT, or private rules staged in Git
- [ ] Residual live items not executed are listed as unverified (not claimed green)

## 14. Residual external verification (default unproven)

Until a named supervised session records otherwise, treat as **unverified externally**:

- BotFather production token behaviour and webhook-free polling stability under real load
- Gemini quota/fallback on the operator’s Google project
- Jira Server/Data Center field configuration, permissions, and attachment size policy
- NetworkManager L2TP/IPsec full-tunnel recovery and split routing to Telegram/Gemini
- systemd restart under host OOM / disk-full conditions
- Backup restore drill on the production volume

## 15. Audit weakness coverage (E2E focus)

Maps high-risk review themes to plan sections (implementation claimed only where offline code/tests exist; live columns require §9–§11 execution):

| Review theme | Offline/unit | This plan section |
| --- | --- | --- |
| Durable draft + callback identity | Yes | §8 |
| FSM + unknown submission | Yes | §9.4, runbook |
| Keyed concurrency | Yes | §9.6 |
| PAT-only / TTL / private | Yes | §6 |
| Media honesty | Partial UX | §7.5 |
| Mutation recovery | Yes | §9.3–§9.5 |
| Docs / deploy drift | P8-G docs | §2, README |
| Credential at-rest boundary | Host `0600` | threat model doc |
| Live multi-tenant proof | No | Residual §14 |
