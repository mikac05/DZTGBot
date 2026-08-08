# Workflow SQLite operator runbook

**Audience:** host operators for a single Ubuntu 24.04 DZTGBot instance.  
**Database:** durable workflow authority (`SQLiteWorkflowRepository`) for drafts, callback token hashes, submission attempts, attachments, and published issue metadata.  
**Placeholders only:** replace path and account tokens locally. Never paste secrets, PATs, or row payloads into tickets or chat logs.

Related:

- Deploy: `scripts/deploy.sh`
- Unit: `deploy/systemd/dztgbot.service`
- Settings: `WORKFLOW_DB_PATH` in `.env.example` / protected environment file
- Credential boundary: `docs/security/credential-threat-model.md`
- Supervised product tests: `docs/end-to-end-test-plan.md`

## 1. Standard production layout

| Item | Required value pattern |
| --- | --- |
| State directory | `/var/lib/dztgbot` mode `0700`, owner service user |
| Workflow DB | `/var/lib/dztgbot/workflow.sqlite3` (example) mode `0600` |
| WAL / SHM companions | Same directory, mode `0600` when present |
| Backups | `/var/lib/dztgbot/backups/` mode `0700` |
| Rules | `/var/lib/dztgbot/jira_rules.txt` mode `0600` |
| User credentials JSON | `/var/lib/dztgbot/user_credentials.json` mode `0600` (default) |
| Environment file | Root-owned `0600` **outside** checkout |

Shell placeholders used below:

```bash
DZTGBOT_SERVICE_USER=TODO_REPLACE_WITH_SERVICE_USER
DZTGBOT_PROJECT_DIR=/TODO_REPLACE_WITH_ABSOLUTE_PROJECT_DIRECTORY
DZTGBOT_VENV_PYTHON=/TODO_REPLACE_WITH_ABSOLUTE_VENV_PYTHON
DZTGBOT_ENV_FILE=/TODO_REPLACE_WITH_ABSOLUTE_ENVIRONMENT_FILE
DZTGBOT_WORKFLOW_DB=/var/lib/dztgbot/workflow.sqlite3
DZTGBOT_BACKUP_DIR=/var/lib/dztgbot/backups
```

**Hard requirements:**

- Absolute path on **local** disk.
- Outside the Git checkout.
- Outside cloud-sync / network filesystems (deploy and application refuse known markers; operators must not bypass with exotic mounts).
- Direct child of `/var/lib/dztgbot` so systemd `ReadWritePaths` applies.
- Not a symbolic link.

## 2. Install and permissions

Prefer the installer:

```bash
sudo \
  DZTGBOT_SERVICE_USER="$DZTGBOT_SERVICE_USER" \
  DZTGBOT_ENV_FILE="$DZTGBOT_ENV_FILE" \
  bash "$DZTGBOT_PROJECT_DIR/scripts/deploy.sh"
```

Deploy will:

1. Ensure `/var/lib/dztgbot` exists (`0700`, service owner).
2. Validate free space (minimum deploy gate) and writability.
3. Integrity-check an existing DB, create a consistent SQLite backup, run migration preflight as the service user.
4. Enforce DB file mode `0600` and service ownership.
5. Restart `dztgbot.service` with a bounded active wait.

Manual permission correction (only if deploy is not available):

```bash
sudo install -d -o "$DZTGBOT_SERVICE_USER" -g "$DZTGBOT_SERVICE_USER" -m 0700 /var/lib/dztgbot
sudo install -d -o "$DZTGBOT_SERVICE_USER" -g "$DZTGBOT_SERVICE_USER" -m 0700 "$DZTGBOT_BACKUP_DIR"
sudo chown "$DZTGBOT_SERVICE_USER:$DZTGBOT_SERVICE_USER" "$DZTGBOT_WORKFLOW_DB"
sudo chmod 0600 "$DZTGBOT_WORKFLOW_DB"
```

Confirm the service can read/write and that the file is not world-readable:

