# DZTGBot end-to-end workflow and UX review

Date: 2026-08-07

## Review boundary

This is a read-only review of the tracked repository at commit `e8be39b` before
the handoff documentation changes. It covers the Telegram handlers, implicit and
formal conversation state, Gemini analysis, Jira mutations, credential and rule
storage, VPN integration, deployment, tests, and operational documentation.

The review did not change application code and did not perform a live Telegram,
Gemini, Jira, VPN, systemd, or remote-server test. Previous handoff records report
a live deployment, but that external state was not revalidated during this review.

Local verification performed during the review:

- 30/30 offline unit tests passed with Python 3.12.13.
- Source compilation passed.
- The repository handoff and secret-safety validation passed.
- The working tree was clean before this report was added.
- The current Windows virtual environment failed `pip check` because
  `charset-normalizer 3.4.9` and `websockets 16.1.1` report unsupported-platform
  installations. This is a local-environment inconsistency, not a demonstrated
  failure on the separately managed Ubuntu deployment.

## 1. Project overview

DZTGBot is an asynchronous Python 3.12 Telegram bot built with
`python-telegram-bot` 22.8. It uses Gemini structured output to turn forwarded
Telegram messages into Jira templates, asks for human confirmation, and creates
or updates Jira Server/Data Center issues over REST API v2. It stores per-user
Jira credentials and runtime classification rules in atomic local files, manages
an L2TP/IPsec VPN through NetworkManager, and runs as a hardened non-root systemd
service.

The principal modules are:

- `src/dztgbot/__main__.py`: configuration, dependency construction, handler
  registration, polling lifecycle, and global error logging.
- `src/dztgbot/core.py`: forwarding, batching, manual drafts, callbacks, Jira
  creation/update, attachments, and most user-facing messages.
- `src/dztgbot/jira_auth.py`: onboarding, help, Jira credential binding, and
  logout.
- `src/dztgbot/analysis.py`: Gemini request, structured response parsing, preview
  rendering, editable-text parsing, and partial field validation.
- `src/dztgbot/jira_client.py`: Jira authentication, create, update, and
  attachment API calls.
- `src/dztgbot/rules.py` and `src/dztgbot/user_store.py`: atomic local state.
- `src/dztgbot/admin.py` and `src/dztgbot/vpn.py`: operator controls.
- `scripts/deploy.sh` and `deploy/systemd/dztgbot.service`: Ubuntu 24.04
  deployment and runtime hardening.

Only Jira credential collection uses a formal `ConversationHandler` state
(`AWAITING_PAT`). Drafting, batching, editing, attachments, and publication use
mutable keys in `context.user_data`, including `pending_template`,
`pending_photo_file_ids`, `editing_draft`, `pending_batch`, and `last_published`.

## 2. Workflow inventory

1. **Startup and polling**: load configuration, rules, credentials, VPN status,
   Jira/Gemini clients, handlers, and begin polling for messages and callbacks.
2. **Bot command setup**: intended to register `/start`, `/new`, `/auth`,
   `/logout`, and `/help` in `post_init`.
3. **Onboarding**: `/start` shows Jira binding status and a persistent menu.
4. **Help**: `/help` or the help button explains forwarding, manual creation,
   authentication, and logout.
5. **Group authentication redirect**: `/auth` in a non-private chat tells the
   user to move to private chat.
6. **Private authentication**: accept PAT, basic credentials, or a session
   cookie; delete the Telegram message; validate against Jira; store on success;
   retry or `/cancel` on failure.
7. **Logout**: remove the locally stored credential through `/logout` or the
   persistent button. This does not revoke the credential at Jira.
8. **Ordinary input**: silently ignore ordinary text/photos unless implicit
   manual-edit mode is active.
9. **Single forward**: accept a direct forward or a direct reply to a forward,
   normalize it, and open the batching window.
10. **Multi-forward batching**: merge up to 20 messages received within a
    2.5-second sliding window.
11. **Gemini analysis**: combine message text/media labels with current runtime
    Jira rules, request structured output, and try alternative models on detected
    rate limits.
12. **Unauthenticated preview**: show Edit/Cancel only and direct the user to
    authenticate separately.
13. **Inline draft controls**: toggle issue type, toggle priority, fully edit,
    confirm creation, or cancel.
14. **Quick manual draft**: `/new <title>` creates a default Task draft.
15. **Guided manual draft**: `/new` or the persistent menu collects a first-line
    title, description, optional photos, issue type, and priority.
