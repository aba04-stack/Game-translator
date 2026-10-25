"""
renpy_extractor.py — извлечение диалогов из Ren'Py игр.
Парсит .rpy файлы: реплики персонажей, нарратор, меню-выборы.
"""

import hashlib
from engine.file_utils import atomic_write_json
import json
import re
from pathlib import Path
from typing import Callable

# ─── Паттерны ────────────────────────────────────────────────────────────────
_DQ = r'"(?:[^"\\]|\\.)*"'   # строка в двойных кавычках
_SQ = r"'(?:[^'\\]|\\.)*'"  # строка в одинарных кавычках
_ANY_STR = f"(?:{_DQ}|{_SQ})"

# say: [char_var] "text"  или  "text"  (нарратор)
_SAY_RE = re.compile(
    r"^(\s*)(?:(\w+)\s+)?(" + _ANY_STR + r")\s*(?:#.*)?$"
)

# вариант меню: "text":  или  "text" if condition:
_CHOICE_RE = re.compile(
    r"^(\s*)(" + _ANY_STR + r")(?:\s+if\s+[^:]+)?\s*:\s*(?:#.*)?$"
)

# menu: или menu(something):
_MENU_RE = re.compile(r"^\s*menu\s*(?:\([^)]*\))?\s*:\s*(?:#.*)?$")

# define char = Character("Имя", ...)
_DEFINE_CHAR_RE = re.compile(
    r"^\s*define\s+(\w+)\s*=\s*(?:\w+\.)?Character\s*\(\s*(" + _ANY_STR + r")"
)

# Ключевые слова Ren'Py — никогда не реплики
_RNP_KEYWORDS = frozenset({
    "scene", "show", "hide", "play", "stop", "queue", "pause",
    "jump", "call", "return", "pass", "with", "at", "as", "onlayer",
    "label", "menu", "screen", "style", "init", "python", "transform",
    "image", "define", "default", "translate", "voice",
    "window", "frame", "vbox", "hbox", "grid", "textbutton", "button",
    "if", "elif", "else", "for", "while", "from", "import", "in",
    "layeredimage", "renpy", "use", "add", "text", "bar", "null",
    "timer", "key", "mousearea", "on", "event", "contains", "zorder",
})

# Специальные операторы с текстом (не персонажи, но переводятся)
_RNP_SPECIAL_SAY = frozenset({"extend", "nvl"})


# ─── Вспомогательные функции ─────────────────────────────────────────────────

# ─── Декомпиляция .rpyc ──────────────────────────────────────────────────────

def _check_rpyc_situation(search_dir: Path) -> tuple[bool, bool]:
    """
    Вернуть (has_rpy, has_rpyc) для папки.
    """
    has_rpy  = any(search_dir.rglob("*.rpy"))
    has_rpyc = any(search_dir.rglob("*.rpyc"))
    return has_rpy, has_rpyc


