# DZTGBot AI collaboration instructions

This repository is the durable source of truth across AI accounts, applications, and computers. Chat history, account memory, browser sessions, connector access, and credentials do not transfer.

## Required context

For project work, read these files in order:

1. `docs/context/HANDOFF.md` — concise current snapshot.
2. `docs/context/CONTINUE_HERE.md` — authoritative next action.
3. `docs/context/PROJECT_CONTEXT.md` — stable architecture and decisions.
4. `README.md` and task-relevant source files.

Verify important claims against the current working tree. When documentation and code differ, record the discrepancy before changing either.

## Command: DZTGBot handoff

Treat `DZTGBot handoff` case-insensitively as explicit authorization to prepare and Git-sync a handoff for this repository only.

1. Inspect the current request, working tree, branch, tests, and relevant files.
2. Update `PROJECT_CONTEXT.md` only for stable architecture or settled decisions.
3. Update `CONTINUE_HERE.md` with the exact current state and one concrete next action.
4. Rewrite the narrative sections of `HANDOFF.md` with:
   - current objective and success criteria;
   - completed work and changed files;
   - settled decisions and rationale;
   - open inputs, risks, blockers, and deferred scope;
   - exact next action;
   - verification performed and results.
5. Never include secrets, credential values, private VPN data, copied chat transcripts, personal machine paths, or authenticated-session data.
6. Run proportionate project tests. Record only safe summaries in the handoff.
7. Run `python scripts/handoff.py sync`. This refreshes metadata, validates the context and secret boundaries, stages all non-ignored project changes, creates a handoff commit, and pushes the current branch.
8. Never force-push, rewrite history, clean, reset, or bypass a failed validation. If synchronization is blocked, preserve all work and report the exact recovery action.

Outside this exact command, do not infer authorization to commit or push.

## Command: DZTGBot continue

Treat `DZTGBot continue` case-insensitively as authorization to fast-forward this repository from its configured Git upstream and resume the recorded work.

1. Run `python scripts/handoff.py continue` before relying on local context.
2. If local changes exist, the helper will refuse to pull. Preserve them, inspect the divergence, and ask before any merge, rebase, stash, reset, or discard operation.
3. Read the required context files in the order above.
4. Compare the handoff with the actual code and Git state.
5. Briefly state the loaded objective, last verified state, and next action.
6. Continue directly with the exact next action when it is safe and authorized. Do not repeat completed discovery.

## Non-negotiable project boundaries

- Never commit `.env`, tokens, API keys, VPN profiles, VPN XML, endpoints, credentials, private rules, or server-specific secret paths.
- Keep Telegram, Gemini, VPN, Jira rules, and service configuration environment/file based.
- Do not create Jira issues until an explicit human-approval phase is designed and authorized.
- Preserve placeholder prompts and Jira rules until the user supplies approved replacements.
- Treat `src/ref/vpnsettings.xml` as private input. Do not quote or summarize secret values.
- Keep VPN operations L2TP/IPsec through NetworkManager; WireGuard is incompatible with the supplied server setup.
- The deployment helper may support Ubuntu 22.04 and Oracle Linux 9, but the actual target must be detected or confirmed before deployment.
- Do not claim live Telegram, Gemini, VPN, systemd, or server validation unless it was actually performed in that environment.

