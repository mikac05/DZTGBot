# End-to-end server test plan and admin command reference

This plan supports Ubuntu 22.04 only and contains no usable credentials, VPN endpoints, or Jira configuration.

## 1. Test boundaries

- Untouched `TODO_...` values are intentionally rejected during startup.
- A real Telegram exchange cannot be completed with a placeholder bot token. Supply a developer-owned test bot token privately through the protected environment file.
- A Gemini success test requires a privately supplied test API key and a supported model configured on the server.
- Never paste secrets into a command, terminal history, Telegram message, log, screenshot, or tracked file.
- Use a dedicated test Telegram chat and placeholder Jira rules. No Jira connection or issue creation exists in this phase.
- Keep `VPN_ALLOW_START=false` throughout the simulated VPN-down test. Do not disconnect or reconfigure the real tunnel.

## 2. Server preflight

Run `scripts/deploy.sh` as documented in the README before this test plan. The deployment must complete successfully and report that `dztgbot.service` is active.

Set shell variables to the paths and account already chosen during deployment. These values are placeholders and must be replaced locally:

```bash
DZTGBOT_PROJECT_DIR=/TODO_REPLACE_WITH_ABSOLUTE_PROJECT_DIRECTORY
DZTGBOT_VENV_PYTHON=/TODO_REPLACE_WITH_ABSOLUTE_VENV_PYTHON
DZTGBOT_ENV_FILE=/TODO_REPLACE_WITH_ABSOLUTE_ENVIRONMENT_FILE
DZTGBOT_SERVICE_USER=TODO_REPLACE_WITH_SERVICE_USER
```

Verify the runtime without printing environment variables:

```bash
cd "$DZTGBOT_PROJECT_DIR"
"$DZTGBOT_VENV_PYTHON" --version
"$DZTGBOT_VENV_PYTHON" -m pip check
test -r requirements.txt
test -r config/jira_rules.example.txt
sudo test -r "$DZTGBOT_ENV_FILE"
sudo -u "$DZTGBOT_SERVICE_USER" test -r src/dztgbot/__main__.py
```

Expected results:

- Python reports version 3.12.x.
- `pip check` reports no broken requirements.
- Every `test` command exits successfully and prints nothing.

Check for unresolved required placeholders without printing configured secret values:

```bash
sudo awk -F= '$2 ~ /^TODO_/ {print $1 "=<unresolved>"}' "$DZTGBOT_ENV_FILE"
```

Before an actual Telegram test, the command must not report unresolved values for:

- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `TELEGRAM_ADMIN_USER_IDS`
- `JIRA_RULES_PATH`

## 3. Verify placeholder fail-fast behaviour

This check uses the tracked example only and does not contact Telegram or Gemini:

```bash
cd "$DZTGBOT_PROJECT_DIR"
cp .env.example .env
chmod 0600 .env
PYTHONPATH=src "$DZTGBOT_VENV_PYTHON" -m dztgbot
```

Expected result:

- Startup stops immediately with a configuration error stating that `TELEGRAM_BOT_TOKEN` must be supplied.
- No network service remains running.
- The `.env` file remains ignored by Git.

Remove the local placeholder copy before testing the systemd environment, so it cannot cause confusion:

```bash
rm .env
```

## 4. Prepare private test configuration

Edit the protected server file interactively:

```bash
sudoedit "$DZTGBOT_ENV_FILE"
```

Use this key-only checklist. Replace required placeholders inside the protected file; do not copy the completed file into the repository:

```dotenv
TELEGRAM_BOT_TOKEN=TODO_SUPPLY_PRIVATE_TEST_BOT_TOKEN_ON_SERVER
GEMINI_API_KEY=TODO_SUPPLY_PRIVATE_TEST_GEMINI_KEY_ON_SERVER
GEMINI_MODEL=TODO_SUPPLY_SUPPORTED_MODEL_ON_SERVER
GEMINI_TIMEOUT_SECONDS=30
TELEGRAM_ADMIN_USER_IDS=TODO_SUPPLY_AUTHORISED_NUMERIC_USER_IDS_ON_SERVER
TELEGRAM_CONCURRENT_UPDATES=4
JIRA_RULES_PATH=/var/lib/dztgbot/jira_rules.txt
VPN_ENABLED=false
VPN_CONNECTION_NAME=TODO_SUPPLY_PRIVATE_CONNECTION_NAME_WHEN_VPN_IS_ENABLED
VPN_PROFILE_PATH=TODO_SUPPLY_PRIVATE_ABSOLUTE_PROFILE_PATH_WHEN_VPN_IS_ENABLED
VPN_ALLOW_START=false
VPN_NMCLI_BIN=/usr/bin/nmcli
VPN_SUDO_BIN=/usr/bin/sudo
VPN_COMMAND_TIMEOUT_SECONDS=10
LOG_LEVEL=INFO
```

