---
name: VK API relative enrichment
description: Non-obvious rules for the official VK API kinship-evidence enrichment in telegram-bot (vk_api_client.py)
---

# VK API enrichment (kinship evidence)

Official VK API only — no scraping, captcha-bypass, or closed-page access.

## Gotchas that are NOT obvious from code

- **Names come back transliterated (Latin) by default with a service token.** A service-token `users.get` returns `Durov Pavel`, not `Дуров Павел`, which silently breaks Cyrillic surname matching against Sauron data. Fix: pass `lang=0` on every `_api_call`. Without it, every surname/city compare fails.
  **Why:** discovered when surname-match test failed even though the profile clearly matched.
- **`relatives` field gives only `{id, type}` for relatives who are VK users** (name present only for non-users). To compare names you must do a second batch `users.get` on those ids to resolve names.
- **Service tokens have no "own profile":** `users.get` with no `user_ids` returns `[]`, not an error. Validate a service token by probing a known public id (e.g. `user_ids=1`) instead of treating empty as failure.
- **Closed profiles still return basic fields** (`first_name`/`last_name`) with `is_closed=true` + `can_access_closed=false`. Do not treat as None; emit "VK закрыт/нет доступа". When several VK links exist, prefer an open profile with evidence and only fall back to the closed note if none are open.

## Confidence contract (must stay enforced)

- High VK confidence ONLY when: explicit `relatives` match (resolved name shares ≥2 tokens with main person) OR ≥2 **independent** VK signals.
- Independent signals: surname match, maiden==main surname, vk-current-surname==main (name change), DOB match, phone (public contact) match, corroborated city (city counts only WITH a name match). City alone = weak, bonus only, never a signal.
- Dependent checks must NOT increment the signal counter (e.g. "first name + DOB" depends on the already-counted DOB signal — bonus only).
- **Sauron-native high confidence is set inside `_gather_evidence` on the pre-VK score.** In the integration do NOT re-trigger high via `score >= 18` after adding the VK bonus — that lets a weak VK signal inflate confidence. Upgrade to high only via `vk_high_confidence`.
