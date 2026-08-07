# DZTGBot agent entry point

Read and follow `AGENTS.md` before working in this repository.

- `DZTGBot handoff` saves the durable context and synchronizes safe project files to Git.
- `DZTGBot continue` fast-forwards a clean checkout, loads the durable context, and resumes the exact next action.
- In Google Antigravity, the native aliases are `/dztgbot-handoff` and `/dztgbot-continue`.

Never place credentials or private VPN configuration in tracked files.

