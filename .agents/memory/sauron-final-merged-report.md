---
name: Sauron FINAL_MERGED.xlsx report
description: Conventions and gotchas for the single-Excel relative report produced by Sauron file search
---

# FINAL_MERGED.xlsx (Sauron file search)

Single Excel file, one sheet `Sheet1`, **one row = one relative of a found ("погибший") person**. No ZIP / no per-CSV as the main result.

Columns (exact order): ФИО погибшего, Дата рождения погибшего, Дата смерти погибшего, ФИО родственника, Дата рождения родственника, Телефон, phone_norm, СНИЛС, MAX, Соц. сети, Ошибка.

## Durable decisions
- **Strict "1 row = 1 relative".** Do NOT add extra rows for not-found / errored people without relatives. The "Ошибка" column is per-relative-row and normally empty.
  **Why:** user spec is exact; architect flagged error-rows as a violation of the row contract.
- **Map relative → person by `RelativeRecord.source_row` (the input row_num), not by source_fio.**
  **Why:** duplicate full names with different DOB/DOD in the input would otherwise all attach to the first homonym. Keep a source_fio fallback only for legacy records where source_row==0.
- **MAX flag is relative-level, not per-phone.** `in_maxim` applies to the whole relative; there is no specific "Max number". Render `"Да - <first phone>"`, or `"Да"` if no phones, else `"Нет"`. Do NOT build a maxim-by-owner index keyed on owner_fio — that reintroduces the homonym bug.
- Phone display: `"79140367459 (МТС); 79512941691 (Tele2)"`; phone_norm: `"79140367459; 79512941691"`. Operator from DEF-code tables (best-effort, no MNP).
- СНИЛС / Телефон / phone_norm / MAX cells stored as text (`number_format='@'`) so Excel doesn't mangle them.
- Social links: one per line (`\n`).
