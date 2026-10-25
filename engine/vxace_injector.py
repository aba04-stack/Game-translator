"""
vxace_injector.py — вставка переводов в RPG Maker VX/Ace .rvdata2 файлы.

Читает переведённые JSON, находит нужные объекты в Ruby Marshal
и заменяет текст, сохраняя всю структуру файла нетронутой.
"""

import importlib
import io
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from engine.file_utils import atomic_write_text, find_dir_ci


def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


def _get_ruby_str(obj):
    """Получить текст из RubyString или str."""
    try:
        from rubymarshal.reader import RubyString
        if isinstance(obj, RubyString):
            return obj.text or ""
    except ImportError:
        pass
    return str(obj) if obj is not None else ""


def _set_ruby_str(obj, key: str, new_text: str, original: str = "") -> bool:
    """
    Установить новый текст в атрибут RubyObject.

    BUG FIX: original уже принимался в _apply_translation, но никогда не
    использовался для проверки — запись была вслепую по пути, без сверки
    с тем, что extractor видел в этом атрибуте (тот же риск и тот же
    фикс, что и в _set_nested из engine/injector.py — см. комментарий там
    для полного объяснения). RubyString.__eq__ умеет сравниваться
    напрямую с обычной str, поэтому old != original работает как есть.
    """
    try:
        from rubymarshal.reader import RubyString
        old = obj.attributes.get(key)
        if old is None:
            return False
        if original and isinstance(old, (RubyString, str)) and old != original:
            return False  # источник изменился с момента Extract — не рискуем
        if isinstance(old, RubyString):
            obj.attributes[key] = RubyString(new_text, attributes=old.attributes)
        else:
            obj.attributes[key] = RubyString(new_text)
        return True
    except (AttributeError, ImportError):
        return False


def _load_rvdata2(path: Path):
    import rubymarshal.reader as rdr
    try:
        with open(path, "rb") as f:
            return rdr.load(f)
    except ValueError as e:
        if "Unknown Ruby object type" in str(e) or "Unknown class" in str(e):
            _log(f"  ⚠ Пропуск {path.name}: содержит неизвестные Ruby классы ({e})")
            return None
        raise
    except Exception as e:
        _log(f"  ⚠ Пропуск {path.name}: ошибка парсинга ({e})")
        return None


def _save_rvdata2(path: Path, data) -> bool:
    """Сохранить данные в .rvdata2, используя атомарную запись через temp файл."""
    import rubymarshal.writer as wrt
    buf = io.BytesIO()
    wrt.write(buf, data)
    raw = buf.getvalue()
    # BUG FIX: прямой write_bytes обрезает файл при сбое. Атомарная замена безопаснее.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".rpgmtl_")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(raw)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise
    return True


# ── Парсер пути ────────────────────────────────────────────────────────────────

_PATH_SEGMENT = re.compile(
    r'@[\w]+|\[\d+\]|\[\d+\.\.\d+\]|\.\w+'
)

def _parse_path(path: str) -> list:
    """
    Разобрать путь вида:
      [1]@name
      events[3].pages[0].list[5..7].dialog
      [2].pages[0].list[3].parameters[0][0]
    в список шагов [(type, value), ...]
    """
    steps = []
    i = 0
    s = path
    while i < len(s):
        # @attr
        m = re.match(r"@(\w+)", s[i:])
        if m:
            steps.append(("attr", "@" + m.group(1)))
            i += m.end(); continue
        # [N..M]
        m = re.match(r"\[(\d+)\.\.(\d+)\]", s[i:])
        if m:
            steps.append(("range", (int(m.group(1)), int(m.group(2)))))
            i += m.end(); continue
        # [N]
        m = re.match(r"\[(\d+)\]", s[i:])
        if m:
            steps.append(("index", int(m.group(1))))
            i += m.end(); continue
        # word (имя поля: events, pages, list, dialog...)
        m = re.match(r"[.]*([a-zA-Z_]\w*)", s[i:])
        if m:
            steps.append(("key", m.group(1)))
            i += m.end(); continue
        i += 1
    return steps