def _decompile_rpyc_files(
    search_dir: Path,
    log_cb=None,
    progress_cb=None,
    output_dir: Path | None = None,
) -> int:
    """
    Декомпилировать все .rpyc в .rpy используя unrpyc.
    Возвращает количество успешно декомпилированных файлов.

    BUG FIX (внешний аудит): декомпиляция пишет rpyc_path.with_suffix(".rpy")
    — то есть НОВЫЕ .rpy файлы создаются ПРЯМО В ПАПКЕ ИГРЫ (search_dir —
    это живой game_dir, не staging-копия). Extract тем самым уже изменяет
    исходную установку игры ещё до Inject, причём это не редкий случай — это
    ТИПИЧНАЯ ситуация: игры почти всегда распространяются только с .rpyc,
    без .rpy. Полный редирект вывода в output_dir потребовал бы переделки
    сопоставления путей во всём извлечении/инъекции Ren'Py (риск сломать
    больше, чем починить, за один патч) — вместо этого: (1) явно
    предупреждаем перед стартом, что в папку игры будут добавлены файлы,
    (2) если существующий .rpy уже есть — как и раньше, не трогаем и не
    перезаписываем, (3) список реально СОЗДАННЫХ файлов сохраняется в
    output_dir/decompiled_rpy_manifest.json, чтобы пользователь мог узнать
    и при желании удалить их из папки игры.
    """
    # BUG FIX: exclude _backup_* from rpyc scan
    rpyc_files = sorted(
        p for p in search_dir.rglob("*.rpyc")
        if not any(part.startswith("_backup") for part in p.parts)
    )
    if not rpyc_files:
        return 0

    _log(f"[RenPy] Найдено .rpyc файлов: {len(rpyc_files)}", log_cb)

    # Проверяем наличие unrpyc
    try:
        import unrpyc
        has_unrpyc = True
    except ImportError:
        has_unrpyc = False

    if not has_unrpyc:
        _log(
            "[RenPy] ✗ ВАЖНО: В игре найдены только скомпилированные файлы (.rpyc).\n"
            "  Для перевода Ren'Py требуются исходные файлы (.rpy).\n"
            "  Пожалуйста, декомпилируйте их самостоятельно перед переводом:\n"
            "  1. Скачайте unrpyc: https://github.com/CensoredUsername/unrpyc\n"
            "  2. Перетащите файлы игры на unrpyc, чтобы получить .rpy файлы.\n"
            "  3. После этого повторите Extract.",
            log_cb,
        )
        return 0

    to_create = [p for p in rpyc_files if not p.with_suffix(".rpy").exists()]
    if to_create:
        _log(
            f"[RenPy] ⚠ Декомпиляция создаст {len(to_create)} новых .rpy "
            "файлов ПРЯМО В ПАПКЕ ИГРЫ (это единственный способ получить "
            "исходники из .rpyc — Ren'Py сам ищет .rpy рядом с .rpyc). "
            "Уже существующие .rpy не трогаются. Список созданных файлов "
            "будет сохранён для справки.",
            log_cb,
        )

    decompiled = 0
    created_files: list[str] = []
    for i, rpyc_path in enumerate(rpyc_files):
        rpy_path = rpyc_path.with_suffix(".rpy")
        if rpy_path.exists():
            continue  # уже есть .rpy — пропускаем
        try:
            import threading

            result: list = []
            error: list = []

            def _run():
                try:
                    ctx = unrpyc.Context()
                    unrpyc.decompile_rpyc(rpyc_path, ctx)
                    result.append(ctx)
                except Exception as exc:
                    error.append(exc)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            # BUG FIX: decompile на сломанном .rpyc может зависнуть навсегда.
            # Даём 30 секунд на файл — больше чем достаточно для нормального .rpyc.
            t.join(timeout=30)
            if t.is_alive():
                _log(f"  ✗ {rpyc_path.name}: таймаут (файл возможно повреждён)", log_cb)
                continue
            if error:
                _log(f"  ✗ {rpyc_path.name}: {error[0]}", log_cb)
                continue
            if result:
                ctx = result[0]
                if getattr(ctx, "state", None) == "ok" and rpy_path.exists():
                    decompiled += 1
                    try:
                        created_files.append(rpy_path.relative_to(search_dir).as_posix())
                    except ValueError:
                        created_files.append(str(rpy_path))
                    _log(f"  ✓ {rpyc_path.name} → {rpy_path.name}", log_cb)
                else:
                    err_msg = getattr(ctx, "error", "") or "неизвестная ошибка"
                    _log(f"  ✗ {rpyc_path.name}: {err_msg}", log_cb)
        except ImportError:
            _log(f"  ✗ unrpyc не установлен, пропуск {rpyc_path.name}", log_cb)
            break
        except Exception as e:
            _log(f"  ✗ {rpyc_path.name}: {e}", log_cb)
        if progress_cb:
            progress_cb(i + 1, len(rpyc_files), -1.0, "")

    if created_files and output_dir is not None:
        try:
            manifest_path = Path(output_dir) / "decompiled_rpy_manifest.json"
            existing: list[str] = []
            if manifest_path.exists():
                try:
                    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = []
            merged = sorted(set(existing) | set(created_files))
            atomic_write_json(manifest_path, merged, indent=2)
            _log(
                f"[RenPy] Список новых .rpy файлов в папке игры сохранён: "
                f"{manifest_path.name} ({len(created_files)} новых в этом Extract, "
                f"{len(merged)} всего)",
                log_cb,
            )
        except Exception as e:
            _log(f"[RenPy] ⚠ Не удалось сохранить список декомпилированных файлов: {e}", log_cb)

    _log(f"[RenPy] Декомпилировано: {decompiled}/{len(rpyc_files)}", log_cb)
    return decompiled

