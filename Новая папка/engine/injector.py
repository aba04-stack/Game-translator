"""
injector.py — вставка переводов обратно в RPG Maker MV/MZ JSON-файлы.
Работает точно по path-адресам из extractor.py.
Создаёт резервные копии оригиналов перед изменением.
"""

from engine.file_utils import atomic_write_text, find_dir_ci
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Any

EVENT_CODE_SHOW_TEXT_LINE = 401
EVENT_CODE_SCROLL_LINE = 405


def _log(msg: str, cb: Callable[[str], None] | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


def _data_dir(game_dir: Path) -> Path | None:
    for candidate in [
        game_dir / "data",
        game_dir / "www" / "data",
        *([d] if (d := find_dir_ci(game_dir, "Data")) else []),
    ]:
        if candidate.exists():
            return candidate
    return None


def _set_nested(root: Any, path: str, value: str, original: str = "") -> bool:
    """
    Установить значение по path в JSON-структуре.
    path примеры:
      [0].name
      events[5].pages[0].list[12..15].dialog
      terms.basic[0]
      displayName
    Возвращает True при успехе.

    BUG FIX: запись раньше была чисто позиционной (по path), без проверки,
    что значение, которое сейчас там лежит, всё ещё совпадает с тем, что
    видел extractor. Если файл данных игры изменился между Extract и
    Inject (разработчик отредактировал имя/термин, или массив сдвинулся),
    это молча перезаписывало ТЕКУЩЕЕ значение переводом СТАРОГО текста —
    без ошибки, без предупреждения. Когда original передан и текущее
    значение — строка, не совпадающая с ним, запись пропускается (тот же
    принцип, что уже используют XML/PO/YAML-инжектор Unity и бинарный
    патчер Wolf RPG). Если по этому key/index ещё ничего нет (KeyError/
    IndexError при чтении) — сравнивать не с чем, ведём себя как раньше.
    """
    parts = _tokenize_path(path)
    obj = root
    for i, (kind, key) in enumerate(parts[:-1]):
        try:
            if kind == "index":
                # BUG FIX: dict may use string keys — try int first, then str
                if isinstance(obj, dict):
                    int_key = int(key)
                    obj = obj[int_key] if int_key in obj else obj[key]
                else:
                    obj = obj[int(key)]
            elif kind == "key":
                obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return False

    last_kind, last_key = parts[-1]
    try:
        if last_kind == "index":
            # BUG FIX: dict may use string keys
            if isinstance(obj, dict):
                int_key = int(last_key)
                key_to_use = int_key if int_key in obj else last_key
            else:
                key_to_use = int(last_key)
        else:
            key_to_use = last_key

        if original:
            try:
                current = obj[key_to_use]
            except (KeyError, IndexError, TypeError):
                current = None
            if isinstance(current, str) and current != original:
                return False  # источник изменился с момента Extract — не рискуем

        obj[key_to_use] = value
        return True
    except (KeyError, IndexError, TypeError):
        return False


def _tokenize_path(path: str) -> list[tuple[str, str]]:
    """Разобрать path в список (kind, key): kind = 'index' | 'key'."""
    parts = []
    rest = path.lstrip(".")
    token_re = re.compile(r"\[(\d+)\]|\.?([^\[.]+)")
    for m in token_re.finditer(rest):
        if m.group(1) is not None:
            parts.append(("index", m.group(1)))
        else:
            parts.append(("key", m.group(2)))
    return parts


def _inject_dialog(data: Any, path: str, translation: str, original: str = "",
                    log_callback: Callable | None = None) -> bool:
    """
    Вставить диалог в диапазон команд [start..end].dialog
    path пример: events[3].pages[0].list[10..13].dialog
    """
    m = re.search(r"\[(\d+)\.\.(\d+)\]\.dialog$", path)
    if not m:
        return False
    start, end = int(m.group(1)), int(m.group(2))
    prefix = path[:m.start()]

    parent = _resolve_path(data, prefix)
    if parent is None or not isinstance(parent, list):
        return False

    # BUG FIX: _apply_warp converts \n → \\n; split on both
    lines = re.split(r"\n|\\n", translation)
    original_lines = re.split(r"\n|\\n", original) if original else []
    count = end - start + 1
    # BUG FIX (внешний аудит): RPG Maker выделяет ФИКСИРОВАННОЕ число команд
    # под многострочный блок — inject не может добавить/убрать команды, так
    # что перевод обязан лечь ровно в `count` строк. Раньше избыток молча
    # обрезался (`lines[:count]`), безвозвратно теряя хвост переведённого
    # текста (тот же класс проблемы, что P0-1 у Smart Warp, только уровнем
    # ниже). Теперь лишние строки не выбрасываются, а схлопываются в
    # последний доступный слот — контент не теряется, максимум страдает
    # аккуратность разбивки на строки в игровом окне сообщения.
    if len(lines) > count:
        original_line_count = len(lines)
        overflow = " ".join(lines[count - 1:])
        lines = lines[:count - 1] + [overflow]
        _log(f"    ⚠ {path}: перевод в {original_line_count} строк(и) "
             f"не помещается в {count} доступных — лишнее объединено в последнюю строку, "
             f"текст не потерян, но проверьте разбивку.", log_callback)
    while len(lines) < count:
        lines.append("")
    if original_lines:
        while len(original_lines) < count:
            original_lines.append("")
        original_lines = original_lines[:count]

    # Never patch a shifted/stale event block. Every source line must still
    # equal the exact text captured during Extract. Otherwise the user may have
    # edited the game after Extract and positional injection could hit another
    # dialogue line.
    if original_lines:
        for offset in range(count):
            idx = start + offset
            if idx >= len(parent) or not isinstance(parent[idx], dict):
                return False
            cmd = parent[idx]
            params = cmd.get("parameters", [])
            current = params[0] if params and isinstance(params[0], str) else ""
            if current != original_lines[offset]:
                return False

    for i, line in enumerate(lines):
        idx = start + i
        if idx < len(parent):
            cmd = parent[idx]
            if isinstance(cmd, dict) and cmd.get("code") in (
                EVENT_CODE_SHOW_TEXT_LINE, EVENT_CODE_SCROLL_LINE
            ):
                params = cmd.get("parameters", [])
                if params:
                    params[0] = line
                    cmd["parameters"] = params
                # NOTE: if params is empty the dialog entry is malformed;
                # we skip silently rather than crash (extractor would have caught it)
    return True


def _inject_scroll(data: Any, path: str, translation: str, original: str = "",
                    log_callback: Callable | None = None) -> bool:
    """Аналог _inject_dialog для scroll text."""
    m = re.search(r"\[(\d+)\.\.(\d+)\]\.scroll$", path)
    if not m:
        return False
    start, end = int(m.group(1)), int(m.group(2))
    prefix = path[:m.start()]

    parent = _resolve_path(data, prefix)
    if parent is None or not isinstance(parent, list):
        return False

    # BUG FIX: _apply_warp converts \n → \\n; split on both
    lines = re.split(r"\n|\\n", translation)
    original_lines = re.split(r"\n|\\n", original) if original else []
    count = end - start + 1
    # BUG FIX (внешний аудит): см. идентичный комментарий в _inject_dialog —
    # избыток строк схлопывается в последний слот вместо потери текста.
    if len(lines) > count:
        original_line_count = len(lines)
        overflow = " ".join(lines[count - 1:])
        lines = lines[:count - 1] + [overflow]
        _log(f"    ⚠ {path}: перевод в {original_line_count} строк(и) "
             f"не помещается в {count} доступных — лишнее объединено в последнюю строку, "
             f"текст не потерян, но проверьте разбивку.", log_callback)
    while len(lines) < count:
        lines.append("")
    if original_lines:
        while len(original_lines) < count:
            original_lines.append("")
        original_lines = original_lines[:count]

    # Never patch a shifted/stale event block. Every source line must still
    # equal the exact text captured during Extract. Otherwise the user may have
    # edited the game after Extract and positional injection could hit another
    # dialogue line.
    if original_lines:
        for offset in range(count):
            idx = start + offset
            if idx >= len(parent) or not isinstance(parent[idx], dict):
                return False
            cmd = parent[idx]
            params = cmd.get("parameters", [])
            current = params[0] if params and isinstance(params[0], str) else ""
            if current != original_lines[offset]:
                return False

    for i, line in enumerate(lines):
        idx = start + i
        if idx < len(parent):
            cmd = parent[idx]
            if isinstance(cmd, dict) and cmd.get("code") == EVENT_CODE_SCROLL_LINE:
                params = cmd.get("parameters", [])
                if params:
                    params[0] = line
                    cmd["parameters"] = params
    return True


def _resolve_path(data: Any, path: str) -> Any:
    """Получить объект по path (без финального ключа)."""
    if not path:
        return data
    parts = _tokenize_path(path)
    obj = data
    for kind, key in parts:
        try:
            if kind == "index":
                # BUG FIX: events dict in some RPG Maker versions uses string keys
                # like {"1": event1, "2": event2}. Try int first, fall back to str.
                if isinstance(obj, dict):
                    int_key = int(key)
                    if int_key in obj:
                        obj = obj[int_key]
                    elif key in obj:
                        obj = obj[key]
                    else:
                        return None
                else:
                    obj = obj[int(key)]
            else:
                obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return None
    return obj


def _inject_plugin_arg(data: Any, path: str, translation: str, original: str = "") -> bool:
    """
    Вставить перевод в аргумент плагин-команды MZ.
    path пример: events[3].pages[0].list[12].plugin_arg.Text
    params[3] — JSON-строка с именованными аргументами.

    BUG FIX: та же проверка original, что и в _set_nested — args[key] = translation
    раньше выполнялся вслепую, без сверки с тем, что extractor видел в этом
    аргументе. См. комментарий в _set_nested для полного объяснения риска.
    """
    m = re.search(r"^(.*)\[(\d+)\]\.plugin_arg\.(.+)$", path)
    if not m:
        return False
    parent_path, idx_str, key = m.group(1), m.group(2), m.group(3)
    idx = int(idx_str)

    parent = _resolve_path(data, parent_path.rstrip(".")) if parent_path.rstrip(".") else data
    if parent is None or not isinstance(parent, list) or idx >= len(parent):
        return False

    cmd = parent[idx]
    if not isinstance(cmd, dict):
        return False

    params = cmd.get("parameters", [])
    if len(params) < 4 or not isinstance(params[3], str):
        return False

    try:
        args = json.loads(params[3])
        if not isinstance(args, dict):
            return False
        if original and isinstance(args.get(key), str) and args.get(key) != original:
            return False  # источник изменился с момента Extract — не рискуем
        args[key] = translation
        params[3] = json.dumps(args, ensure_ascii=False)
        cmd["parameters"] = params
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _inject_script_literal(data: Any, path: str, original: str, translation: str) -> bool:
    """
    Вставить перевод script_literal обратно в строку Script-команды (355/655).

    BUG FIX: путь вида "...[i].parameters[0]" раньше проваливался в общий
    _set_nested(), который ЦЕЛИКОМ заменяет значение по пути — то есть вся
    строка JS-кода (например '$gameMessage.add("こんにちは、旅人よ！");')
    заменялась ГОЛЫМ переводом ('Привет, путник!'), полностью теряя
    окружающий код. Игра падала при попытке выполнить такую "строку кода"
    с "SyntaxError: Invalid or unexpected token" (реальный краш,
    воспроизведён и подтверждён на настоящем extractor/injector).

    Теперь ищем оригинальную подстроку ВНУТРИ текущей строки кода и
    заменяем только её, сохраняя всё окружение. Также экранируем кавычки/
    бэкслеши/переносы строк в переводе под тот же стиль кавычек, которым
    была обёрнута исходная строка — иначе кавычка внутри самого перевода
    преждевременно закроет JS string literal и сломает синтаксис остатка
    строки тем же образом.
    """
    parts = _tokenize_path(path)
    obj = data
    for kind, key in parts[:-1]:
        try:
            if kind == "index":
                if isinstance(obj, dict):
                    int_key = int(key)
                    obj = obj[int_key] if int_key in obj else obj[key]
                else:
                    obj = obj[int(key)]
            elif kind == "key":
                obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return False

    last_kind, last_key = parts[-1]
    try:
        if isinstance(obj, dict):
            int_key = int(last_key)
            key_to_use = int_key if int_key in obj else last_key
        else:
            key_to_use = int(last_key)
        current_line = obj[key_to_use]
    except (KeyError, IndexError, TypeError, ValueError):
        return False

    if not isinstance(current_line, str) or original not in current_line:
        return False

    # BUG FIX: искать нужно ПРЕЖДЕ ВСЕГО точное вхождение "quote + original +
    # same quote" (original был извлечён как m.group(1) из _JS_STRING_RE,
    # т.е. всегда — необработанное, необрезанное содержимое МЕЖДУ парой
    # кавычек в исходном файле, без декодирования escape-последовательностей;
    # значит он гарантированно встречается verbatim именно в таком виде).
    #
    # Голый .find(original) без привязки к границам кавычек ошибочно
    # находил original как ПОДСТРОКУ более длинной, отдельной цитируемой
    # строки, если та встречалась РАНЬШЕ в той же строке кода — например:
    #   a.add("こんにちは、世界"); b.add("こんにちは");
    # Инъекция перевода для короткой "こんにちは" находила её ВНУТРИ более
    # длинной "こんにちは、世界" (которая идёт первой) и портила именно её,
    # оставляя настоящую цель (стоящую в b.add(...)) нетронутой. Ищем
    # сначала anchored-вариант; голый substring-поиск — лишь fallback на
    # случай нестандартного контента, где anchored-поиск не сработал.
    idx = None
    end_idx = None
    quote_char = None
    for q in ('"', "'"):
        anchored = q + original + q
        pos = current_line.find(anchored)
        if pos != -1:
            idx = pos + 1
            end_idx = idx + len(original)
            quote_char = q
            break

    if idx is None:
        idx = current_line.find(original)
        end_idx = idx + len(original)
        # Определяем символ кавычки, которым была обёрнута исходная строка —
        # смотрим непосредственно перед original и сразу после него.
        if idx > 0 and end_idx < len(current_line) and current_line[idx - 1] == current_line[end_idx]:
            if current_line[idx - 1] in ("'", '"'):
                quote_char = current_line[idx - 1]

    safe_translation = translation
    if quote_char:
        # Бэкслеши экранируем первыми, иначе последующее экранирование
        # кавычки задвоится при повторном проходе по уже вставленным \".
        safe_translation = safe_translation.replace("\\", "\\\\").replace(quote_char, "\\" + quote_char)
    # JS не допускает буквальный перенос строки внутри "..."/'...' —
    # ИИ иногда переводит многострочно, даже если оригинал был одной строкой.
    safe_translation = safe_translation.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "")

    new_line = current_line[:idx] + safe_translation + current_line[end_idx:]
    try:
        obj[key_to_use] = new_line
        return True
    except (KeyError, IndexError, TypeError):
        return False


