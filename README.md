# DZTGBot — Jira 8.4.1 & Gemini AI Telegram Assistant

A high-performance, asynchronous Python 3.12 Telegram bot for **Jira 8.4.1 (Self-Hosted Data Center / Server)** and **Gemini AI**. Optimized for Product Managers, Designers, QA Engineers, and Developers to triage, create, edit, and track Jira issues directly from Telegram with zero-latency (< 1 sec) actions.

---

## ⚡ Key Capabilities

### 🤖 AI-Powered Message Intake & Forwarding
- **Forward-to-Create:** Forward any Telegram message, chat history, or screenshot to the bot — Gemini AI automatically extracts summary, description, priority, and issue type into a reviewable draft.
- **PROD Ticket Auto-Linking:** Forwarding text containing `PROD-xxx` automatically routes the draft to a dev project (e.g. `BOT`) and creates an automatic `Relates to` issue link back to `PROD-xxx`.

### 🎛️ Issue Card Action Center
- **Dynamic Workflow Transitions:** Reads allowed status moves via `GET /transitions` and renders the **Primary Transition Button** (e.g. `[▶️ Start Dev]`) on Row 1 of the card.
- **Direct Reply Comments & Attachments:** Reply directly to any Telegram Issue Card with text or photos to post comments and image attachments to Jira instantly.
- **Issue Actions:** 1-tap `[➡️ Move]`, `[📝 Edit]`, `[💬 Comment]`, `[⚠️ Block]`, `[👤 Assign]`, `[👁️ Watch]`, and `[➕ Sub-task]`.
- **🎨 Figma Spec Integration:** Auto-detects `figma.com` links in issue descriptions and appends a 1-tap `[🎨 Figma Spec ↗]` direct link button.

### 🔍 Fast JQL Search & Executive Reports
- **Compact Search & Pagination:** `/my`, `/created`, `/unassigned`, `/blocked`, `/sprint`, `/s <keyword>` render 5 items per page with 1-tap card preview buttons (`[1. PROJ-123]`).
- **`/standup` Executive Digest:** Generates a daily standup summary grouped into 🔴 Blocked, 🔵 In Progress, 🟡 In QA/Review, and 🟢 Done.
- **Unread Push Notifications:** Background poller checks Jira updates every 5 minutes and pushes alerts to Telegram.

### 🔑 Flexible Enterprise Authentication
- **PAT Bearer Auth (Default):** Secure, revocable Personal Access Tokens (`Authorization: Bearer <PAT>`).
- **Optional Basic Auth Switch:** Set `AUTH_PAT_ONLY=false` in `.env` to allow `username:password` Basic Auth when PAT generation is not enabled on your Jira server.

---

## 🚀 Local Development

```bash
# 1. Clone repository & create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1

# 2. Install dependencies
python -m pip install -r requirements.txt

# 3. Environment configuration
cp .env.example .env
# Edit .env: set TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, TELEGRAM_ADMIN_USER_IDS, JIRA_URL, and WORKFLOW_DB_PATH

# 4. Run bot locally
PYTHONPATH=src python -m dztgbot
```

### Running Tests
```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

---

## 📦 Production Deployment (Ubuntu 24.04)

DZTGBot provides an automated deployment script for **Ubuntu 24.04 LTS** using systemd and a protected local SQLite database.

```bash
# 1. Run automated installer (creates system user & environment placeholder)
sudo \
  DZTGBOT_SERVICE_USER=dztgbot \
  DZTGBOT_ENV_FILE=/etc/dztgbot.env \
  bash scripts/deploy.sh

# 2. Edit production secrets safely
sudo sudoedit /etc/dztgbot.env

# 3. Re-run deploy script to start the service
sudo \
  DZTGBOT_SERVICE_USER=dztgbot \
  DZTGBOT_ENV_FILE=/etc/dztgbot.env \
  bash scripts/deploy.sh
```

### Service Control Commands
```bash
sudo systemctl status dztgbot.service
sudo systemctl restart dztgbot.service
sudo journalctl -u dztgbot.service -f
```

---

## 🛡️ Privacy & Security Boundaries
- **Private Chat Only:** All auth, workflows, callbacks, and admin commands enforce private DM operation.
- **Message Deletion:** Credential messages are automatically deleted from Telegram chat history upon reception.
- **Zero Secret Leakage:** Bot logs never contain tokens, passwords, API keys, or full message contents.