```bash
sudo -u "$DZTGBOT_SERVICE_USER" test -r "$DZTGBOT_WORKFLOW_DB"
sudo -u "$DZTGBOT_SERVICE_USER" test -w "$DZTGBOT_WORKFLOW_DB"
sudo stat -c '%a %U %G %n' "$DZTGBOT_WORKFLOW_DB"
```

## 3. Preflight checks

### 3.1 Configuration

```bash
sudo awk -F= '$1 ~ /^(WORKFLOW_DB_PATH|JIRA_RULES_PATH|JIRA_URL)$/ {
  if ($2 ~ /^TODO_/) print $1 "=<unresolved>"; else print $1 "=<set>"
}' "$DZTGBOT_ENV_FILE"
```

### 3.2 Disk space and read-only mount

```bash
df -h /var/lib/dztgbot
findmnt -T /var/lib/dztgbot
# Fail if the mount options include ro
```

If the filesystem is read-only or critically full, **do not** start the bot. Free space or remediate the mount first (see §7).

### 3.3 Integrity and journal mode

Stop the service for a cold check when diagnosing corruption. For a quick read-only check:

```bash
sudo systemctl stop dztgbot.service
sudo -u "$DZTGBOT_SERVICE_USER" "$DZTGBOT_VENV_PYTHON" - <<'PY'
import sqlite3
from pathlib import Path
path = Path("/var/lib/dztgbot/workflow.sqlite3")  # TODO: align with WORKFLOW_DB_PATH
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    print("integrity_check", conn.execute("PRAGMA integrity_check").fetchone()[0])
    print("journal_mode", conn.execute("PRAGMA journal_mode").fetchone()[0])
    print("user_version", conn.execute("PRAGMA user_version").fetchone()[0])
    print("foreign_keys", conn.execute("PRAGMA foreign_keys").fetchone()[0])
finally:
    conn.close()
PY
```

Expected healthy production: `integrity_check ok`, `journal_mode wal` (application initializes WAL).

## 4. Schema, WAL, and migrations

- Schema history tables and SQL migrations live under `src/dztgbot/infrastructure/persistence/migrations/`.
- Current latest version is applied automatically on repository `initialize()`.
- Migration scripts are checksum-recorded; tampering with applied migration text fails closed.
- Unknown future `user_version` fails closed (`workflow_schema_version_unknown`).

**Preflight during deploy** runs as the service user and prints a non-secret `workflow_migration_preflight_ok schema_version=N` line.

**Do not** hand-edit migration files on a live host to “force” upgrades. Ship code through Git and redeploy.

### WAL notes

- Companion files: `*-wal`, `*-shm`.
- Always back up with the SQLite backup API or a clean stop + copy of **main + wal + shm** together.
- Deleting only the main file while leaving WAL can lose or corrupt data.

## 5. Safe backup

### 5.1 Online-safe logical backup (preferred)

With the service **stopped** (deploy does this automatically before maintenance):

```bash
sudo systemctl stop dztgbot.service
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST="$DZTGBOT_BACKUP_DIR/workflow-${STAMP}.sqlite3"
sudo -u "$DZTGBOT_SERVICE_USER" "$DZTGBOT_VENV_PYTHON" - "$DZTGBOT_WORKFLOW_DB" "$DEST" <<'PY'
import sqlite3, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
try:
    destination = sqlite3.connect(dst)
    try:
        source.backup(destination)
    finally:
        destination.close()
finally:
    source.close()
print("backup_ok")
PY
sudo chmod 0600 "$DEST"
sudo chown "$DZTGBOT_SERVICE_USER:$DZTGBOT_SERVICE_USER" "$DEST"
sudo systemctl start dztgbot.service
```

### 5.2 Backup hygiene

- Treat backups as **secret** (draft text + operational metadata; credentials live in a sibling JSON file—back that up separately with equal care).
- Do not sync `/var/lib/dztgbot` to OneDrive/Dropbox/NAS without an approved secret-handling procedure.
- Retain a small rotation of backups; delete older files deliberately after restore drills.