def _apply_entry(data: Any, path: str, translation: str, original: str = "",
                  log_callback: Callable | None = None) -> bool:
    """Применить один перевод к данным."""
    if ".dialog" in path and re.search(r"\[\d+\.\.\d+\]\.dialog", path):
        return _inject_dialog(data, path, translation, original, log_callback)
    if ".scroll" in path and re.search(r"\[\d+\.\.\d+\]\.scroll", path):
        return _inject_scroll(data, path, translation, original, log_callback)
    if ".plugin_arg." in path:
        return _inject_plugin_arg(data, path, translation, original)
    if re.search(r"\[\d+\]\.parameters\[0\]$", path):
        return _inject_script_literal(data, path, original, translation)
    return _set_nested(data, path, translation, original)


def inject_translations(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    backup: bool = True,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
) -> dict:
    """
    Главная функция вставки переводов.
    translated_dir — папка с *_translated.json файлами (или extracted/).
    Возвращает сводку.
    """
    _log("[Injector] Начало вставки переводов", log_callback)
    _log(f"[Injector] Игра: {game_dir}", log_callback)
    _log(f"[Injector] Переводы: {translated_dir}", log_callback)

    data_dir = _data_dir(game_dir)
    if not data_dir:
        msg = f"[Injector] ✗ Папка data/ не найдена в {game_dir}"
        _log(msg, log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0, "total_skipped": 0}

    trans_files = sorted(translated_dir.glob("*_translated.json"))
    if not trans_files:
        # BUG FIX: was "*.json" which would include source files too
        trans_files = sorted((translated_dir / "extracted").glob("*_translated.json"))
    if not trans_files:
        # BUG FIX: was raising FileNotFoundError — callers (UI/pipeline) expect a dict,
        # not an exception, when there are simply no translations yet.
        _log("[Injector] Нет файлов с переводом — инъекция пропущена.", log_callback)
        return {
            "files_modified": 0, "files_skipped": 0, "total_injected": 0,
            "total_skipped": 0, "total_errors": 0,
        }

    # Проверка на идемпотентность (защита от двойного перевода)
    injection_marker = data_dir / ".rpgmtl_injected"
    if injection_marker.exists():
        _log("[Injector] ВНИМАНИЕ: Папка data уже содержит инъектированные переводы.", log_callback)
        _log("[Injector] Повторная инъекция поверх переведенных файлов приведет к двойному переводу.", log_callback)

        # Если есть бэкап, восстанавливаем из него перед новой инъекцией
        backup_dir = game_dir / "_backup_original"
        if backup_dir.exists():
            _log("[Injector] Восстанавливаем оригинальные файлы из бэкапа перед инъекцией...", log_callback)
            for b_file in backup_dir.glob("*.json"):
                try:
                    shutil.copy2(b_file, data_dir / b_file.name)
                except (OSError, shutil.Error):
                    pass
            _log("[Injector] Оригиналы успешно восстановлены.", log_callback)
            # BUG FIX: remove marker now so that if inject finds nothing to do
            # (files_modified == 0), the marker is gone and the next inject starts
            # clean instead of seeing a stale marker and redundantly restoring again.
            try:
                injection_marker.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            _log("[Injector] ОШИБКА: Бэкап не найден. Инъекция отменена для защиты файлов.", log_callback)
            return {
                "files_modified": 0, "files_skipped": 0, "total_injected": 0,
                "total_skipped": 0, "total_errors": 0,
            }

    if backup:
        backup_dir = game_dir / "_backup_original"
        if not backup_dir.exists():
            backup_dir.mkdir(parents=True)
            _log(f"[Injector] Создан бэкап: {backup_dir}", log_callback)
            for jf in data_dir.glob("*.json"):
                try:
                    shutil.copy2(jf, backup_dir / jf.name)
                except (OSError, shutil.Error):
                    pass
            _log(f"[Injector] Скопировано оригиналов: {len(list(backup_dir.glob('*.json')))}", log_callback)
        else:
            _log(f"[Injector] Бэкап уже существует: {backup_dir}", log_callback)

    total_injected = 0
    total_skipped = 0
    total_errors = 0
    files_modified = 0

    for file_idx, trans_file in enumerate(trans_files):
        # BUG FIX: .replace("_translated", "") replaces ALL occurrences in the name.
        # E.g. "Map_translated_forest_translated.json" → "Map__forest.json" (wrong).
        # Correct: only strip the suffix "_translated" immediately before ".json".
        stem = trans_file.stem  # e.g. "Actors_translated"
        if stem.endswith("_translated"):
            orig_name = stem[: -len("_translated")] + trans_file.suffix
        else:
            orig_name = trans_file.name

        orig_path = data_dir / orig_name
        if not orig_path.exists():
            _log(f"  [Injector] Пропуск {trans_file.name}: оригинал {orig_name} не найден", log_callback)
            continue

        _log(f"  [Injector] {trans_file.name} → {orig_name}", log_callback)

        try:
            with open(trans_file, "r", encoding="utf-8") as f:
                entries: list[dict] = json.load(f)
        except Exception as e:
            _log(f"    ✗ Ошибка чтения {trans_file.name}: {e}", log_callback)
            total_errors += 1
            continue

        try:
            with open(orig_path, "r", encoding="utf-8") as f:
                game_data = json.load(f)
        except Exception as e:
            _log(f"    ✗ Ошибка чтения {orig_name}: {e}", log_callback)
            total_errors += 1
            continue

        file_injected = 0
        file_skipped = 0
        file_errors = 0

        for entry in entries:
            translation = entry.get("translated", "").strip()
            if not translation:
                file_skipped += 1
                continue
            path = entry.get("path", "")
            if not path:
                file_skipped += 1
                continue

            try:
                ok = _apply_entry(game_data, path, translation, entry.get("original", ""), log_callback)
                if ok:
                    file_injected += 1
                else:
                    file_errors += 1
                    _log(f"    ✗ Не удалось вставить: {path!r}", log_callback)
            except Exception as e:
                file_errors += 1
                _log(f"    ✗ Ошибка при вставке {path!r}: {e}", log_callback)

        # Записываем только если реально что-то вставили.
        # BUG FIX: ранее файл писался (и files_modified++) даже когда file_injected == 0
        # (все строки пропущены). Это приводило к тому, что маркер идемпотентности
        # пересоздавался при «пустом» инжекте — следующий inject снова делал restore.
        if file_injected > 0:
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=orig_path.parent, suffix=".tmp", prefix=".rpgmtl_"
                )
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        json.dump(game_data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, orig_path)
                except Exception:
                    try: os.unlink(tmp_path)
                    except Exception: pass
                    raise
                files_modified += 1
            except Exception as e:
                _log(f"    ✗ Ошибка записи {orig_name}: {e}", log_callback)
                file_errors += 1
        _log(
            f"    ✓ Вставлено: {file_injected}, пропущено: {file_skipped}, ошибок: {file_errors}",
            log_callback,
        )

        total_injected += file_injected
        total_skipped += file_skipped
        total_errors += file_errors
        if progress_callback:
            progress_callback(file_idx + 1, len(trans_files), -1.0, trans_file.name)

    summary = {
        "files_modified": files_modified,
        "total_injected": total_injected,
        "total_skipped": total_skipped,
        "total_errors": total_errors,
    }
    _log(
        f"[Injector] ✓ Готово: файлов изменено={files_modified}, "
        f"строк вставлено={total_injected}, пропущено={total_skipped}, ошибок={total_errors}",
        log_callback,
    )
    
    # Ставим маркер инъекции
    if files_modified > 0:
        try:
            injection_marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(injection_marker, "injected")
        except Exception as e:
            _log(f"  ⚠ Не удалось записать маркер инъекции: {e}", log_callback)
            
    return summary