Seed placeholder-only rules if the runtime file is not already present:

```bash
sudo install \
  -o "$DZTGBOT_SERVICE_USER" \
  -g "$DZTGBOT_SERVICE_USER" \
  -m 0600 \
  "$DZTGBOT_PROJECT_DIR/config/jira_rules.example.txt" \
  /var/lib/dztgbot/jira_rules.txt
```

If the service group differs from the service user, replace the `-g` value with the configured service group.

## 5. Start and observe the service

```bash
sudo systemctl restart dztgbot.service
sudo systemctl status dztgbot.service --no-pager
sudo journalctl -u dztgbot.service -n 50 --no-pager
```

Expected result:

- The service state is `active (running)`.
- The journal contains `DZTGBot is running`.
- The journal contains an initial VPN state but no token, key, forwarded text, generated description, VPN endpoint, or VPN credential.

Follow logs during each test in a second terminal:

```bash
sudo journalctl -u dztgbot.service -f
```

## 6. Telegram intake tests

### 6.1 Ordinary-message rejection

1. Open the test bot chat using a Telegram client.
2. Send a new ordinary text message that is not forwarded and is not a reply to a forwarded message.

Expected result:

- The bot sends no reply.
- No Gemini request is made.

### 6.2 Simulate a direct forwarded message

1. Create a harmless placeholder message in a separate test chat or Saved Messages.
2. Use Telegram's **Forward** action to forward that message to the bot.
3. Do not copy and paste the text; copied text is an ordinary message and must be ignored.

The bot accepts the message when Telegram supplies `forward_origin`, including when Telegram hides part of the original sender identity.

### 6.3 Simulate a reply containing a forward

1. Forward a harmless placeholder message to the bot.
2. Reply directly to that forwarded message with an ordinary text reply.

Expected result:

- Both the direct forward and the direct reply to that forward are accepted.
- For the reply case, analysis uses the forwarded message's sender, chat, text or caption, and media type—not the reply text.
- Replies to ordinary non-forwarded messages are ignored.

## 7. Expected result scenarios

### 7.1 Gemini success while VPN support is disabled

Configuration:

```dotenv
VPN_ENABLED=false
VPN_ALLOW_START=false
```

Send a direct forward.

Expected Telegram replies, in order:

```text
Forward received. Analyzing...
```

```text
The VPN tunnel is unavailable, so Jira is temporarily unreachable. I will still prepare the task preview.
```

Then a preview with this structure:

```text
Jira task preview (not created)

Summary: <generated value>
Issue type: <generated value>
Priority: <generated value>
Project: <generated value or Not assigned>
Assignee: <generated value or Not assigned>
Labels: <generated values or None>
Components: <generated values or None>

Description:
<generated value>

Acceptance criteria:
- <generated value>
```

Verification:

- The preview is review-only and explicitly says `not created`.
- No Jira request is made.
- The preview is limited to 4,000 Telegram characters.

### 7.2 Full success with an already-active VPN

Perform this only after the private L2TP/IPsec profile has been console-tested and the tunnel is already active. Do not start the full tunnel solely for this test.

Configuration:

```dotenv
VPN_ENABLED=true
VPN_CONNECTION_NAME=TODO_SUPPLY_PRIVATE_ACTIVE_CONNECTION_NAME_ON_SERVER
VPN_PROFILE_PATH=TODO_SUPPLY_PRIVATE_ABSOLUTE_PROFILE_PATH_ON_SERVER
VPN_ALLOW_START=false
```

Restart the service and have an authorised administrator send `/vpn`. It must reply:

```text
L2TP/IPsec VPN tunnel is up.
```

Send a direct forward.

Expected Telegram replies:

1. `Forward received. Analyzing...`
2. The human-readable Jira preview.

The temporary Jira-unreachable warning must not appear.

### 7.3 Deterministic Gemini-failure test

Keep the private Telegram test token configured. Temporarily replace only the Gemini key in the protected environment file with this explicitly invalid, non-secret test value:

```dotenv
GEMINI_API_KEY=TEST_ONLY_INVALID_GEMINI_KEY
```

Restart and send a direct forward:

```bash
sudo systemctl restart dztgbot.service
```

Expected Telegram replies:

1. `Forward received. Analyzing...`
2. The Jira-unreachable warning if the VPN is disabled or down.
3. `Gemini analysis failed or returned an invalid result. Please try again later.`

Expected logging:

- The journal records `Gemini analysis failed` and an exception type.
- The forwarded text and credentials are not logged.

Restore the private test Gemini key using `sudoedit`, then restart the service. Do not leave the invalid test value deployed.

### 7.4 Safe VPN-down simulation

