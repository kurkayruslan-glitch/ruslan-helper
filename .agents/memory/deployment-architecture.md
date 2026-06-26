---
name: deployment architecture
description: How the Telegram bot is deployed in this monorepo and why it must be Reserved VM.
---

# Deployment architecture (Ruslan Helper)

The Telegram bot (`telegram-bot/bot.py`) is NOT its own deployment. It is launched as a
**background sidecar** inside the api-server artifact's production run command
(`artifacts/api-server/.replit-artifact/artifact.toml` → `[services.production.run]`):
`cd telegram-bot && python bot.py & ... exec node ... api-server/dist/index.mjs`.
So the single repl deployment serves the pnpm web artifacts AND runs the bot.

Python deps for the bot (telebot=pytelegrambotapi, flask, openai, openpyxl, twilio, gspread,
psycopg) live in the **root `pyproject.toml`** and are installed automatically on deploy build.
Adding a Python lib the bot needs → add it to root `pyproject.toml`, then the user must REDEPLOY
for production to pick it up (dev workspace gets it immediately via package install).

**Why it must be Reserved VM, never Autoscale:**
Telegram allows only ONE getUpdates poller per bot token. Autoscale (cloudrun) spins up multiple
instances, each launching `bot.py`, so they fight for the token → endless
`Error 409 Conflict: terminated by other getUpdates request`. Reserved VM runs exactly one
always-on instance → no conflict, and bot stays up 24/7.

**How to apply:**
- Deployment type is chosen by the USER in the Publish UI (agent cannot set it; `.replit` is
  edit-restricted and holds `deploymentTarget = "cloudrun"`).
- Do NOT also run a workspace "Telegram Bot" workflow while the deployment is live — that's a
  second poller → 409. Only one instance total, ever.
- Leave the bot in default (development) 409 behavior here: it retries forever. Do NOT set
  `DEPLOYMENT_MODE=production` in this sidecar setup — prod mode makes the bot self-exit after
  ~5 consecutive 409s, but the container won't restart it (the `node` main process keeps the
  container alive), leaving the bot dead. Retry-forever is safer for a single VM instance.
