---
name: deployment architecture
description: How the Telegram bot is deployed (Railway via GitHub) and the one-poller rule.
---

# Deployment architecture (Ruslan Helper)

The Telegram bot (`telegram-bot/bot.py`) is hosted 24/7 on **Railway**, deployed from the
GitHub repo `github.com/kurkayruslan-glitch/ruslan-helper` (remote `github`, branch `main`).
Pushing to `github/main` triggers an automatic Railway redeploy. There is **no Replit
deployment** for the bot (`getDeploymentInfo()` returns `isDeployed: false`).

**Why a workspace 409 is actually a good sign:** if the workspace "Telegram Bot" workflow
shows `Error 409 Conflict: terminated by other getUpdates request`, it means the Railway
instance is alive and holds the token. Telegram allows only ONE getUpdates poller per token.

**One-poller rule (critical):**
- Never run the workspace "Telegram Bot" workflow at the same time as Railway — they fight for
  the token and the bot becomes flaky. When Railway is the 24/7 host, the workspace workflow
  should be removed/stopped. The dev run command is documented in `replit.md`.
- If you must run/test the bot in the workspace, pause Railway first.

**Why never Autoscale (if ever moved to Replit deployment):** Autoscale spins up multiple
instances, each polling → endless 409. Use a single always-on instance (Railway, or Replit
Reserved VM) only.

**Getting new code to production:**
- `git commit` is a restricted destructive op for the agent — it cannot commit/push directly.
  The Replit checkpoint auto-commits locally at task end; the USER pushes to `github/main`
  via the Replit Git pane → Railway redeploys. New code is NOT live until that push happens.

**How to apply:**
- After editing bot code, verify with `python3 -m py_compile telegram-bot/*.py` and
  `python3 telegram-bot/test_sauron_smoke.py` (no-network smoke test). Live bot can't be
  tested in workspace while Railway holds the token.
