# DZTGBot handoff

## Current objective

Make the production-oriented Telegram bot and its complete project state portable across ChatGPT/Codex accounts, Google Antigravity, other AI coding applications, and computers without relying on chat memory or transferring secrets.

## Completed

- Implemented the Python 3.12 async forward-only Telegram bot core.
- Added strict async Gemini-to-Jira-template analysis and human-readable previews.
- Added runtime Jira rules with admin-only hot updates and last-known-good fallback.
- Added NetworkManager L2TP/IPsec status/start support matching the supplied profile constraints.
- Added a hardened systemd service and a rerunnable Ubuntu 22.04-only deployment script.
- Removed every alternate-distribution installer branch, package path, flag, and documentation reference; Ubuntu 22.04 is now enforced throughout the project.
- Added offline tests, end-to-end server test guidance, secret exclusions, and placeholder-only configuration.
- Added repository-native cross-agent instructions, context documents, Codex skill metadata, Antigravity rules/workflows, and safe Git synchronization tooling.

## Decisions

- Git-tracked files are the only transferable memory. Account memory and authenticated sessions are never assumed.
- `DZTGBot handoff` is the only phrase that authorizes the workflow to commit and push this repository.
- `DZTGBot continue` may fetch and fast-forward only when the checkout is clean.
- The sync helper refuses known secret files/patterns, non-fast-forward situations, and destructive recovery.
- The native Antigravity equivalents are `/dztgbot-handoff` and `/dztgbot-continue`.
- Ubuntu 22.04 is the only supported deployment target; all other platforms are explicitly out of scope.

## Open items

- The initial handoff is committed locally, but the currently authenticated Git identity does not have write permission to the configured `origin`; the remote was not changed.
- Real Telegram/Gemini credentials and admin IDs have not been supplied and must remain outside Git.
- Gemini prompts and Jira rules are intentionally placeholders pending approval.
- The private VPN profile still must be installed and tested on the target host if VPN access is required.
- No live target deployment, Telegram exchange, Gemini request, VPN connection, or systemd run has been verified yet.

## Exact next action

Grant the current Git identity write permission to the configured origin, or explicitly authorize changing `origin` to a repository it can write. Then run `DZTGBot handoff` again to push, and test `DZTGBot continue` from another account/application or clean clone. After continuity is verified, perform the first target-server installer pass described in `CONTINUE_HERE.md`.

## Verification

- Combined bot and handoff suite: 23 offline tests passed.
- Python compilation and dependency checks passed.
- Bash deployment-script syntax and secret-ignore audits passed.
- The project-local handoff skill passed its structural validator.
- Handoff context, ignore policy, and credential-pattern validation passed.
- A dirty-checkout `continue` test refused to pull and preserved the working tree unchanged.
- The initial commit succeeded locally; the push was safely retained as a blocker after GitHub denied write permission.
- An unsupported-platform reference audit passed; tracked project content now describes Ubuntu 22.04 only.

## Transfer limitations

Git does not transfer `.env`, private VPN files, tokens, keys, server access, Telegram/Gemini sessions, browser authentication, connector permissions, or AI-account memory. Configure those separately and privately on each authorized environment.

## Git snapshot metadata

<!-- HANDOFF-METADATA:START -->
- Generated UTC: `2026-08-07T02:11:37Z`
- Branch: `main`
- Upstream: `origin/main`
- Base commit before this handoff: `226d8143b83f`
- Working-tree entries before metadata refresh: `7`
- The handoff commit is the commit containing this file.
<!-- HANDOFF-METADATA:END -->