def _navigate(data, steps: list, stop_before_last=False):
    """
    Пройти по steps и вернуть (container, last_step).
    Если stop_before_last=True — вернуть объект на предпоследнем шаге.
    """
    obj = data
    limit = len(steps) - (1 if stop_before_last else 0)
    for step_type, step_val in steps[:limit]:
        try:
            if step_type == 'attr':
                obj = obj.attributes[step_val]
            elif step_type == 'index':
                # Для dict с int ключами (events hash) и list
                if isinstance(obj, dict):
                    obj = obj[step_val]
                elif hasattr(obj, 'attributes'):
                    # Никогда не должно случиться, но fallback
                    obj = obj[step_val]
                else:
                    obj = obj[step_val]
            elif step_type == 'key':
                # .events, .pages, .list, .parameters → пробуем @key в атрибутах
                attr_key = f"@{step_val}"
                if hasattr(obj, 'attributes') and attr_key in obj.attributes:
                    obj = obj.attributes[attr_key]
                elif hasattr(obj, 'attributes') and step_val in obj.attributes:
                    obj = obj.attributes[step_val]
                # .dialog, .scroll — маркеры, не навигируем
                elif step_val in ('dialog', 'scroll'):
                    pass
                # иначе пробуем как dict key
                elif isinstance(obj, dict) and step_val in obj:
                    obj = obj[step_val]
            elif step_type == 'range':
                # Навигируем к @list (ищем его в предыдущем шаге)
                # На самом деле range — это уже в нужном контейнере
                pass
        except (KeyError, IndexError, TypeError, AttributeError):
            return None, None
    last = steps[-1] if steps else None
    return obj, last


# ── Применение перевода ─────────────────────────────────────────────────────────

def _apply_translation(data, path: str, original: str, translated: str) -> bool:
    """Вставить перевод в объект data по пути path."""
    from rubymarshal.reader import RubyString
    steps = _parse_path(path)
    if not steps:
        return False

    last_type, last_val = steps[-1]

    # ── Простой атрибут: [1]@name ──────────────────────────────────
    if last_type == 'attr':
        container, _ = _navigate(data, steps, stop_before_last=True)
        if container is None:
            return False
        return _set_ruby_str(container, last_val, translated, original)

    # ── Диалог/скролл: list[5..7].dialog ───────────────────────────
    if last_type == 'key' and last_val in ('dialog', 'scroll'):
        # Ищем range перед .dialog
        range_step = None
        for st, sv in reversed(steps[:-1]):
            if st == 'range':
                range_step = sv
                break
        if range_step is None:
            return False
        start_idx, end_idx = range_step

        # Навигируем до list
        list_steps = []
        for st, sv in steps:
            if st == 'range':
                break
            list_steps.append((st, sv))
        cmd_list, _ = _navigate(data, list_steps) if list_steps else (data, None)
        if cmd_list is None:
            return False
        count = end_idx - start_idx + 1

        # BUG FIX (найдено при аудите покрытия): эта ветка получала `original`
        # третьим параметром, но НИКОГДА его не использовала — в отличие от
        # ветки 'index' чуть ниже в этой же функции (которая явно сравнивает
        # `old != original`) и в отличие от _inject_dialog/_inject_scroll в
        # injector.py (MV/MZ), где ровно такая проверка есть с комментарием
        # "Never patch a shifted/stale event block". Без неё: если
        # разработчик игры поменял диалог ПОСЛЕ Extract (например, заменил
        # одну реплику в блоке из нескольких строк), Inject молча писал
        # перевод УСТАРЕВШЕГО текста поверх НОВОЙ строки разработчика —
        # подтверждено эмпирически. Теперь, как и для MV/MZ: каждая строка
        # диапазона должна дословно совпадать с тем, что было при Extract,
        # иначе весь диапазон целиком пропускается (никогда не патчим только
        # часть — это может расфазировать нумерацию строк).
        original_lines = re.split(r"\n|\\n", original) if original else []
        if original_lines:
            for offset in range(count):
                idx = start_idx + offset
                if idx >= len(cmd_list):
                    return False
                cmd = cmd_list[idx]
                try:
                    params = cmd.attributes.get("@parameters", [])
                    current = _get_ruby_str(params[0]) if params else ""
                except AttributeError:
                    return False
                expected = original_lines[offset] if offset < len(original_lines) else ""
                if current != expected:
                    return False  # источник изменился с момента Extract — не рискуем

        # BUG FIX: use re.split to handle both literal \n and Smart Warp \\n
        new_lines = re.split(r"\n|\\n", translated)

        for offset in range(count):
            idx = start_idx + offset
            if idx >= len(cmd_list):
                break
            cmd = cmd_list[idx]
            new_text = new_lines[offset] if offset < len(new_lines) else ""
            try:
                params = cmd.attributes.get("@parameters", [])
                if params:
                    params[0] = RubyString(new_text)
            except (AttributeError, IndexError):
                pass
        return True

    # ── Индекс в параметрах: .parameters[0][0] ──────────────────────
    if last_type == 'index':
        container, _ = _navigate(data, steps, stop_before_last=True)
        if container is None:
            return False
        try:
            old = container[last_val]
            # BUG FIX: same check as _set_ruby_str above — this branch
            # received `original` too but never compared it against `old`.
            if original and isinstance(old, (RubyString, str)) and old != original:
                return False  # источник изменился с момента Extract — не рискуем
            if isinstance(old, RubyString):
                container[last_val] = RubyString(translated)
            else:
                container[last_val] = translated
            return True
        except (IndexError, TypeError):
            return False

    return False


