# DZTGBot continuation checkpoint

## Current status

The bot implementation, offline tests, deployment script, systemd unit, VPN compatibility template, test plan, and cross-agent handoff workflow are present. No real credential or private VPN value has been added. No Jira connection exists.

The repository is being prepared for its first Git commit and push so it can be cloned by another account, application, or computer.

## Exact next action

After the handoff commit is pushed, verify portability from a clean clone or another AI application by issuing:

```text
DZTGBot continue
```

The receiving agent must fast-forward safely, read the durable context, report this checkpoint, and then assist with the first target-server deployment pass from the cloned checkout.

For deployment, the next concrete operation is to run `scripts/deploy.sh` on the target server with a chosen non-root service-account name and protected environment-file path. The first run creates the protected placeholder environment file and exits; credentials are entered later with `sudoedit`, never in chat or command history.

## Inputs still required from the user or target environment

- Detect or confirm whether the actual target is Ubuntu 22.04 or Oracle Linux 9.
- Choose the target checkout location, non-root service account, and protected environment-file location.
- Privately enter the real Telegram token, Gemini key, supported Gemini model, and numeric Telegram admin IDs.
- Supply approved Gemini prompts and Jira task rules; tracked versions intentionally remain placeholders.
- If VPN is required immediately, install the private L2TP/IPsec NetworkManager profile derived from the supplied XML and conduct a console-supervised full-tunnel test.

## Do not redo

- Do not redesign the src layout or replace the async Telegram/Google SDK choices without a new requirement.
- Do not switch to WireGuard.
- Do not add Jira write logic.
- Do not request credentials in chat.
- Do not re-infer VPN secrets from `src/ref/vpnsettings.xml` or expose its contents.
- Do not repeat completed Phase 0–7 scaffolding unless verification finds a concrete defect.

## Required verification on resume

1. Confirm the handoff commit is the checked-out `HEAD` and the worktree state is understood.
2. Run the offline tests before deployment changes.
3. Confirm ignored secret paths remain ignored.
4. On the target host, let the installer detect the OS; do not assume from old chat history.
5. Follow `docs/end-to-end-test-plan.md` after systemd reports the service active.

