# DZTGBot handoff

Prepare a complete, secret-safe handoff and synchronize it to Git so another AI account, application, or computer can continue.

1. Read `AGENTS.md` and follow its `DZTGBot handoff` procedure exactly.
2. Inspect the actual working tree and current task; do not reconstruct state from chat alone.
3. Refresh `docs/context/PROJECT_CONTEXT.md`, `CONTINUE_HERE.md`, and `HANDOFF.md` at their documented levels of detail.
4. Run the relevant offline tests and record safe result summaries.
5. Run `python scripts/handoff.py sync`.
6. If validation or Git synchronization fails, do not force, reset, clean, or discard anything. Report the blocker and exact safe recovery step.
7. Finish by reporting the pushed branch and commit, or clearly state that the handoff remains local.