16. **Full-text draft editing**: render a structured block, parse the returned
    text, partially validate it, and return a new preview.
17. **Jira creation**: remove the current draft from memory, start/check VPN,
    and POST the issue with the caller's credential.
18. **Attachment uploading**: download Telegram photos and upload them to the
    newly created Jira issue one at a time.
19. **Post-create actions**: display key/title/URL and offer link, summary, and
    published-issue editing actions.
20. **Published-issue editing**: use the user's single in-memory
    `last_published` record, parse edits, PUT a subset of fields, and optionally
    add photos.
21. **Rules viewing**: authorized administrators use `/rules` to read the active
    rule text.
22. **Rules replacement**: authorized administrators use `/setrules` inline or
    as a reply; the file is replaced atomically with one backup.
23. **VPN operations**: authorized administrators use `/vpn` or `/vpnstart`.
24. **Failure and restart**: handler failures are logged by exception type and
    systemd restarts the service; rules and credentials persist, while drafts,
    batches, auth conversations, and publication state do not.

Commands found: `/start`, `/new`, `/auth`, `/logout`, `/help`, `/cancel`,
`/rules`, `/setrules`, `/vpn`, and `/vpnstart`.

Callback actions found: `jira_confirm`, `jira_edit`, `jira_cancel`,
`jira_copylink`, `jira_copysummary`, `jira_editpublished`, `jira_toggle_type`,
and `jira_toggle_priority`.

## 3. UX and design assessment

### Onboarding, help, and menu

Strengths:

- Concise Taiwan Traditional Chinese onboarding.
- Authentication-aware persistent menu.
- Main actions are visible without remembering commands.

Weaknesses and friction:

- Help says a user can directly send title/description text, but ordinary input
  is ignored until `/new` has already enabled manual-edit mode.
- `/start` can disclose the caller's Jira display name in a group.
- Reply keyboards are not selective in groups and the manual-draft keyboard is
  not restored or removed after normal draft completion.
- The configured `post_init` command-registration callback is not called by the
  custom lifecycle. Commands may therefore be absent unless configured
  externally.
- There is no feedback, support, privacy, or retention path.

Verdict: initially clear, but misleading and not group-safe.

### Authentication and logout

Strengths:

- Credential collection is restricted to private chat.
- The input message is deleted immediately when Telegram permits it.
- Jira identity is validated before local storage.
- Local writes are atomic and mode `0600` on newly written files.

Weaknesses and friction:

- There is no conversation timeout. Any later ordinary text, including a menu
  button or forwarded text, can be interpreted as a credential and deleted.
- The application defaults to concurrent update processing even though
  `ConversationHandler` relies on sequential updates.
- Failure to delete the credential message is silently ignored.
- Supporting account passwords and browser session cookies creates more risk
  and expiry failure than a PAT-only design.
- `/myself` validates identity but not create permission or access to the target
  project and issue type.
- Authentication does not resume or refresh the pending unauthenticated draft.
- Logout removes only the server's local copy and does not revoke the credential
  at Jira.

Verdict: usable happy path, insufficiently safe and recoverable.

### Forward intake, batching, and AI analysis

Strengths:

- Clear receipt and analysis progress messages.
- Useful short batching window and explicit maximum batch size.
- Bounded preview and explicit statement that the draft is not yet created.
- Message content is treated as data rather than as model instructions.

Weaknesses and friction:

- There is one mutable batch and one current draft per Telegram user, not per
  chat or draft. The same user's activity in multiple chats can be combined and
  answered in the wrong chat.
- Callback data contains only an action name, not a draft identifier. An old
  button acts on the caller's newest mutable draft rather than the draft shown
  in the message.
- Raw `asyncio.create_task` is used, so batch-task errors and shutdown are not
  integrated with the Telegram application lifecycle.
- A photo is added to pending attachments before the 20-message cap is checked.
- There is no deduplication, cancel-analysis action, queue position, per-user
  quota, or global backpressure.
- Normalized sender/chat identities are discarded before the Gemini prompt.
- The fixed model fallback attempts alternatives only for detected rate limits;
  transient service failures and timeouts do not have bounded retry/backoff.

Verdict: efficient for one private user operating serially; unsafe under real
multi-chat or concurrent use.

### Media handling

Strengths:

- Telegram photos are counted in previews and uploaded after Jira creation.

Weaknesses and friction:

- Gemini receives no media bytes, only labels such as `photo`, `video`, or
  `voice`. A captionless image therefore contains no issue information for the
  model.
- Non-photo documents, audio, video, and voice are neither analyzed nor attached
  even though the intake normalizes and accepts their media type.
- The user is not told this capability boundary.

Verdict: actual behavior materially under-delivers the apparent media workflow.

### Manual creation and draft editing

Strengths:

- Both quick and guided creation modes are available.
- Full-text editing offers control over major Jira fields.
- Initial Jira creation always requires an inline confirmation.

Weaknesses and friction:

- Manual drafts hard-code project `NGSSA3`, ignoring configured defaults.
- The reply-keyboard label `Confirm submit` actually leads to another preview
  and a second confirmation step.
- The draft keyboard remains after normal completion and its later actions are
  silently ignored.
- The 15-minute timeout is checked only when another message arrives and can say
  that a draft was saved even when no usable recovery action exists.
- Preview strings use Markdown markers without a parse mode, so literal `**`
  can be displayed.
- Validation is applied only to edited manual templates and covers only summary,
  description, a fixed issue-type list, and project-key shape. AI and quick
  drafts bypass equivalent business validation.

Verdict: broad capability but over-complicated and internally inconsistent.

### Jira creation and attachments

Strengths:

- Human confirmation is the correct control before an external mutation.
- VPN connection is established lazily before Jira operations.
- Common authentication, permission, connectivity, and timeout errors have
  user-facing messages.
- Acceptance criteria are appended to the description during initial creation.

Weaknesses and friction:

- The pending draft is removed and its buttons are disabled before the Jira
  request. On failure the user has no Retry and the draft is lost.
- A create timeout is ambiguous: Jira may have committed the issue even though
  the bot did not receive the response. A manual retry can produce a duplicate.
- A rejected issue type is silently changed to `Task` without new user approval.
- Jira create metadata and required fields are not discovered before submission.
- Partial or total attachment failure is not clearly reported and has no retry
  or reconciliation path.
- Attachment errors log Telegram file IDs and exception text despite the stated
  privacy boundary.

Verdict: the happy path is clear; mutation recovery is not production-safe.

### Post-publication sharing and editing

Strengths:

- The success message gives a usable issue key, title, and URL.
- In-chat editing is valuable when it targets the correct issue.

Weaknesses and friction:

- Every historical Copy/Edit button reads the one latest `last_published`
  record. Pressing a button on an older result can copy or edit the newest issue.
- The Copy buttons return another preformatted message rather than using the
  Bot API's native copy-text button.
- Updating writes only summary, description, issue type, and priority. It omits
  labels, components, assignee, and acceptance criteria from the current
  template.
- The current Jira issue is not reloaded or diffed first, so a stored template
  can overwrite later human changes.
- The published issue key is removed before the update. After failure, the next
  edit can fall into a new-issue preview instead of retrying the update.
- All post-publication state disappears on restart; old Copy actions then fail
  silently and Edit/Confirm reports missing state inconsistently.

Verdict: useful concept, but currently unsafe.

### Admin, deployment, and operation

Strengths:

- Numeric user-ID authorization for administrative commands.
- Atomic rules replacement and last-known-good fallback.
- Serialized, narrowly scoped VPN startup.
- Strict Ubuntu 24.04 deployment gate, non-root service, root-owned secrets,
  narrow sudoers commands, and strong systemd hardening.
- Global error logging avoids serializing Telegram updates and error strings.

Weaknesses and friction:

- Admin commands can run in groups and can disclose internal rules.
- Rules have no schema, semantic validation, revision ID, diff, approval,
  rollback command, or staged test.
- There is no structured metrics layer, correlation ID, workflow outcome
  counter, functional health check, readiness probe, or automated alerting.
- There is no inbound or outbound rate limiter.
- The local JSON credential store is a single-node, all-users plaintext file
  with no backup, encryption layer, schema migration, or corruption recovery.
- `core.py` combines UI, implicit state, AI orchestration, Jira mutation, and
  attachment transfer, which makes handler-level testing and ownership difficult.
- The test suite has no complete Telegram handler journeys, concurrency tests,
  stale-callback tests, or mutation-recovery tests.

Verdict: strong host-security mechanics, weak product-operation and workflow
governance.

## 4. Overall design verdict

