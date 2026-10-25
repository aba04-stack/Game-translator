# CHANGELOG — сессия «готовность» (2026-10-22)

Тесты: **444 → 771** (0 failed, 0 skipped; стабильны при `--random-order`).
Каждое исправление: воспроизведено до правки → покрыто тестом → тест проверен мутацией (возврат старого
поведения заставляет тест падать). Подробности и ограничения — `PROJECT_STATUS.md`, раздел 1.

## Исправлено

| Область | Было | Стало | Файлы |
|---|---|---|---|
| Потеря данных | оборванный `translation_state.json` молча считался пустым и затирался | карантин `*.corrupt-<время>`, восстановление из `.bak`, громкое сообщение | `engine/state.py` |
| Ручные правки (P0.1) | ИИ-перевод, полировка, глоссарий, авто-ремонт могли перезаписать правки человека | статусы строк (ручная / одобрено / заблокировано), защита во всех путях, GUI-подсветка и меню, `cli import` помечает правки | `engine/state.py`, `pipeline.py`, `rpgmtl_desktop.py`, `cli.py` |
| Восстановление оригинала | работало в потоке GUI, ошибки глотались, «✓ Восстановлено N» даже при сбоях, мусорная папка `data/data/` | `engine/restore.py`: честный учёт, байт-в-байт, фоновый поток, прогресс, отмена; команда `cli restore` | `engine/restore.py`, `rpgmtl_desktop.py`, `cli.py` |
| QA | нечитаемые файлы пропускались молча; шаблон VX Ace давал ложный critical на каждой строке с кодом | `unreadable_files`, барьер Inject fail-closed; исправлен шаблон токенов | `qa_report.py`, `pipeline.py`, `ai_translator.py` |
| Отмена Translate (P0.2) | в параллельном режиме завершённые (оплаченные) батчи выбрасывались | сохраняются | `ai_translator.py` |
| Постоянные ошибки API | 401/404: 60 запросов и 60 с на 12 строк | 1 запрос, 0,0 с, понятная причина; 429/5xx/общий 400 повторяются как раньше | `ai_translator.py`, `pipeline.py` |
| Модели | дефолты Gemini, Groq, DeepSeek, Claude закрыты провайдерами; `temperature` ломает Claude 5.5 | `DEFAULT_MODELS` (единый источник), `temperature` не шлётся тем, кто его отвергает + откат при HTTP 400, предупреждения о жизненном цикле | `ai_translator.py`, `pipeline.py`, `rpgmtl_desktop.py` |
| CLI (P0.4) | ключ только в `--api-key`, нет офлайн-режима, падение на Ctrl+C | ключ из окружения, `--provider mock`, мягкий Ctrl+C, коды возврата 0/1/2/3/130 | `cli.py`, `ai_translator.py` |
| Прогресс (P0.5) | у QA и Restore не было прогресса | единый контракт `progress(current, total, eta, filename)` на всех этапах | `qa_report.py`, `engine/restore.py` |
| Логи | строки из потоков склеивались; `stdout=None` / кодировка могли ронять | `error_utils.console_print` | `error_utils.py` |
| Тесты | две скрытые зависимости между тестами | пер-тестовая изоляция, восстановление Qt-классов | `tests/conftest.py` |
| Сборка / зависимости | устаревший `google-generativeai`; в `build_exe.bat` не ставились `rubymarshal` / `openpyxl` | `google-genai`, установка из `requirements.txt`, недостающие hidden-imports (**сборка не запускалась**) | `requirements.txt`, `build_exe.bat`, `run.py` |

## Добавлено

`README.md`, `requirements-dev.txt`, `pytest.ini`, `.gitignore`, `.github/workflows/tests.yml` (**не запускался**),
новые тесты: `test_state_safety`, `test_manual_edit_protection`, `test_gui_edit_protection`, `test_cli_readiness`,
`test_restore`, `test_cancel_safety`, `test_progress_contract`, `test_console_log`, `test_qa_false_positives`,
`test_provider_compat`, `test_fail_fast`.

## Изменённые существующие тесты (и почему)

- `test_bug_regressions.py`: три теста restore проверяли **текст исходника GUI** (регулярка по коду) или
  «эмулировали» логику внутри себя — после выноса логики в `engine/restore.py` заменены поведенческими
  тестами с тем же смыслом; тест Claude теперь использует модель, принимающую `temperature`; тест «бесплатных
  провайдеров» — актуальную модель Gemini.
- `test_qa_report.py`: пустой отчёт получил ключ `unreadable_files`.

## Не сделано

См. «Что НЕ сделано» в `PROJECT_STATUS.md`: реальные игры, живые API, Windows-сборка, гонка записи состояния,
выбор преемника `gpt-4o-mini`, лицензия.