# ── Главная функция ─────────────────────────────────────────────────────────────

def inject_vxace(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable | None = None,
    backup: bool = True,
    progress_callback: Callable | None = None,
) -> dict:
    """Вставить переводы в .rvdata2 файлы."""
    _log("[VXAce Inject] Начало вставки переводов", log_callback)

    # BUG FIX: см. тот же фикс в extract_vxace — явная проверка ДО обработки
    # файлов вместо тихого per-file "✗ ...: No module named 'rubymarshal'".
    try:
        importlib.import_module("rubymarshal.reader")
    except ImportError:
        msg = (
            "[VXAce Inject] ✗ Библиотека 'rubymarshal' не установлена — она нужна "
            "для записи .rvdata2 файлов RPG Maker VX/Ace.\n"
            "  Установите командой: pip install rubymarshal>=1.2.0"
        )
        _log(msg, log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0,
                "error": "rubymarshal_missing"}

    extract_dir = translated_dir / "extracted"
    trans_files = list(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []
    if not trans_files:
        # BUG FIX: API consistency — возвращаем dict как другие injector-ы
        _log("[VXAce Inject] Файлы с переводом не найдены. Сначала Extract → Translate.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # Группируем переводы по rvdata2 файлу
    # {rvdata2_filename: [(path, original, translated), ...]}
    by_file: dict[str, list[tuple[str, str, str]]] = {}

    for tf in trans_files:
        try:
            entries = json.loads(tf.read_text(encoding="utf-8"))
        except Exception as e:
            _log(f"  ✗ Ошибка чтения {tf.name}: {e}", log_callback)
            continue
        for entry in entries:
            if entry.get("game_type") != "vxace":
                continue
            t = entry.get("translated", "").strip()
            if not t:
                continue
            # BUG FIX: "file" now holds the JSON output name (e.g. "Actors.json"),
            # not the original .rvdata2 path. Use "_source_rvdata2" added by extractor.
            fname = entry.get("_source_rvdata2", "") or entry.get("file", "")
            path  = entry.get("path", "")
            orig  = entry.get("original", "")
            if not fname or not path or not orig:
                continue
            by_file.setdefault(fname, []).append((path, orig, t))

    if not by_file:
        _log("[VXAce Inject] Нет строк с переводом.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # Найти data dir
    data_dir = None
    data_dir = find_dir_ci(game_dir, "Data")
    if data_dir and not list(data_dir.glob("*.rvdata2")):
        data_dir = None
    if not data_dir:
        msg = f"[VXAce Inject] .rvdata2 файлы не найдены в {game_dir}"
        _log(msg, log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # ── Идемпотентность ──────────────────────────────────────
    _vxace_backup = game_dir / "_backup_vxace_original"
    _vxace_marker = _vxace_backup / ".rpgmtl_injected"
    if _vxace_marker.exists():
        _log("[VXAce Inject] Маркер предыдущей инъекции. Восстанавливаем оригиналы...", log_callback)
        # BUG FIX: was `for fname in by_file` — only restored files with current
        # translations. Previously injected files without new translations remained
        # in the injected state. Now restore ALL backed-up files.
        for bf in _vxace_backup.iterdir():
            if not bf.is_file() or bf.name.startswith(".rpgmtl"):
                continue
            try:
                shutil.copy2(bf, data_dir / bf.name)
            except Exception as e:
                _log(f"  ✗ {e}", log_callback)
        try:
            _vxace_marker.unlink(missing_ok=True)
        except Exception:
            pass

    # Бэкап
    if backup:
        backup_dir = game_dir / "_backup_vxace_original"
        if not backup_dir.exists():
            backup_dir.mkdir(parents=True)
            for fname in by_file:
                src = data_dir / fname
                if src.exists():
                    try:
                        shutil.copy2(src, backup_dir / fname)
                    except (OSError, shutil.Error):
                        pass
            _log(f"[VXAce Inject] Бэкап создан: {backup_dir}", log_callback)

    files_modified = 0
    total_injected = 0
    total_errors   = 0

    items = list(by_file.items())
    for file_idx, (fname, translations) in enumerate(items):
        rvdata_path = data_dir / fname
        if not rvdata_path.exists():
            _log(f"  Файл не найден: {fname}", log_callback)
            total_errors += 1
            continue

        try:
            data = _load_rvdata2(rvdata_path)
        except Exception as e:
            _log(f"  ✗ Ошибка чтения {fname}: {e}", log_callback)
            total_errors += 1
            continue

        # BUG FIX: _load_rvdata2 returns None (not raises) for unknown Ruby class errors.
        # Without this guard every translation would silently fail with file_err++ and
        # the log would show "errors=N" with no explanation instead of "file skipped".
        if data is None:
            _log(f"  ⚠ Пропуск {fname}: файл не удалось распарсить (неизвестный Ruby тип)", log_callback)
            total_errors += 1
            continue

        file_ok  = 0
        file_err = 0

        for path, original, translated in translations:
            try:
                ok = _apply_translation(data, path, original, translated)
                if ok:
                    file_ok += 1
                else:
                    file_err += 1
                    _log(f"    ⚠ Не удалось вставить: {path!r}", log_callback)
            except Exception as e:
                file_err += 1
                _log(f"    ✗ {path}: {e}", log_callback)

        if file_ok > 0:
            try:
                _save_rvdata2(rvdata_path, data)
                files_modified += 1
                _log(f"  ✓ {fname}: вставлено={file_ok}, ошибок={file_err}", log_callback)
            except Exception as e:
                _log(f"  ✗ Ошибка записи {fname}: {e}", log_callback)
                total_errors += 1
        else:
            _log(f"  — {fname}: нет изменений (ошибок={file_err})", log_callback)

        total_injected += file_ok
        total_errors   += file_err

        if progress_callback:
            progress_callback(file_idx + 1, len(items), -1.0, "")

    _log(
        f"[VXAce Inject] ✓ файлов={files_modified}, "
        f"строк={total_injected}, ошибок={total_errors}",
        log_callback,
    )
    if files_modified > 0:
        try:
            _vxace_marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_vxace_marker, "injected")
        except Exception as e:
            _log(f"  ⚠ Не удалось записать маркер инъекции: {e}", log_callback)
    return {
        "files_modified": files_modified,
        "total_injected": total_injected,
        "total_errors":   total_errors,
    }
