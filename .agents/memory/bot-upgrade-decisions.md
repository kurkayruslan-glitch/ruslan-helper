---
name: Bot night upgrade decisions
description: UX and safety patterns from the Jarvis-level upgrade of Ruslan Helper bot
---

# Bot upgrade — durable decisions

## Calls safety pattern
Before making any real phone call: collect number → show plain-text call plan → ask да/нет → only then call.
State machine key in `waiting_for_owner_call[chat_id]` uses steps: `"number"` → `"name"` → `"confirm"` → execute.
**Why:** Ruslan approved this; avoids accidental calls to wrong numbers.

## handle_voice flow
1. Check OpenAI key first — fail with clear message, no API calls.
2. `bot.send_message` returns `msg` → edit in place with transcript or error.
3. Add `chat_id` to `voice_request_chats` before `process_text` → removed in `finally`.
**Why:** edit-in-place avoids flooding chat with "расшифровываю..." + separate reply.

## TeamViewer / remote apps
`pc_apps.py` has `_REMOTE_APPS_DIRECT` dict with hardcoded Windows paths for TeamViewer, AnyDesk, Chrome Remote Desktop.
`find_remote_app_path(app)` checks direct paths first, then PATH. `is_remote_app_running(app)` uses psutil.
**Why:** registry/PATH lookup fails in remote sessions; direct paths are reliable.

## New menu structure (main_menu)
Row layout: [⚡ Jarvis, 💬 Поговорить] [🎤 Голос, 📞 Звонок] [🏨 Бронирование, 📋 Задачи] [🚕 Тоха, 💻 Мой ПК] [🎮 Dota 2, 📊 Я Тигр] [📋 ФОП]
**Why:** covers all new handler functions; matches BUTTON_LABELS routing.

## _btn_skills / _btn_skills update
Shows real-time status (key presence checks) per section. Import `get_facts` inside try/except for live count.
Google Sheets status: `from sheets import list_sheets` inside try/except.
**Why:** dynamic status > static list; user sees what actually works right now.