## 6. Safe restore

1. Announce maintenance; stop accepting operator creates if needed.
2. `sudo systemctl stop dztgbot.service`
3. Integrity-check the **backup** file (read-only pragma).
4. Move the current DB aside (do not delete until success):

```bash
sudo systemctl stop dztgbot.service
RECOVERY_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo mv "$DZTGBOT_WORKFLOW_DB" "${DZTGBOT_WORKFLOW_DB}.pre-restore-${RECOVERY_STAMP}"
# Move companions if present
for suffix in -wal -shm -journal; do
  if sudo test -e "${DZTGBOT_WORKFLOW_DB}${suffix}"; then
    sudo mv "${DZTGBOT_WORKFLOW_DB}${suffix}" "${DZTGBOT_WORKFLOW_DB}${suffix}.pre-restore-${RECOVERY_STAMP}"
  fi
done
sudo -u "$DZTGBOT_SERVICE_USER" cp -- "$DZTGBOT_BACKUP_DIR/TODO_REPLACE_WITH_BACKUP_FILENAME.sqlite3" "$DZTGBOT_WORKFLOW_DB"
sudo chown "$DZTGBOT_SERVICE_USER:$DZTGBOT_SERVICE_USER" "$DZTGBOT_WORKFLOW_DB"
sudo chmod 0600 "$DZTGBOT_WORKFLOW_DB"
```

5. Run migration preflight by starting the app once or re-running deploy’s DB section / full deploy.
6. `sudo systemctl start dztgbot.service` and verify `active`.
7. Spot-check with a **private** non-production draft; do not mass-retry unknown submissions.

## 7. Disk-full and read-only recovery

### Symptoms

- Service crash-loop; journal shows OSError / database disk image errors / write failures.
- Deploy aborts with insufficient free space or write-probe failure.
- SQLite errors during attempt claims or state CAS.

### Actions

1. `sudo systemctl stop dztgbot.service`
2. `df -h` / `df -i` on `/var/lib/dztgbot`
3. Free space: old journal logs (`journalctl --vacuum-time=...`), old backups you have verified elsewhere, package cache — **not** random deletes of `*-wal`
4. If mount is `ro`, remediate filesystem / hardware; remount `rw` only after fsck policy
5. Integrity-check DB; restore from backup if corrupt
6. Confirm free space above a comfortable floor (deploy gate uses ≥ 256 MiB; production should keep more headroom)
7. Start service; watch journal for stable active state

## 8. Corruption recovery

If `PRAGMA integrity_check` ≠ `ok`:

1. Keep the service stopped.
2. Copy the broken file tree to a quarantined name under `/var/lib/dztgbot/backups/` for forensics (mode `0600`).
3. Restore the newest known-good backup (§6).
4. If no backup exists, the recovery path is **data loss** for in-flight drafts; recreate empty DB only by removing the corrupt file after quarantine and allowing migration preflight to recreate schema — users must re-auth only if credentials JSON is intact; drafts are gone.
5. Do not run experimental `sqlite3 .recover` in production without an offline copy and an explicit decision.

Never log or export full table dumps into chat; they may include message-derived text.

## 9. Unknown submission outcomes and reconciliation

Application behaviour (do not bypass with raw SQL):

| State | Meaning | Operator / user action |
| --- | --- | --- |
| `SUBMISSION_UNKNOWN` | Create HTTP outcome ambiguous | User uses in-bot **reconcile** control; no second create until resolved |
| `UPDATE_UNKNOWN` | Update outcome ambiguous | Reconcile update against Jira; do not spam PUT |
| `SUBMISSION_RETRYABLE` / `UPDATE_RETRYABLE` | Known failure | Retry or cancel in-bot |
| `ABANDONED_UNKNOWN` | Terminal abandoned unknown | Investigate offline; do not re-open via SQL hacks |