def _unquote(s: str) -> str:
    """Снять кавычки и раскодировать базовые escape-последовательности.

    BUG FIX: предыдущая версия обрабатывала \\n раньше \\\\,
    из-за чего строка \\\\n (literal backslash+n в файле)
    неверно превращалась в backslash + newline.
    Правильный порядок: сначала \\\\ через плейсхолдер.
    """
    if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
        inner = s[1:-1]
        # BUG FIX: protect \\\\ FIRST before processing \\n
        inner = inner.replace("\\\\", "\x00RPGMTL_BACKSLASH\x00")
        inner = inner.replace('\\"', '"').replace("\\\'" , "'")
        inner = inner.replace("\\n", "\n").replace("\\t", "\t")
        inner = inner.replace("\x00RPGMTL_BACKSLASH\x00", "\\")
        return inner
    return s

def _log(msg: str, cb: Callable[[str], None] | None = None) -> None:
    print(msg, flush=True)
    if cb:
        cb(msg)


def _should_translate(text: str) -> bool:
    """Нужно ли переводить эту строку Ren'Py."""
    t = text.strip()
    if not t:
        return False

    # Только цифры/пунктуация — нет.
    # Односимвольные реплики вроде "え", "…", "?" не отбрасываем заранее.
    if len(t) <= 1 and not re.search(r"[a-zA-Zа-яёА-ЯЁ぀-ヿ一-鿿가-힯]", t):
        return False
    if re.fullmatch(r"[\d\s\.\,\!\?\:\;\-\_\(\)]+", t):
        return False

    # BUG FIX: та же защита, что добавлена в engine/text_filter.py после
    # реального краша игры (RPG Maker MZ Plugin Command). Ren'Py-теги
    # изображений (image имя_варианта = ...) и другие идентификаторы с
    # подчёркиванием ссылаются друг на друга по имени в разных местах
    # скрипта; перевод одного вхождения и не перевод другого рассинхронит
    # ссылку. Обычный диалог практически никогда не содержит "_".
    if "_" in t:
        return False

    # Пути к файлам — нет
    if re.search(r"\.\w{2,4}$", t) and ("/" in t or "\\" in t):
        return False

    # Переменные в квадратных скобках — убрать перед проверкой смысла
    clean = re.sub(r"\[.*?\]", "", t).strip()
    if not clean:
        return False

    # Достаточно наличия букв/иероглифов — даже если строка короткая
    if re.search(r"[a-zA-Zа-яёА-ЯЁ぀-ヿ一-鿿가-힯]", clean):
        return True
    return False



# ─── Парсер ──────────────────────────────────────────────────────────────────