Well-designed areas:

- Explicit human approval before initial Jira creation.
- Async long polling appropriate for a private single-instance bot.
- Concise progress messaging on the central happy path.
- Atomic local state writes and restrictive permissions.
- Last-known-good runtime rules.
- Narrow VPN control and hardened systemd service.
- Privacy-conscious global error handler.
- Localized core interface.

Not well-designed areas:

- Draft and callback identity.
- Multi-chat and concurrent state isolation.
- Create/update failure recovery and idempotency.
- Post-publication editing semantics.
- Authentication timeout and credential-input safety.
- Honest media capability communication.
- Group privacy and keyboard behavior.
- Rate limiting and abuse control.
- Monitoring, measurable product outcomes, and support paths.
- Documentation and deployment reproducibility.
- End-to-end and concurrency test coverage.

Overall judgment: **pilot-grade, not production-grade**. It is credible for a
trusted user operating one private-chat draft at a time. It should not be treated
as safe for simultaneous drafts, group workflows, broad access, or
business-critical Jira creation until the highest-risk state and recovery issues
are resolved.

## 5. Ranked improvement opportunities

1. **Create durable, uniquely identified draft entities — high impact.**
   Store draft ID, owner, chat, status, timestamps, source messages,
   attachments, and submission attempts. Include the draft ID in every callback.
   This eliminates stale-button and cross-chat collisions.
2. **Use an explicit workflow state machine — high impact.**
   Model collecting, analyzing, review, submitting, created,
   partial-attachment, editing-published, cancelled, expired, and retryable
   failure states.
3. **Add mutation recovery and idempotency — high impact.**
   Retain drafts after failures, provide Retry/Cancel, record attempt status, and
   reconcile ambiguous timeouts before another Jira create.
4. **Correct Telegram concurrency and task lifecycle — medium impact.**
   Process stateful updates sequentially or use a keyed processor, and register
   background work through the Telegram application.
5. **Make authentication private, time-bounded, and PAT-only by default —
   medium impact.** Warn when deletion fails, add an authorized-user boundary,
   and preflight project/create permissions.
6. **Discover and validate live Jira metadata — medium-high impact.**
   Use actual project, issue-type, priority, required-field, and permission data.
   Never silently replace the user's selected issue type.
7. **Either implement actual multimodal handling or reject unsupported media —
   medium-high impact.** Ensure the AI sees what the user expects it to analyze.
8. **Repair the Telegram interaction details — low-medium impact.**
   Use consistent HTML rendering, restore/remove keyboards, direct selections,
   native copy buttons, private-chat deep links, and accurate help text.
9. **Add quotas, backpressure, and rate limiting — medium impact.**
   Protect Gemini, Jira, Telegram, and the service from one user or burst.
10. **Move durable state to a transactional store with a stronger secret
    boundary — high impact.** Add schema migration, backup/recovery, and an
    approved credential-protection design.
11. **Make published-issue updates diff-based and complete — medium-high
    impact.** Reload Jira state, show changes, preserve concurrent human edits,
    and update all supported fields consistently.
12. **Add production observability — medium impact.** Record structured,
    privacy-safe workflow IDs, latency, completion/failure outcomes, health, and
    alerts.
13. **Add handler-level integration and staged end-to-end tests — medium
    impact.** Cover stale callbacks, simultaneous drafts, auth timeout, restart,
    Jira timeout, attachment failure, and group behavior.
14. **Split `core.py` by responsibility — medium impact.** Separate intake,
    state/repository, rendering, Jira orchestration, and attachment services.
15. **Reconcile tracked operational documentation — low-medium impact.**
    Establish one current source for Jira mutation support, Ubuntu version,
    split tunneling, Gemini configuration, and actual verification status.
16. **Add admin change governance — medium impact.** Make administration
    private-only and add rule versions, diff/preview, validation, rollback, and
    optional second-person approval.
17. **Add privacy, retention, support, and feedback paths — low impact.**
    Explain Gemini/Jira data transfer and provide a recovery channel.

## Recommended continuation

Do not expand features first. The recommended first implementation batch is:

1. draft IDs and actor/chat binding;
2. explicit FSM and safe callback routing;
3. PTB-compatible sequential/keyed concurrency;
4. failure-preserving Jira submission with retry and duplicate protection;
5. handler-level tests for those invariants.

The user should approve that implementation scope before code changes begin.