**Forbidden:** manually flipping state columns to `REVIEW` to “force retry” after an unknown create — that reintroduces duplicate-issue risk the FSM was built to prevent.

If the UI is unavailable, stop the service and escalate with draft IDs from privacy-safe metrics/logs only (no body text). Prefer restoring a pre-incident backup over surgical SQL.

## 10. Retention

- Expired callback token rows may be deleted by application helpers (`delete_expired_callbacks`); this does not delete drafts by itself.
- Terminal drafts (`CANCELLED`, `EXPIRED`, `COMPLETE`) are retention candidates for a future automated purge; until a product-approved purge job exists, operators should **not** DELETE rows ad hoc.
- Credentials JSON is separate; `/logout` removes one user locally and does not revoke Jira PATs.

## 11. Service restart behaviour

| Event | Expected behaviour |
| --- | --- |
| `systemctl restart dztgbot.service` | SIGTERM, graceful stop (polling → app → keyed processor → limiter → scheduler → gateways → repo), then start |
| Deploy | Stops unit before venv/DB maintenance; `reset-failed`; start or restart; waits until active or fails with journal tail |
| Crash | `Restart=on-failure` with `RestartSec=5s`; `StartLimitBurst` inside `StartLimitIntervalSec` |
| Host reboot | `WantedBy=multi-user.target` brings the unit back if enabled |

After restart, durable drafts and published metadata remain in SQLite. In-memory auth conversations and ephemeral Telegram UI state do not.

Verification:

```bash
sudo systemctl restart dztgbot.service
sudo systemctl is-active dztgbot.service
sudo journalctl -u dztgbot.service -n 40 --no-pager
```

## 12. Monitoring (privacy-safe)

Watch for:

- `systemctl is-failed dztgbot.service`
- Restart rate (`systemctl show dztgbot.service -p NRestarts`)
- Disk free on the state filesystem
- Journal patterns: configuration errors, `workflow_*` repository errors, integrity failures — **types only**
- Application privacy-safe counters if exposed in future admin tooling (`SafeMetrics`) — never label metrics with PATs or message text

There is no built-in external pager integration in first release.

## 13. Migration rollback

SQLite migrations in this project are **forward** checksummed scripts.

Rollback options, ordered by preference:

1. **Code rollback + compatible schema:** if the new code has not applied a new migration version, redeploy the previous release tag.
2. **Restore pre-deploy backup:** deploy creates `workflow-*.sqlite3` under backups before migration; restore §6, then run the older code.
3. **No down-migration scripts:** do not invent reverse SQL on production.

Always integrity-check after rollback restore.

## 14. Recovery command pocket card

```bash
# Status
sudo systemctl status dztgbot.service --no-pager

# Stop / start
sudo systemctl stop dztgbot.service
sudo systemctl start dztgbot.service

# Follow logs (no secrets expected)
sudo journalctl -u dztgbot.service -f

# Permissions snapshot
sudo stat -c '%a %U %G %n' /var/lib/dztgbot "$DZTGBOT_WORKFLOW_DB"

# Integrity
sudo systemctl stop dztgbot.service
sudo -u "$DZTGBOT_SERVICE_USER" "$DZTGBOT_VENV_PYTHON" -c "import sqlite3; c=sqlite3.connect('file:/var/lib/dztgbot/workflow.sqlite3?mode=ro', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"

# Redeploy after fix
sudo DZTGBOT_SERVICE_USER="$DZTGBOT_SERVICE_USER" DZTGBOT_ENV_FILE="$DZTGBOT_ENV_FILE" bash "$DZTGBOT_PROJECT_DIR/scripts/deploy.sh"
```

## 15. What this runbook does not authorize

- Live firewall, VPN tunnel, or NetworkManager topology changes beyond the existing product `/vpn` `/vpnstart` controls.
- Real Jira mutations without the supervised human-confirmation product path.
- Committing backups, env files, or DB copies into Git.
- Implementing credential encryption without the approved design in `docs/security/credential-threat-model.md` §6.
