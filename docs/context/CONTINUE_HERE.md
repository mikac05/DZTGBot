# Continue here

## Current status

The complete read-only Telegram workflow, UX, resilience, maintainability, and
operations audit is saved at
`docs/reviews/telegram-bot-end-to-end-review-2026-08-07.md`.

No application code was changed. The review concluded that the current bot is a
credible private, serial-use pilot but is not safe for multi-user/group
production use yet.

Highest-priority observed risks:

1. Generic callbacks resolve against one mutable latest draft or
   `last_published` issue per Telegram user, not the object displayed by the
   button.
2. Draft and batch state is per user rather than per chat/workflow, so concurrent
   chats and simultaneous drafts can collide.
3. `ConversationHandler` is combined with concurrent update processing.
4. Jira creation removes the draft before the request, so a failure loses the
   retry context; ambiguous timeouts can cause duplicates.
5. Published-issue editing can target the latest issue from an older button,
   updates only a subset of fields, and loses the update target on failure.
6. Authentication has no timeout, accepts passwords/session cookies, and
   silently ignores Telegram credential-message deletion failure.
7. Media capability is misleading: Gemini receives no media bytes and only
   Telegram photos are uploaded to Jira.
8. Tracked README, test-plan, deployment, VPN, and stable-context statements had
   contradictory current-state claims. The audit records these discrepancies;
   application documentation outside `docs/context/` has not yet been repaired.

Local audit verification on 2026-08-07:

- 30/30 offline unit tests passed with Python 3.12.13.
- Source compilation passed.
- Repository context and secret-safety validation passed.
- The Windows virtual environment failed `pip check` for two platform-specific
  installations; treat this as local environment drift until recreated.
- No live Telegram, Gemini, Jira, VPN, systemd, or remote-server verification was
  performed during the audit.

## Exact next action

After running `DZTGBot continue` on the other computer, read the saved review and
ask the user to approve the first remediation batch. The recommended first batch
is: unique draft IDs plus owner/chat binding, an explicit workflow state machine,
PTB-compatible stateful concurrency, failure-preserving Jira submission with
duplicate protection, and handler-level tests for those invariants.

Do not implement that batch until the user confirms the scope.

## Inputs still required from the user or target environment

- Approval of the first implementation batch and its priority order.
- Decision whether group-chat creation remains supported or the bot becomes
  private-chat-only for mutating workflows.
- Decision whether authentication will be PAT-only.
- Decision whether durable draft/submission state should use SQLite or another
  approved transactional store.
- Fresh target-environment access only if the user asks to verify or redeploy the
  live service.

## Do not redo

- Do not repeat the full repository workflow audit unless code changed in the
  audited areas or contradictory evidence appears.
- Do not treat the prior deployment claim as freshly verified on a new computer.
- Do not reconfigure NetworkManager, VPN routing, systemd, Telegram BotFather,
  Jira, or Gemini without explicit authorization and target-environment access.
- Do not remove the existing lazy VPN checks in `JiraClient` as part of the state
  remediation.
- Do not create Jira issues during development without an explicit approved test
  phase.

## Required verification on resume

- Confirm the handoff commit is the checked-out `HEAD` after
  `python scripts/handoff.py continue`.
- Read this file, `PROJECT_CONTEXT.md`, and the saved review before proposing
  code changes.
- Recreate or repair the local Python 3.12 virtual environment if `pip check`
  still reports platform-incompatible packages.
- Run `PYTHONPATH=src python -m unittest discover -s tests -v` before and after
  any implementation.
- Verify external service state only when authorized; repository files do not
  transfer credentials or authenticated sessions.