def _parse_rpy_file(
    rpy_path: Path,
    fname: str,
    char_names: dict[str, str],
) -> list[dict]:
    """Распарсить один .rpy файл и вернуть список записей."""
    text = rpy_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    entries: list[dict] = []

    # Стек состояний для вложенных блоков
    in_python = False
    python_indent = -1
    in_translate = False
    translate_indent = -1
    in_menu = False
    menu_indent = -1

    for lineno, raw_line in enumerate(lines):
        stripped = raw_line.rstrip()
        if not stripped:
            continue

        content = stripped.lstrip()
        if content.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())

        # ── Выход из python/translate блоков ─────────────────
        if in_python:
            if indent <= python_indent and content:
                in_python = False
            else:
                continue

        if in_translate:
            if indent <= translate_indent and content:
                in_translate = False
            else:
                continue

        # ── Выход из блока menu ───────────────────────────────
        if in_menu and indent <= menu_indent:
            in_menu = False
            menu_indent = -1

        # ── python: / init python: блок ───────────────────────
        if re.match(r"^\s*(?:init\s+)?(?:\d+\s+)?python\s*:", raw_line):
            in_python = True
            python_indent = indent
            continue

        # ── translate блок (другой язык — пропускаем) ─────────
        if re.match(r"^\s*translate\s+\w+\s+", raw_line):
            in_translate = True
            translate_indent = indent
            continue

        # ── $ — строка с inline Python ────────────────────────
        if content.startswith("$"):
            continue

        # ── define char = Character("Имя") ────────────────────
        dm = _DEFINE_CHAR_RE.match(raw_line)
        if dm:
            char_names[dm.group(1)] = _unquote(dm.group(2))
            continue

        # ── menu: ─────────────────────────────────────────────
        if _MENU_RE.match(raw_line):
            in_menu = True
            menu_indent = indent
            continue

        # ── Вариант меню "Choice": ────────────────────────────
        if in_menu:
            cm = _CHOICE_RE.match(raw_line)
            if cm:
                choice_raw = cm.group(2)
                choice_text = _unquote(choice_raw)
                if _should_translate(choice_text):
                    entries.append({
                        "file": fname,
                        "path": f"line:{lineno}",
                        "original": choice_text,
                        "translated": "",
                        "context": "choice",
                        "speaker": "",
                        "game_type": "renpy",   # BUG FIX: was missing
                        "line_start": lineno,
                        "line_end": lineno,
                        "quote_char": choice_raw[0],
                    })
                continue

        # ── Say statement ─────────────────────────────────────
        sm = _SAY_RE.match(raw_line)
        if sm:
            char_var = (sm.group(2) or "").strip()
            text_raw = sm.group(3)

            # char_var — не ключевое слово
            if char_var and char_var.lower() in _RNP_KEYWORDS:
                continue

            text = _unquote(text_raw)
            if not _should_translate(text):
                continue

            # extend / nvl — специальные операторы, не персонажи
            if char_var and char_var.lower() in _RNP_SPECIAL_SAY:
                speaker = ""
                ctx = char_var.lower()
            else:
                speaker = char_names.get(char_var, char_var) if char_var else ""
                ctx = "dialog" if char_var else "narrator"

            entries.append({
                "file": fname,
                "path": f"line:{lineno}",
                "original": text,
                "translated": "",
                "context": ctx,
                "speaker": speaker,
                "game_type": "renpy",   # BUG FIX: was missing
                "line_start": lineno,
                "line_end": lineno,
                "quote_char": text_raw[0],
            })

    return entries


# ─── Главная функция ─────────────────────────────────────────────────────────

