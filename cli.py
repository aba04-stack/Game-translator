#!/usr/bin/env python3
"""
cli.py — Интерфейс командной строки для Game Translator.

Использование:
  python cli.py extract  --game /path/to/game --output /path/to/out
  python cli.py translate --game /path/to/game --output /path/to/out
                          --provider openai --target-lang Russian
  python cli.py polish    --game /path/to/game --output /path/to/out
                          --provider openai --target-lang Russian
  python cli.py inject   --game /path/to/game --output /path/to/out
  python cli.py restore  --game /path/to/game
  python cli.py stats    --game /path/to/game --output /path/to/out
  python cli.py export   --output /path/to/out --csv translations.csv
  python cli.py import   --output /path/to/out --csv translations.csv

API-ключ (translate / polish):
  Лучше всего — переменной окружения: ключ в --api-key виден в списке процессов
  и в истории оболочки. Порядок поиска: --api-key → RPGMTL_API_KEY →
  переменная провайдера (OPENAI_API_KEY, GEMINI_API_KEY/GOOGLE_API_KEY,
  ANTHROPIC_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY). Для локального сервера
  (LM Studio / Ollama, --base-url) ключ не нужен.

Параметры translate:
  --provider     openai|gemini|claude|groq|deepseek|mock
                 mock — офлайн-проверка всего конвейера без сети и без ключа
                 (текст получает префикс "[язык] ", это НЕ перевод)
  --api-key      API ключ провайдера (см. выше)
  --model        имя модели (авто если не указано)
  --target-lang  Russian|English|Chinese|Korean|... (по умолчанию: Russian)
  --source-lang  Japanese|Chinese|... (по умолчанию: авто-определение)
  --batch-size   размер батча (по умолчанию: 25)
  --max-workers  параллельных потоков (по умолчанию: 1)
  --max-retries  максимум попыток (по умолчанию: 3)
  --base-url     кастомный URL (для LM Studio / Ollama)
  --no-warp      отключить Smart Warp при inject

Параметры polish (та же AI, но НЕ переводит с нуля — улучшает уже
существующий перевод: более естественная формулировка при сохранении
смысла, тона и escape-кодов; строки, где перевода ещё нет, пропускаются):
  --provider, --api-key, --model, --target-lang, --batch-size,
  --max-retries, --request-delay, --base-url — как у translate
  --include-protected  полировать и ручные правки / одобренные строки
                       (по умолчанию они пропускаются; заблокированные — никогда)

Ручные правки и защита:
  `import` помечает изменённые строки как «ручная правка» — translate и polish
  не перезапишут их. Те же статусы (одобрить / заблокировать) доступны во
  вкладке «Строки» GUI.

Прерывание:
  Первый Ctrl+C — остановка после текущего батча с сохранением прогресса;
  второй Ctrl+C — немедленный выход.

Коды возврата:
  0 — успех; 1 — ошибка выполнения (данные, файлы, провайдер);
  2 — ошибка использования (нет API-ключа, неверные аргументы);
  3 — translate завершён, но часть строк НЕ переведена (повторите запуск:
      переведённое берётся из кэша, дорабатываются только недостающие);
  130 — прервано пользователем (Ctrl+C).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import sys
import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

from engine.file_utils import atomic_write_bytes, atomic_write_json

sys.path.insert(0, str(Path(__file__).parent))

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_INTERRUPTED = 130

PROVIDER_CHOICES = ["openai", "gemini", "claude", "groq", "deepseek", "mock"]

# Переменные окружения с ключом, по провайдерам (после общей RPGMTL_API_KEY).
_PROVIDER_ENV = {
    "openai":   ("OPENAI_API_KEY",),
    "gemini":   ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "claude":   ("ANTHROPIC_API_KEY",),
    "groq":     ("GROQ_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


class CliError(Exception):
    """Ожидаемая ошибка CLI: печатается одной строкой в stderr, без traceback."""

    def __init__(self, message: str, code: int = EXIT_FAIL):
        super().__init__(message)
        self.code = code


def _log(msg: str) -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _resolve_api_key(provider: str, explicit: str | None, base_url: str = "") -> str:
    """--api-key → RPGMTL_API_KEY → переменная провайдера.

    mock ключа не требует; локальный сервер (base_url) принимает любой ключ.
    """
    if provider == "mock":
        return "mock-no-key"
    if explicit and explicit.strip():
        return explicit.strip()
    names = ("RPGMTL_API_KEY",) + _PROVIDER_ENV.get(provider, ())
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if base_url:
        return "not-needed"
    raise CliError(
        f"не указан API-ключ для провайдера {provider!r}: передайте --api-key или "
        f"задайте переменную окружения ({', '.join(names)}). "
        "Для локального сервера (LM Studio / Ollama) укажите --base-url.",
        code=EXIT_USAGE,
    )


class _ProgressPrinter:
    """Прогресс в stderr: «живая» строка в терминале, редкие строки в логах/CI.

    В пайпе или CI \\r-перерисовка превращает лог в одну гигантскую строку, поэтому
    без TTY печатаем строку на каждые 10% и по завершении.
    """

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._last_bucket = -1
        self._finished = set()

    def update(self, done: int, total: int, eta: float = 0.0, fname: str = "") -> None:
        pct = int(done / total * 100) if total else 0
        eta_str = f"  ETA: {eta:.0f}s" if eta and eta > 0 else ""
        suffix = f"  {fname}" if fname else ""
        if self.tty:
            print(f"\r  [{pct:3d}%] {done}/{total}{eta_str}{suffix}   ",
                  end="", file=self.stream, flush=True)
            if done == total:
                print(file=self.stream, flush=True)
            return
        bucket = pct // 10
        finished_key = (done, total)
        is_end = done == total and finished_key not in self._finished
        if bucket != self._last_bucket or is_end:
            self._last_bucket = bucket
            if is_end:
                self._finished.add(finished_key)
            print(f"  [{pct:3d}%] {done}/{total}{eta_str}{suffix}", file=self.stream, flush=True)


@contextmanager
def _graceful_sigint(cancel_event: threading.Event):
    """Первый Ctrl+C → cancel_event (остановка после батча, прогресс сохраняется),
    второй → обычный KeyboardInterrupt."""
    state = {"hit": False}

    def handler(signum, frame):
        if state["hit"]:
            raise KeyboardInterrupt
        state["hit"] = True
        cancel_event.set()
        _err("\n⏹ Останавливаюсь после текущего батча (прогресс сохраняется)… "
             "Ctrl+C ещё раз — выйти немедленно.")

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:          # не главный поток — сигналы недоступны
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _exit_code(ok: bool, cancel_event: threading.Event | None = None) -> int:
    if cancel_event is not None and cancel_event.is_set():
        return EXIT_INTERRUPTED
    return EXIT_OK if ok else EXIT_FAIL


def cmd_extract(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=_log)
    ok, msg = p.extract()
    print(msg)
    return EXIT_OK if ok else EXIT_FAIL


def cmd_translate(args) -> int:
    from pipeline import Pipeline
    api_key = _resolve_api_key(args.provider, args.api_key, args.base_url)
    p = Pipeline(args.game, args.output, log_callback=_log)
    printer = _ProgressPrinter()
    cancel = threading.Event()

    # BUG FIX: Pipeline.translate() always invokes progress_callback with 4
    # positional args (cur, total, eta, fname) — see pipeline.py's p_wrap /
    # _overall_progress and every engine extractor/injector, which all share
    # this signature. This callback only accepted 3, so the very first
    # progress update (fired unconditionally before any translation happens)
    # raised "TypeError: progress() takes 3 positional arguments but 4 were
    # given" and crashed the CLI immediately — `cli.py translate` never worked.
    def progress(done: int, total: int, eta: float, fname: str = ""):
        printer.update(done, total, eta, fname)

    with _graceful_sigint(cancel):
        ok, msg = p.translate(
            provider=args.provider,
            api_key=api_key,
            model=args.model or "",
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            batch_size=args.batch_size,
            max_workers=args.max_workers,
            max_retries=args.max_retries,
            request_delay=args.request_delay,
            base_url=args.base_url or "",
            progress_callback=progress,
            cancel_event=cancel,
        )
    print(msg)
    rc = _exit_code(ok, cancel)
    failed = int(p.last_translate_summary.get("failed", 0) or 0)
    if rc == EXIT_OK and failed:
        # Пайплайн возвращает ok=True при частичном провале (GUI показывает
        # предупреждение в сообщении), но для скрипта «0» здесь было бы ложью.
        _err(f"⚠ Не переведено строк: {failed}. Код возврата {EXIT_PARTIAL}: "
             "повторите запуск — переведённое берётся из кэша.")
        return EXIT_PARTIAL
    return rc


def cmd_polish(args) -> int:
    from pipeline import Pipeline
    api_key = _resolve_api_key(args.provider, args.api_key, args.base_url)
    p = Pipeline(args.game, args.output, log_callback=_log)
    printer = _ProgressPrinter()
    cancel = threading.Event()

    def progress(done: int, total: int, eta: float, fname: str = ""):
        printer.update(done, total, eta, fname)

    with _graceful_sigint(cancel):
        ok, msg = p.polish(
            provider=args.provider,
            api_key=api_key,
            model=args.model or "",
            target_lang=args.target_lang,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            request_delay=args.request_delay,
            base_url=args.base_url or "",
            progress_callback=progress,
            cancel_event=cancel,
            include_protected=bool(getattr(args, "include_protected", False)),
        )
    print(msg)
    return _exit_code(ok, cancel)


def cmd_inject(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=_log)
    ok, msg = p.inject(apply_warp=not args.no_warp, backup=not args.no_backup)
    print(msg)
    return EXIT_OK if ok else EXIT_FAIL


def cmd_restore(args) -> int:
    """Вернуть оригинальные файлы игры из резервных копий (см. engine/restore.py)."""
    from engine.restore import restore_game_backups
    printer = _ProgressPrinter()
    cancel = threading.Event()

    def progress(done: int, total: int, eta: float, fname: str = ""):
        printer.update(done, total, eta, fname)

    with _graceful_sigint(cancel):
        result = restore_game_backups(
            Path(args.game), log_cb=_log, progress_callback=progress, cancel_event=cancel,
        )
    print(result.summary())
    if result.cancelled:
        return EXIT_INTERRUPTED
    return EXIT_OK if result.ok else EXIT_FAIL


def cmd_stats(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=lambda _: None)
    stats = p.get_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("extract_done"):
        pct = (stats.get("translated_strings", 0) / max(stats.get("unique_strings", 1), 1)) * 100
        print(f"\nПрогресс: {stats.get('translated_strings',0)}/{stats.get('unique_strings',0)} ({pct:.1f}%)")
    return EXIT_OK


def _read_json_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CliError(f"не удалось прочитать {path.name}: {e}")
    if not isinstance(data, list):
        raise CliError(f"{path.name}: ожидался JSON-массив записей")
    return data


def cmd_export(args) -> int:
    """Экспорт переводов в CSV для ручного редактирования."""

    out_dir = Path(args.output)
    extract_dir = out_dir / "extracted"
    if not extract_dir.exists():
        raise CliError(f"папка extracted не найдена в {out_dir} — сначала выполните extract")

    rows = []
    for jf in sorted(extract_dir.glob("*_translated.json")):
        data = _read_json_list(jf)
        # BUG FIX: .replace("_translated", "") replaces ALL occurrences in the stem.
        # E.g. "Map_translated_cave_translated" → "Map_cave" (wrong).
        # Correct: only strip the "_translated" suffix immediately before the extension.
        stem = jf.stem  # e.g. "Actors_translated"
        file_key = stem[: -len("_translated")] if stem.endswith("_translated") else stem
        for entry in data:
            rows.append({
                "file":       file_key,
                "path":       entry.get("path", ""),
                "context":    entry.get("context", ""),
                "original":   entry.get("original", ""),
                "translated": entry.get("translated", ""),
            })

    out_csv = Path(args.csv)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["file", "path", "context", "original", "translated"],
                            delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_bytes(out_csv, buf.getvalue().encode("utf-8-sig"))
    print(f"Экспортировано {len(rows)} строк → {out_csv}")
    return EXIT_OK


def cmd_import(args) -> int:
    """Импорт переводов из CSV обратно в *_translated.json.

    Сопоставление: (файл, path) — точно; если оригинал в CSV непустой и не
    совпадает с извлечённым, строка пропускается (игру извлекали заново после
    экспорта — пути могли сместиться, и перевод попал бы не в ту строку).
    Строки без path сопоставляются по оригиналу в порядке файла.
    Пустые переводы в CSV игнорируются (существующий перевод не стирается).

    Изменённые строки помечаются как «ручная правка» — последующие translate /
    polish их не перезапишут.
    """
    out_dir = Path(args.output)
    extract_dir = out_dir / "extracted"
    csv_path = Path(args.csv)

    if not csv_path.is_file():
        raise CliError(f"файл {csv_path} не найден")
    if not extract_dir.is_dir():
        raise CliError(f"папка extracted не найдена в {out_dir} — сначала выполните extract")

    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            header = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise CliError(f"не удалось прочитать {csv_path}: {e}")
    missing = [c for c in ("file", "path", "translated") if c not in header]
    if missing:
        raise CliError(
            f"в CSV нет обязательных колонок: {', '.join(missing)}. "
            "Ожидается формат команды export (разделитель «;», колонки "
            "file;path;context;original;translated)."
        )

    # stem → [(path, original, translated)]
    wanted: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    ignored_empty = 0
    for row in rows:
        stem = (row.get("file") or "").strip()
        trans = row.get("translated") or ""
        if not stem:
            continue
        if not trans.strip():
            ignored_empty += 1
            continue
        wanted[stem].append(((row.get("path") or ""), (row.get("original") or ""), trans))

    try:
        from ai_translator import _validate_codes
    except ImportError:          # проверка кодов — приятное дополнение, не условие импорта
        _validate_codes = None

    changed = unchanged = mismatched = broken_codes = files_changed = not_found = 0
    manual: dict[str, str] = {}
    errors: list[str] = []

    for jf in sorted(extract_dir.glob("*_translated.json")):
        # BUG FIX: same as cmd_export — use endswith guard, not str.replace
        stem = jf.stem
        stem_key = stem[: -len("_translated")] if stem.endswith("_translated") else stem
        file_rows = wanted.pop(stem_key, None)
        if not file_rows:
            continue
        try:
            data = _read_json_list(jf)
        except CliError as e:
            errors.append(str(e))
            continue

        by_path: dict[str, deque] = defaultdict(deque)
        by_orig: dict[str, deque] = defaultdict(deque)
        for path, orig, trans in file_rows:
            if path:
                by_path[path].append((orig, trans))
            else:
                by_orig[orig].append(trans)

        file_changed = False
        for entry in data:
            if not isinstance(entry, dict):
                continue
            entry_path = entry.get("path", "") or ""
            entry_orig = entry.get("original", "") or ""
            new = None
            if entry_path and by_path.get(entry_path):
                csv_orig, trans = by_path[entry_path].popleft()
                if csv_orig.strip() and csv_orig.strip() != entry_orig.strip():
                    mismatched += 1
                    continue
                new = trans
            elif not entry_path and by_orig.get(entry_orig):
                new = by_orig[entry_orig].popleft()
            if new is None:
                continue
            if entry.get("translated", "") == new:
                unchanged += 1
                continue
            entry["translated"] = new
            file_changed = True
            changed += 1
            if entry_orig.strip():
                manual[entry_orig.strip()] = new
            if _validate_codes is not None and entry_orig and not _validate_codes(entry_orig, new):
                broken_codes += 1

        # CSV-строки этого файла, которым не нашлась запись
        not_found += (sum(len(q) for q in by_path.values())
                      + sum(len(q) for q in by_orig.values()))
        if file_changed:
            atomic_write_json(jf, data)
            files_changed += 1

    # CSV-строки для файлов, которых нет в extracted/ вообще
    not_found += sum(len(rows_left) for rows_left in wanted.values())

    state_note = ""
    if manual:
        try:
            from engine.state import record_edits
            record_edits(out_dir, manual=manual)
        except Exception as e:  # noqa: BLE001
            state_note = (f"  ⚠ переводы записаны, но пометки «ручная правка» сохранить не удалось ({e}): "
                          "AI-полировка может перезаписать эти строки.")

    print(f"Импорт: обновлено строк: {changed} (файлов: {files_changed}), без изменений: {unchanged}")
    if ignored_empty:
        print(f"  пустых переводов в CSV пропущено: {ignored_empty}")
    if mismatched:
        print(f"  ⚠ пропущено — оригинал в CSV не совпал с извлечённым: {mismatched} "
              "(игру извлекали заново после экспорта?)")
    if not_found:
        print(f"  ⚠ в extracted/ не найдено записей для строк CSV: {not_found}")
    if broken_codes:
        print(f"  ⚠ нарушены escape-коды в {broken_codes} строках — проверьте перед inject")
    if state_note:
        print(state_note)
    for e in errors:
        _err(f"Ошибка: {e}")

    nothing_applied = changed == 0 and unchanged == 0
    if errors or (nothing_applied and (mismatched or not_found)):
        return EXIT_FAIL
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rpgmtl",
        description="Game Translator — ИИ-переводчик игр",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # extract
    p_ext = sub.add_parser("extract", help="Извлечь тексты из игры")
    p_ext.add_argument("--game",   required=True, help="Папка с игрой")
    p_ext.add_argument("--output", required=True, help="Папка вывода")

    # translate
    p_tr = sub.add_parser("translate", help="Перевести через AI")
    p_tr.add_argument("--game",        required=True)
    p_tr.add_argument("--output",      required=True)
    p_tr.add_argument("--provider",    required=True, choices=PROVIDER_CHOICES)
    p_tr.add_argument("--api-key",     default=None, dest="api_key",
                      help="API ключ (лучше — переменной окружения, см. --help модуля)")
    p_tr.add_argument("--model",       default="")
    p_tr.add_argument("--target-lang", default="Russian", dest="target_lang")
    p_tr.add_argument("--source-lang", default="auto", dest="source_lang")
    p_tr.add_argument("--batch-size",  type=int, default=25, dest="batch_size")
    p_tr.add_argument("--max-workers", type=int, default=1, dest="max_workers")
    p_tr.add_argument("--max-retries", type=int, default=3, dest="max_retries")
    p_tr.add_argument("--request-delay", type=float, default=0.0, dest="request_delay",
                      help="Задержка между батчами в секундах (default: 0)")
    # BUG FIX: referenced as args.base_url in cmd_translate() (and documented
    # in the module docstring above) but never actually registered here —
    # every invocation of `cli.py translate` raised
    # "AttributeError: 'Namespace' object has no attribute 'base_url'".
    p_tr.add_argument("--base-url", default="", dest="base_url",
                      help="Кастомный URL (для LM Studio / Ollama)")

    # polish
    p_pol = sub.add_parser("polish", help="AI-полировка уже переведённых строк")
    p_pol.add_argument("--game",        required=True)
    p_pol.add_argument("--output",      required=True)
    p_pol.add_argument("--provider",    required=True, choices=PROVIDER_CHOICES)
    p_pol.add_argument("--api-key",     default=None, dest="api_key",
                       help="API ключ (лучше — переменной окружения)")
    p_pol.add_argument("--model",       default="")
    p_pol.add_argument("--target-lang", default="Russian", dest="target_lang")
    p_pol.add_argument("--batch-size",  type=int, default=10, dest="batch_size")
    p_pol.add_argument("--max-retries", type=int, default=3, dest="max_retries")
    p_pol.add_argument("--request-delay", type=float, default=0.0, dest="request_delay",
                       help="Задержка между батчами в секундах (default: 0)")
    p_pol.add_argument("--base-url", default="", dest="base_url",
                       help="Кастомный URL (для LM Studio / Ollama)")
    p_pol.add_argument("--include-protected", action="store_true", dest="include_protected",
                       help="Полировать и ручные правки / одобренные строки "
                            "(заблокированные не трогаются никогда)")

    # inject
    p_inj = sub.add_parser("inject", help="Вставить переводы в игру")
    p_inj.add_argument("--game",       required=True)
    p_inj.add_argument("--output",     required=True)
    p_inj.add_argument("--no-warp",    action="store_true", dest="no_warp")
    p_inj.add_argument("--no-backup",  action="store_true", dest="no_backup")

    # restore
    p_rs = sub.add_parser("restore", help="Вернуть оригинальные файлы игры из резервной копии")
    p_rs.add_argument("--game", required=True, help="Папка с игрой")

    # stats
    p_st = sub.add_parser("stats", help="Показать статистику перевода")
    p_st.add_argument("--game",   required=True)
    p_st.add_argument("--output", required=True)

    # export
    p_ex = sub.add_parser("export", help="Экспортировать переводы в CSV")
    p_ex.add_argument("--output", required=True)
    p_ex.add_argument("--csv",    required=True)

    # import
    p_im = sub.add_parser("import", help="Импортировать переводы из CSV (помечаются как ручные правки)")
    p_im.add_argument("--output", required=True)
    p_im.add_argument("--csv",    required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    dispatch = {
        "extract":   cmd_extract,
        "translate": cmd_translate,
        "polish":    cmd_polish,
        "inject":    cmd_inject,
        "restore":   cmd_restore,
        "stats":     cmd_stats,
        "export":    cmd_export,
        "import":    cmd_import,
    }
    try:
        return dispatch[args.command](args)
    except CliError as e:
        _err(f"Ошибка: {e}")
        return e.code
    except KeyboardInterrupt:
        _err("\nПрервано пользователем.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
