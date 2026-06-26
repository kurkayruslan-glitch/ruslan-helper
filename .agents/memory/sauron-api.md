---
name: Sauron API format
description: Correct authentication and request format for sauron.info API — took multiple probe rounds to discover
---

## Rule
Sauron.info API requires **form-data POST** with `?token=KEY` query param (NOT Bearer header, NOT JSON body).

**Working format:**
```
POST https://sauron.info/api/v1/search?token={SAURON_API_KEY}
Content-Type: application/x-www-form-urlencoded

query=Иванов Иван
```

**Balance check (read-only, free):**
```
GET https://sauron.info/api/v1/balance?token={SAURON_API_KEY}
```

**Response shape:**
```json
{"ok": true, "result": {"uuid": "...", "balance": "646.29", "response": [{"ФИО": "...", "Телефон": "...", "День рождения": "...", "Источник": "...", ...}]}}
```

**Error shape:**
```json
{"ok": false, "error_code": 401, "description": "Access Denied: ..."}
{"ok": false, "error_code": 1002, "description": "You have not submitted a search request"}
```

**Why:** Every other format (Bearer header, X-Api-Key, JSON body, GET params like q/query/search/phone) returns 401 or 1002. Only `POST form-data query=... ?token=KEY` returns ok=true.

**How to apply:** In sauron.py, use `requests.post(url, params={"token": key}, data={"query": query})` — never `json=` argument.