def extract_renpy(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
) -> dict:
    """
    Извлечь текст из Ren'Py игры.
    Ищет .rpy файлы в папке game/ (или корне, если game/ нет).
    Сохраняет структурированные JSON в output_dir/extracted/.
    """
    game_dir = Path(game_dir)
    output_dir = Path(output_dir)

    game_sub = game_dir / "game"
    search_dir = game_sub if game_sub.is_dir() else game_dir

    # Если .rpy нет — пробуем декомпилировать .rpyc
    has_rpy, has_rpyc = _check_rpyc_situation(search_dir)
    if not has_rpy and has_rpyc:
        _log("[RenPy] .rpy файлы не найдены, есть .rpyc — запускаю декомпиляцию...", log_callback)
        decompiled = _decompile_rpyc_files(search_dir, log_cb=log_callback, output_dir=output_dir)
        if decompiled == 0:
            _log(
                "[RenPy] ✗ Декомпиляция не удалась.\n"
                "  Установите unrpyc вручную и повторите Extract.",
                log_callback,
            )
            return {
                "game_dir": str(game_dir),
                "game_type": "renpy",
                "total_strings": 0,
                "files": {},
                "error": "rpyc_decompile_failed",
            }
    elif not has_rpy and not has_rpyc:
        _log("[RenPy] ✗ Не найдено ни .rpy ни .rpyc файлов", log_callback)
        return {
            "game_dir": str(game_dir),
            "game_type": "renpy",
            "total_strings": 0,
            "files": {},
            "error": "no_scripts",
        }

    # BUG FIX: exclude _backup_* dirs from scan to avoid re-processing backup copies
    rpy_files = sorted(
        p for p in search_dir.rglob("*.rpy")
        if not any(part.startswith("_backup") for part in p.parts)
    )
    _log(f"[RenPy] Найдено .rpy файлов: {len(rpy_files)}", log_callback)

    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    # Сначала собираем имена персонажей из всех файлов
    char_names: dict[str, str] = {}
    for rpy_path in rpy_files:
        try:
            text = rpy_path.read_text(encoding="utf-8", errors="replace")
            for m in _DEFINE_CHAR_RE.finditer(text):
                char_names[m.group(1)] = _unquote(m.group(2))
        except Exception:
            pass
    if char_names:
        _log(f"[RenPy] Персонажи: {', '.join(f'{k}={v}' for k, v in list(char_names.items())[:8])}", log_callback)

    total_strings = 0
    files_info: dict[str, int] = {}

    for file_idx, rpy_path in enumerate(rpy_files):
        if progress_callback:
            progress_callback(file_idx + 1, len(rpy_files), -1.0, rpy_path.name)

        try:
            rel = rpy_path.relative_to(search_dir)
        except ValueError:
            rel = Path(rpy_path.name)

        fname = str(rel).replace("\\", "/")
        _log(f"[RenPy] Обработка: {fname}", log_callback)

        try:
            entries = _parse_rpy_file(rpy_path, fname, char_names)
        except Exception as e:
            _log(f"[RenPy] ✗ Ошибка чтения {fname}: {e}", log_callback)
            continue

        if not entries:
            continue

        # BUG FIX: was f"{safe_name}.json" where safe_name kept the ".rpy" extension,
        # resulting in "script.rpy.json" (double extension) and a key mismatch in
        # apply_dedup_to_files (file_data keys = jf.name, entry "file" = "script.rpy").
        # Now strip the source extension and store consistent "file" = output json name.
        raw_safe = fname.replace("/", "__").replace("\\", "__")
        # Strip the original extension so we don't get double extension
        safe_stem = raw_safe.rsplit(".", 1)[0] if "." in raw_safe else raw_safe
        
        # BUG FIX: long path protection for Windows
        if len(safe_stem) > 80:
            h = hashlib.md5(safe_stem.encode('utf-8')).hexdigest()[:8]
            safe_stem = f"{safe_stem[:40]}__{h}__{safe_stem[-30:]}"
            
        out_json_name = f"{safe_stem}.json"
        out_file = extract_dir / out_json_name

        # Update file field in all entries to match the saved filename
        for e in entries:
            e["file"] = out_json_name
            e["_source_rpy"] = fname  # original relative .rpy path for the injector

        try:
            atomic_write_json(out_file, entries)
        except Exception as e:
            _log(f"[RenPy] ✗ Не удалось сохранить {out_json_name}: {e}", log_callback)
            continue

        count = len(entries)
        total_strings += count
        files_info[fname] = count
        _log(f"[RenPy]   → {count} строк", log_callback)

    _log(
        f"[RenPy] ✓ Итого: {total_strings} строк из {len(files_info)} файлов",
        log_callback,
    )

    # BUG FIX: extract_index.json was missing — all other engines write it.
    # Pipeline and UI use it to detect extraction status and file counts.
    index = {
        "game_dir":      str(game_dir),
        "engine":        "renpy",
        "total_strings": total_strings,
        "files":         files_info,
    }
    atomic_write_json(output_dir / "extract_index.json", index)

    return {
        "game_dir":      str(game_dir),
        "total_strings": total_strings,
        "files":         files_info,
        "engine":        "renpy",
    }
