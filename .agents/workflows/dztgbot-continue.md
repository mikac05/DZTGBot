# DZTGBot continue

Load the latest durable DZTGBot state and resume without repeating completed work.

1. Read `AGENTS.md`.
2. Run `python scripts/handoff.py continue` before trusting the local snapshot.
3. If the helper refuses because the checkout is dirty, preserve the changes and stop before any merge, rebase, stash, reset, clean, or discard action.
4. Read `docs/context/HANDOFF.md`, `CONTINUE_HERE.md`, `PROJECT_CONTEXT.md`, and then `README.md`.
5. Verify the recorded state against the current files.
6. Briefly report the loaded objective, verification state, open blockers, and exact next action.
7. Resume that action immediately when it is safe and already authorized.