Do not stop the real VPN. Temporarily configure a deliberately nonexistent test connection:

```dotenv
VPN_ENABLED=true
VPN_CONNECTION_NAME=TEST_ONLY_MISSING_VPN_CONNECTION
VPN_PROFILE_PATH=/TEST_ONLY_MISSING_VPN_PROFILE.nmconnection
VPN_ALLOW_START=false
```

Restart the service:

```bash
sudo systemctl restart dztgbot.service
```

Expected administrator `/vpn` reply:

```text
L2TP/IPsec VPN tunnel is down.
```

Expected administrator `/vpnstart` reply:

```text
L2TP/IPsec VPN tunnel is down and remote start is disabled.
```

Expected forwarded-message flow:

1. `Forward received. Analyzing...`
2. `The VPN tunnel is unavailable, so Jira is temporarily unreachable. I will still prepare the task preview.`
3. A Jira preview when Gemini succeeds, or the Gemini failure message when it does not.

This verifies that a VPN outage does not block the Telegram handler or Gemini preview. Restore the intended private VPN settings with `sudoedit`, then restart the service.

## 8. Admin authorisation test

1. From a Telegram account whose numeric ID is listed in `TELEGRAM_ADMIN_USER_IDS`, run each command in the reference below.
2. From an account whose numeric ID is not listed, run one admin command.

Expected reply for every admin command sent by an unauthorised account:

```text
You are not authorised to manage this bot.
```

The unauthorised request must not read or change rules and must not start the VPN.

## 9. Final admin command reference

Only the numeric Telegram user IDs in `TELEGRAM_ADMIN_USER_IDS` may use these commands.

### `/rules`

- Loads the current rules file, using the in-memory last-known-good rules if an external reload fails.
- Replies first with `Current runtime Jira rules:` followed by the current rules text.
- Long rules are split into messages of at most 3,500 characters per chunk.
- Does not modify the rules.

### `/setrules <new rules>`

- Takes all non-empty text after the command as the replacement rules.
- Strips leading and trailing whitespace.
- Rejects a missing or whitespace-only value with:

```text
Provide non-empty rules after /setrules, or reply with /setrules to a text message.
```

- Atomically saves the current rules as the previous version, writes the new rules, reads them back, and activates them immediately without restarting the bot.
- On success replies:

```text
Rules updated and reloaded. The new rules are active now.
```

- If saving or validation fails, restores or retains the previous rules and replies:

```text
Rules update failed. The previous rules remain active.
```

### `/setrules` as a reply

- When `/setrules` has no inline value, it accepts the text or caption of the Telegram message being replied to.
- Behaviour, validation, backup, hot reload, and replies are otherwise identical to `/setrules <new rules>`.
- It does not accept non-text/non-caption media as rules.

### `/vpn`

- Performs a read-only NetworkManager status check for the configured connection.
- Never displays the VPN endpoint, username, password, pre-shared key, profile contents, or command error output.
- Possible exact replies are:

```text
L2TP/IPsec VPN support is disabled.
L2TP/IPsec VPN tunnel is up.
L2TP/IPsec VPN tunnel is down.
L2TP/IPsec VPN status could not be checked.
```

### `/vpnstart`

- Serialises concurrent start requests with an async lock.
- Returns immediately if the configured tunnel is already active or VPN support is disabled.
- Refuses startup when `VPN_ALLOW_START=false`.
- The root deployment gate requires the private profile to be a root-owned, regular, non-symlink file with mode `0600` in a root-managed directory.
- Uses non-interactive, narrowly authorised `sudo nmcli` commands to load the profile and activate the configured connection.
- Never returns command output or VPN secrets.
- Possible exact replies are:

```text
L2TP/IPsec VPN support is disabled.
L2TP/IPsec VPN tunnel is up.
L2TP/IPsec VPN tunnel is down and remote start is disabled.
The private VPN profile could not be loaded.
L2TP/IPsec VPN tunnel could not be started.
L2TP/IPsec VPN tunnel is down.
L2TP/IPsec VPN status could not be checked.
```

## 10. Completion checklist

- Ordinary messages receive no reply.
- Direct forwards and replies directly containing a forward are accepted.
- Gemini success produces a strict Jira preview and never creates a Jira issue.
- Gemini failure produces the documented user-facing error and a journal error without sensitive content.
- VPN-down simulation produces the Jira-unreachable warning while the bot remains responsive.
- All admin commands reject an unauthorised Telegram user ID.
- Rules update without a process restart and retain the previous version on failure.
- `systemctl status dztgbot.service` remains healthy after tests.
- Temporary invalid Gemini and nonexistent VPN test values are removed from the protected environment file.
- No `.env`, `.nmconnection`, XML VPN profile, credential, token, endpoint, or private rules file is staged in Git.
