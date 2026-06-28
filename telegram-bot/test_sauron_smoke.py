"""Smoke-тест глобального поиска родственников (Sauron + VK API).

НЕ делает реальных персональных запросов и сетевых вызовов.
Проверяет: импорт модулей, наличие публичных функций, работу
вспомогательных парсеров на СИНТЕТИЧЕСКИХ (вымышленных) данных,
дедупликацию кандидатов, что status() не падает и отражает VK API.

Запуск:
    python3 -m py_compile telegram-bot/sauron.py telegram-bot/vk_api_client.py
    python3 telegram-bot/test_sauron_smoke.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sauron
import vk_api_client


def check(name: str, cond: bool) -> bool:
    print(f"{'✅' if cond else '❌'} {name}")
    return bool(cond)


def main() -> int:
    ok = True

    # 1. Публичные функции на месте (обратная совместимость)
    for fn in ("search", "get_balance", "status", "search_for_batch"):
        ok &= check(f"sauron.{fn} существует", callable(getattr(sauron, fn, None)))
    for fn in ("is_available", "get_profile", "enrich_relative", "check_token"):
        ok &= check(f"vk_api_client.{fn} существует", callable(getattr(vk_api_client, fn, None)))

    # 2. Нормализация телефонов (синтетика)
    ru = sauron._rel_norm_phone("8 912 345 67 89")
    ok &= check(f"RU телефон → 7… ({ru})", ru.startswith("7"))
    ua = sauron._rel_norm_phone("0 50 123 45 67")
    ok &= check(f"UA телефон → 380… ({ua})", ua.startswith("380"))

    # 3. Извлечение телефонов / ФИО / VK-ссылок (вымышленные данные)
    phones = sauron._rel_extract_phones("связь: +7 (912) 345-67-89 и 8 912 000 11 22")
    ok &= check(f"извлечение телефонов ({len(phones)})", len(phones) >= 1)
    fios = sauron._rel_extract_fios("Связь с лицом: Тестов Тест Тестович, мать")
    ok &= check(f"извлечение ФИО ({fios})", any("Тестов" in f for f in fios))
    vk_refs = sauron._rel_extract_vk_refs("профиль https://vk.com/id12345 и vk.com/example_user")
    ok &= check(f"извлечение VK-ссылок ({len(vk_refs)})", len(vk_refs) >= 1)

    # 4. Семейный контекст: семья распознаётся, бизнес-связь отбрасывается
    ok &= check("семейный контекст распознан", sauron._rel_is_family_context("Связь с лицом: мать"))
    ok &= check("бизнес-связь отброшена",
                not sauron._rel_is_family_context("Работодатель: ООО Ромашка"))

    # 5. Дедупликация кандидатов (один и тот же человек в разном регистре)
    cands: dict = {}
    sauron._rel_add_candidate(cands, "Тестов Тест Тестович", "ev1", "Sauron", "высокая")
    sauron._rel_add_candidate(cands, "тестов  тест  тестович", "ev2", "VK relatives", "высокая")
    ok &= check("дедуп кандидатов (1 из 2)", len(cands) == 1)

    # 6. status() не падает и упоминает VK API
    st = sauron.status()
    ok &= check("status() → непустая строка", isinstance(st, str) and len(st) > 0)
    ok &= check("status() упоминает VK API", "VK API" in st)
    print(f"   status() → {st!r}")

    # 7. is_available() возвращает bool без исключений
    avail = vk_api_client.is_available()
    ok &= check(f"vk is_available()={avail} (bool)", isinstance(avail, bool))

    # 8. Лимиты из env заданы (числа)
    ok &= check("env-лимиты — числа", all(isinstance(v, int) for v in (
        sauron._REL_MAX_PRIMARY, sauron._REL_MAX_SECONDARY,
        sauron._REL_MAX_VK, sauron._REL_MAX_OUT,
    )))

    print()
    print("✅ SMOKE OK — все проверки прошли" if ok else "❌ SMOKE FAILED — есть провалы")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
