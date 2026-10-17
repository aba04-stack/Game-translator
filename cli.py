#!/usr/bin/env python3
"""
cli.py — Интерфейс командной строки для RPGMTL.

Использование:
  python cli.py extract  --game /path/to/game --output /path/to/out
  python cli.py translate --game /path/to/game --output /path/to/out
                          --provider openai --api-key sk-... --target-lang Russian
  python cli.py polish    --game /path/to/game --output /path/to/out
                          --provider openai --api-key sk-... --target-lang Russian
  python cli.py inject   --game /path/to/game --output /path/to/out
  python cli.py stats    --game /path/to/game --output /path/to/out
  python cli.py export   --output /path/to/out --csv translations.csv

Параметры translate:
  --provider     openai|gemini|claude|groq|deepseek
  --api-key      API ключ провайдера
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
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from engine.file_utils import atomic_write_bytes, atomic_write_json

sys.path.insert(0, str(Path(__file__).parent))


def _log(msg: str) -> None:
    print(msg, flush=True)


def cmd_extract(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=_log)
    ok, msg = p.extract()
    print(msg)
    return 0 if ok else 1


def cmd_translate(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=_log)

    # BUG FIX: Pipeline.translate() always invokes progress_callback with 4
    # positional args (cur, total, eta, fname) — see pipeline.py's p_wrap /
    # _overall_progress and every engine extractor/injector, which all share
    # this signature. This callback only accepted 3, so the very first
    # progress update (fired unconditionally before any translation happens)
    # raised "TypeError: progress() takes 3 positional arguments but 4 were
    # given" and crashed the CLI immediately — `cli.py translate` never worked.
    def progress(done: int, total: int, eta: float, fname: str = ""):
        pct = int(done / total * 100) if total else 0
        eta_str = f"  ETA: {eta:.0f}s" if eta > 0 else ""
        suffix = f"  {fname}" if fname else ""
        print(f"\r  [{pct:3d}%] {done}/{total}{eta_str}{suffix}   ", end="", flush=True)
        if done == total:
            print()

    ok, msg = p.translate(
        provider=args.provider,
        api_key=args.api_key,
        model=args.model or "",
        source_lang=args.source_lang,
        target_lang=args.target_lang,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
        max_retries=args.max_retries,
        request_delay=args.request_delay,
        base_url=args.base_url or "",
        progress_callback=progress,
    )
    print(msg)
    return 0 if ok else 1


def cmd_polish(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=_log)

    def progress(done: int, total: int, eta: float, fname: str = ""):
        pct = int(done / total * 100) if total else 0
        print(f"\r  [{pct:3d}%] {done}/{total}   ", end="", flush=True)
        if done == total:
            print()

    ok, msg = p.polish(
        provider=args.provider,
        api_key=args.api_key,
        model=args.model or "",
        target_lang=args.target_lang,
        batch_size=args.batch_size,
        max_retries=args.max_retries,
        request_delay=args.request_delay,
        base_url=args.base_url or "",
        progress_callback=progress,
    )
    print(msg)
    return 0 if ok else 1


def cmd_inject(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=_log)
    ok, msg = p.inject(apply_warp=not args.no_warp, backup=not args.no_backup)
    print(msg)
    return 0 if ok else 1


def cmd_stats(args) -> int:
    from pipeline import Pipeline
    p = Pipeline(args.game, args.output, log_callback=lambda _: None)
    stats = p.get_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("extract_done"):
        pct = (stats.get("translated_strings", 0) / max(stats.get("unique_strings", 1), 1)) * 100
        print(f"\nПрогресс: {stats.get('translated_strings',0)}/{stats.get('unique_strings',0)} ({pct:.1f}%)")
    return 0


def cmd_export(args) -> int:
    """Экспорт переводов в CSV для ручного редактирования."""

    out_dir = Path(args.output)
    extract_dir = out_dir / "extracted"
    if not extract_dir.exists():
        print(f"Ошибка: папка extracted не найдена в {out_dir}")
        return 1

    rows = []
    for jf in sorted(extract_dir.glob("*_translated.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
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
    writer = csv.DictWriter(buf, fieldnames=["file","path","context","original","translated"],
                            delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_bytes(out_csv, buf.getvalue().encode("utf-8-sig"))
    print(f"Экспортировано {len(rows)} строк → {out_csv}")
    return 0


def cmd_import(args) -> int:
    """Импорт переводов из CSV обратно в _translated.json."""

    out_dir = Path(args.output)
    extract_dir = out_dir / "extracted"
    csv_path = Path(args.csv)

    if not csv_path.exists():
        print(f"Ошибка: файл {csv_path} не найден")
        return 1

    # Load CSV
    updates: dict[str, dict[str, str]] = {}  # {file_stem: {path: translated}}
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            stem = row.get("file", "")
            path = row.get("path", "")
            trans = row.get("translated", "")
            if stem and path and trans:
                updates.setdefault(stem, {})[path] = trans

    modified = 0
    for jf in extract_dir.glob("*_translated.json"):
        # BUG FIX: same as cmd_export — use endswith guard, not str.replace
        stem = jf.stem
        stem_key = stem[: -len("_translated")] if stem.endswith("_translated") else stem
        if stem_key not in updates:
            continue
        data = json.loads(jf.read_text(encoding="utf-8"))
        changed = False
        for entry in data:
            p = entry.get("path", "")
            if p in updates[stem_key]:
                entry["translated"] = updates[stem_key][p]
                changed = True
        if changed:
            atomic_write_json(jf, data)
            modified += 1

    print(f"Обновлено файлов: {modified}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="rpgmtl",
        description="RPGMTL — AI-переводчик игр",
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
    p_tr.add_argument("--provider",    required=True,
                      choices=["openai","gemini","claude","groq","deepseek"])
    p_tr.add_argument("--api-key",     required=True, dest="api_key")
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
    p_pol.add_argument("--provider",    required=True,
                       choices=["openai","gemini","claude","groq","deepseek"])
    p_pol.add_argument("--api-key",     required=True, dest="api_key")
    p_pol.add_argument("--model",       default="")
    p_pol.add_argument("--target-lang", default="Russian", dest="target_lang")
    p_pol.add_argument("--batch-size",  type=int, default=10, dest="batch_size")
    p_pol.add_argument("--max-retries", type=int, default=3, dest="max_retries")
    p_pol.add_argument("--request-delay", type=float, default=0.0, dest="request_delay",
                       help="Задержка между батчами в секундах (default: 0)")
    p_pol.add_argument("--base-url", default="", dest="base_url",
                       help="Кастомный URL (для LM Studio / Ollama)")

    # inject
    p_inj = sub.add_parser("inject", help="Вставить переводы в игру")
    p_inj.add_argument("--game",       required=True)
    p_inj.add_argument("--output",     required=True)
    p_inj.add_argument("--no-warp",    action="store_true", dest="no_warp")
    p_inj.add_argument("--no-backup",  action="store_true", dest="no_backup")

    # stats
    p_st = sub.add_parser("stats", help="Показать статистику перевода")
    p_st.add_argument("--game",   required=True)
    p_st.add_argument("--output", required=True)

    # export
    p_ex = sub.add_parser("export", help="Экспортировать переводы в CSV")
    p_ex.add_argument("--output", required=True)
    p_ex.add_argument("--csv",    required=True)

    # import
    p_im = sub.add_parser("import", help="Импортировать переводы из CSV")
    p_im.add_argument("--output", required=True)
    p_im.add_argument("--csv",    required=True)

    args = parser.parse_args()

    dispatch = {
        "extract":   cmd_extract,
        "translate": cmd_translate,
        "polish":    cmd_polish,
        "inject":    cmd_inject,
        "stats":     cmd_stats,
        "export":    cmd_export,
        "import":    cmd_import,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
