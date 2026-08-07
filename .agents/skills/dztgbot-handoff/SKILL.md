---
name: dztgbot-handoff
description: Save and resume the DZTGBot repository across ChatGPT or Codex accounts, Google Antigravity, other AI coding applications, and computers. Use when the user says "DZTGBot handoff", "DZTGBot continue", asks to prepare a cross-account or cross-device handoff, or wants to restore the exact project state from tracked context and Git.
---

# DZTGBot handoff

Use repository files rather than conversation memory. Treat `AGENTS.md` as the command contract and safety authority.

## Select the mode

- For `DZTGBot handoff`, follow handoff mode.
- For `DZTGBot continue`, follow continue mode.
- Never run Git write operations unless handoff was explicitly requested.

## Handoff mode

1. Read `AGENTS.md` and the three files in `docs/context/`.
2. Inspect the working tree and task-relevant source. Separate observed state, user decisions, recommendations, and unknowns.
3. Refresh stable facts in `PROJECT_CONTEXT.md`, the exact checkpoint in `CONTINUE_HERE.md`, and the concise operational snapshot in `HANDOFF.md`.
4. Exclude secret values, private VPN contents, personal paths, account/session state, and full chat transcripts.
5. Run relevant verification.
6. Run `python scripts/handoff.py sync`. Do not bypass its validation or use force, reset, clean, history rewriting, or destructive recovery.
7. Report whether commit and push succeeded. If not, report the retained local state and exact blocker.

## Continue mode

1. Read `AGENTS.md`.
2. Run `python scripts/handoff.py continue`.
3. If local changes block the fast-forward, preserve them and request direction before any integration or discard operation.
4. Read `HANDOFF.md`, `CONTINUE_HERE.md`, `PROJECT_CONTEXT.md`, and `README.md` in that order.
5. Verify the handoff against the code and Git state.
6. State the objective, last verification, blockers, and exact next action concisely.
7. Continue the recorded action without restarting completed discovery.

## Safety boundary

- Handoff authorizes a normal commit and push only for this repository. It never authorizes deployment, server mutation, force-push, credential transfer, or destructive Git operations.
- Continue authorizes fetch and fast-forward-only pull on a clean checkout. It never authorizes merge, rebase, stash, reset, clean, or discard.
- Actual repository state outranks stale documentation; update the handoff when a discrepancy is found.
- Never claim external credentials, authenticated integrations, or live server state transferred through Git.

