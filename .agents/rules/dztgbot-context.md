# DZTGBot durable context

Read and obey the repository-root `AGENTS.md`.

The project never relies on conversation history for continuity. The tracked files under `docs/context/` contain the durable state.

- Natural command `DZTGBot handoff`: update the context and run the safe Git synchronization workflow.
- Natural command `DZTGBot continue`: fast-forward a clean checkout, read the context, and resume its exact next action.
- Native Antigravity aliases: `/dztgbot-handoff` and `/dztgbot-continue`.

Never write credentials, private VPN configuration, authenticated-session data, or server-specific secret values to tracked files.

