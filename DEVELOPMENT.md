# Техническая документация PatchLingo

Этот файл содержит сведения для разработчиков и пользователей командной строки. Краткое описание проекта находится в [README.md](README.md).

## Установка из исходников

Проверено на Python 3.12; Windows-скрипт запуска рассчитан на Python 3.11+.

```bash
python -m pip install -r requirements.txt
python run.py
```

## Командная строка

```bash
python cli.py extract   --game /path/to/game --output /path/to/output
python cli.py translate --game /path/to/game --output /path/to/output --provider claude --target-lang Russian
python cli.py polish    --game /path/to/game --output /path/to/output --provider claude
python cli.py inject    --game /path/to/game --output /path/to/output
python cli.py restore   --game /path/to/game
python cli.py export    --output /path/to/output --csv translations.csv
python cli.py import    --output /path/to/output --csv translations.csv
python cli.py stats     --game /path/to/game --output /path/to/output
```

Полный список аргументов: `python cli.py --help` и docstring в начале `cli.py`.

### API-ключи и офлайн-режим

Ключ можно передать через переменную окружения. Приоритет настроек: `--api-key`, `RPGMTL_API_KEY`, затем переменная провайдера: `OPENAI_API_KEY`, `GEMINI_API_KEY` или `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`. Локальному серверу (LM Studio / Ollama) ключ не нужен; укажите `--base-url`.

Для офлайн-проверки полного конвейера используйте `--provider mock`. Он добавляет к тексту префикс языка, но не выполняет перевод.

Коды завершения: `0` — успех; `1` — ошибка выполнения; `2` — неверные аргументы или отсутствует ключ; `3` — часть строк не переведена; `130` — прерывание пользователем.

## Модели по умолчанию

Модели задаются в `ai_translator.DEFAULT_MODELS` и могут быть переопределены в настройках или аргументом `--model`. Список сверялся с официальной документацией провайдеров 2026-10-22.

| Провайдер | По умолчанию | Примечание |
|---|---|---|
| Claude | `claude-haiku-5-5` | Модели 5.x не принимают изменённый параметр `temperature`; проект не отправляет его. |
| Gemini | `gemini-3.6-flash` | `gemini-2.0-flash` закрыт 2026-06-01. |
| Groq | `openai/gpt-oss-20b` | Llama-модели закрыты 2026-08-16; работа gpt-oss с проектом не проверялась. |
| DeepSeek | `deepseek-flash` | Legacy-имя `deepseek-v4-flash` выведено из обращения. |
| OpenAI | `gpt-4o-mini` | Запланировано снятие 2027-01-14; преемник не выбран. |

При запуске появляется предупреждение, если выбранная модель закрыта или скоро будет закрыта.

## Защита данных

- Перед первым Inject создаётся резервная копия. Restore восстанавливает исходные файлы побайтно и сообщает о неполном восстановлении.
- Ручные и одобренные правки защищены от перезаписи ИИ, полировкой, глоссарием и автоисправлением кодов. В таблице «Строки» такие записи подсвечиваются.
- `translation_state.json` сохраняется атомарно и имеет резервную копию `.bak`; повреждённые файлы отправляются в карантин.
- QA блокирует Inject при критических ошибках, например при потерянных escape-кодах, а также если файл не удалось проверить.
- Постоянные ошибки API (например, неверный ключ или недоступная модель) прекращают перевод без бесконечных повторов.

## Проверки GitHub Actions

- **PatchLingo CI** запускается при push в `main` и рабочие ветки, а также при pull request в `main`. Он устанавливает зависимости, проверяет запуск CLI, компиляцию, офлайн-тесты и pyflakes. API-ключ не требуется.
- **DeepSeek Integration** запускается вручную из вкладки **Actions** на `main`. Тест отправляет один небольшой пакет из четырёх игровых строк модели `deepseek-flash`, проверяет перевод и специальные коды, затем делает Extract → Inject на временной RPG Maker MV-игре и сверяет JSON. Вывод ограничен 384 токенами, режим рассуждений выключен. Запрос оплачивается по тарифу DeepSeek.
- Для live-проверки: **Actions → DeepSeek Integration → Run workflow**. В репозитории нужен Secret `DEEPSEEK_API_KEY`. Workflow не запускается на pull request и не предоставляет секрет коду из форков.

## Тесты и линтер

```bash
python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest tests -q
QT_QPA_PLATFORM=offscreen python -m pytest tests -q --random-order
python -m pyflakes $(git ls-files '*.py' | grep -v '^tests/')
```

Каждый тест получает отдельную пустую домашнюю папку для настроек, логов и Global TM; тесты не меняют личные данные (`tests/conftest.py`).

## Windows-сборка

Для сборки portable-приложения в Windows с Python 3.11+ запустите `build_exe.bat`; результат появится в `dist/PatchLingo.exe`.

Готовый установщик и portable-приложение собирает GitHub Actions workflow **Windows Build**. Установщик предназначен для текущего пользователя; workflow проверяет запуск установленного приложения в Windows runner. Артефакты хранятся 14 дней.

## Известные ограничения

Подробный статус проекта и список нерешённых ограничений находятся в [PROJECT_STATUS.md](PROJECT_STATUS.md). Кратко: реальные пользовательские игры ещё не проверялись; live API-тест требует ручного запуска; GUI не проходил визуальную проверку; сборка PyInstaller не запускалась локально. «Сохранить правки» в окне и одновременный Translate могут перезаписать состояние друг друга; ручная правка JSON-файлов вне приложения не помечается как ручная.

## Лицензия

Лицензия пока не выбрана владельцем проекта.
