"""
godot_injector.py — вставка переводов обратно в файлы Godot (.tscn, .tres, .gd).
"""

import json
from engine.file_utils import atomic_write_text
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

# Компилируем один раз на уровне модуля — не пересоздаём на каждый вызов inject_godot
_TSCN_TEXT_RE = re.compile(
    r'^(\s*)(text|dialog_text|bbcode_text|title)(\s*=\s*")([^"\\]*(?:\\.[^"\\]*)*)(")'
)

def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)

def _escape(s: str) -> str:
    """Экранировать строку для Godot."""
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _unescape_dialogue(s: str) -> str:
    """Обратная операция для Dialogic .dialogue — см. GodotExtractor._unescape."""
    s = s.replace('\\\\', '\x00RPGMTL_BACKSLASH\x00')
    s = s.replace('\\n', '\n').replace('\\"', '"')
    return s.replace('\x00RPGMTL_BACKSLASH\x00', '\\')


def _escape_dialogue(s: str) -> str:
    """Для .dialogue не нужно экранировать кавычки (формат не quoted-string,
    в отличие от .tscn), но перенос строки внутри одной физической строки
    файла разорвал бы построчный формат — экранируем его, симметрично
    GodotExtractor._unescape."""
    return s.replace('\\', '\\\\').replace('\n', '\\n')

def inject_godot(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable | None = None,
    backup: bool = True,
    progress_callback: Callable | None = None,
) -> dict:
    _log("[Godot Inject] Начало вставки переводов", log_callback)

    extract_dir = translated_dir / "extracted"
    trans_files = list(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []
    if not trans_files:
        _log("[Godot Inject] Файлы с переводом не найдены. Сначала Extract → Translate.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    by_file: dict[str, list[dict]] = {}
    for tf in trans_files:
        try:
            entries = json.loads(tf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for entry in entries:
            if entry.get("game_type") != "godot":
                continue
            t = entry.get("translated", "").strip()
            if not t:
                continue
            
            src_path = entry.get("_source_path", "")
            if not src_path:
                continue
            by_file.setdefault(src_path, []).append(entry)

    # ── Идемпотентность ──────────────────────────────────────
    _godot_backup = game_dir / "_backup_godot_original"
    _godot_marker = _godot_backup / ".rpgmtl_injected"
    if _godot_marker.exists():
        _log("[Godot Inject] Маркер предыдущей инъекции. Восстанавливаем оригиналы...", log_callback)
        # BUG FIX: was `for src_path in by_file` — only restored files with current
        # translations. If a game was partially translated, previously injected files
        # without new translations were left in the injected state.
        # Now restore ALL files from the backup directory (same pattern as Wolf/Tyrano).
        for bf in _godot_backup.rglob("*"):
            if not bf.is_file() or bf.name.startswith(".rpgmtl"):
                continue
            try:
                rel = bf.relative_to(_godot_backup)
                dest = game_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bf, dest)
            except Exception as e:
                _log(f"  ✗ {e}", log_callback)
        try:
            _godot_marker.unlink(missing_ok=True)
        except Exception:
            pass

    # Бэкап
    if backup:
        backup_dir = game_dir / "_backup_godot_original"
        if not backup_dir.exists():
            backup_dir.mkdir(parents=True)
            for src_path in by_file:
                f = game_dir / src_path
                if f.exists():
                    dest = backup_dir / src_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(f, dest)
                    except (OSError, shutil.Error):
                        pass
            _log(f"[Godot Inject] Бэкап создан: {backup_dir}", log_callback)

    files_modified = 0
    total_injected = 0
    total_errors = 0

    items = list(by_file.items())
    for idx, (src_rel, entries) in enumerate(items):
        fpath = game_dir / src_rel
        if not fpath.exists():
            _log(f"  ✗ Файл не найден: {src_rel}", log_callback)
            total_errors += len(entries)
            continue

        try:
            # BUG FIX (P1-16, внешний аудит): errors="replace" молча
            # подменяет любые невалидные UTF-8 байты на "�" ДО того, как мы
            # что-либо с файлом делаем — потом файл целиком перезаписывается
            # уже в UTF-8, то есть повреждённые байты уничтожают исходное
            # содержимое НАВСЕГДА, даже в местах, которые перевод не
            # затрагивает. Godot официально требует UTF-8 для .gd/.tscn/
            # .tres/.dialogue (это ограничение самого движка, не просто
            # соглашение) — так что легитимные файлы почти всегда уже
            # валидны, но если файл ПОВРЕЖДЁН или не в UTF-8 по какой-то
            # причине, лучше явно отказаться и пропустить его, чем молча
            # закрепить потерю байтов перезаписью.
            raw_bytes = fpath.read_bytes()
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as e:
                _log(
                    f"  ✗ {src_rel}: файл не в UTF-8 ({e}) — Godot требует UTF-8 "
                    f"для скриптов/ресурсов, файл пропущен без изменений, "
                    f"чтобы не заменить повреждённые байты необратимо.",
                    log_callback,
                )
                total_errors += len(entries)
                continue
            lines = text.splitlines(keepends=True)
        except Exception as e:
            _log(f"  ✗ Ошибка чтения {src_rel}: {e}", log_callback)
            total_errors += len(entries)
            continue

        file_ok = 0
        file_err = 0

        # BUG FIX (внешний аудит, тот же класс, что уже был правильно решён
        # в wolf_injector.py): regex_tr-записи используют offset, привязанный
        # к ИСХОДНОЙ длине файла. Если в файле несколько regex_tr-вхождений
        # и перевод меняет длину строки (почти всегда, RU длиннее EN/JA),
        # offset каждой СЛЕДУЮЩЕЙ записи после уже применённой съезжает.
        # Обрабатываем regex_tr-записи от конца файла к началу (по убыванию
        # offset) — тогда более ранняя замена никогда не сдвигает позицию
        # ещё не обработанных записей. line:-записи (.tscn/.tres) это не
        # затрагивает, они сохраняют исходный порядок.
        def _entry_sort_key(e):
            p = e.get("path", "")
            if p.startswith("regex_tr:") and p[len("regex_tr:"):].isdigit():
                return (0, -int(p[len("regex_tr:"):]))
            return (1, 0)
        entries = sorted(entries, key=_entry_sort_key)

        # Для tscn/tres замена по номеру строки
        # Для gd замена по регулярке tr("...")
        for entry in entries:
            orig = entry.get("original", "")
            trans = entry.get("translated", "")
            path_str = entry.get("path", "")

            # BUG FIX (P0-3, внешний аудит, подтверждено эмпирически):
            # .dialogue (плагин Dialogic) тоже использует path: "line:N" —
            # ТОТ ЖЕ префикс, что и .tscn/.tres — но синтаксис строки
            # совершенно другой ("Character: текст" / "- вариант" / голый
            # нарратив, не "key = \"value\""). Код ниже был единственной
            # веткой для "line:" и требовал совпадения с _TSCN_TEXT_RE —
            # для .dialogue строк регэксп никогда не совпадает, так что
            # 100% записей из .dialogue гарантированно проваливались
            # (file_err), полностью непереведённый файл, без единой
            # подсказки о причине. Экстрактор для .dialogue РЕАЛЬНО
            # извлекал контент (включая speaker context) — только injector
            # не знал, как записать обратно. Диспетчеризуем по расширению
            # файла отдельной веткой, зеркаля парсинг GodotExtractor
            # (see: exts .dialogue branch).
            if path_str.startswith("line:") and fpath.suffix == ".dialogue":
                try:
                    lineno = int(path_str.split(":", 1)[1])
                except ValueError:
                    file_err += 1
                    continue
                if lineno >= len(lines):
                    file_err += 1
                    continue
                line = lines[lineno]
                core = line.rstrip("\r\n")
                ending = line[len(core):]
                leading_ws = core[:len(core) - len(core.lstrip())]
                stripped = core.strip()

                if stripped.startswith("- "):
                    prefix = leading_ws + "- "
                    current_raw = stripped[2:].strip()
                elif ":" in stripped:
                    speaker, _, rest = stripped.partition(":")
                    prefix = leading_ws + speaker.strip() + ": "
                    current_raw = rest.strip()
                else:
                    prefix = leading_ws
                    current_raw = stripped

                current_text = _unescape_dialogue(current_raw)
                # Drift-защита: не патчим, если строка изменилась с Extract —
                # тот же принцип, что везде в проекте (см. injector.py и др.).
                if orig and current_text != orig:
                    file_err += 1
                    continue
                new_line = prefix + _escape_dialogue(trans) + ending
                if new_line != line:
                    lines[lineno] = new_line
                    file_ok += 1
                else:
                    file_err += 1
                continue

            if path_str.startswith("line:"):
                try:
                    lineno = int(path_str.split(":")[1])
                    if lineno < len(lines):
                        line = lines[lineno]
                        safe_trans = _escape(trans)
                        # BUG FIX: replacement was purely positional (by line
                        # number) via .sub(), with no check that the text
                        # currently on that line still matches what
                        # extraction saw. If the .tscn/.tres file changed
                        # between Extract and Inject, this silently
                        # overwrote whatever is NOW there with a translation
                        # of the OLD text — no error, no warning (same class
                        # of bug as _set_nested in engine/injector.py).
                        # .match() gives us the regex-captured current value
                        # (group 4, still in Godot's on-disk escaped form)
                        # so we can compare it against the escaped original
                        # before allowing the write.
                        m = _TSCN_TEXT_RE.match(line)
                        if m is not None and (not orig or m.group(4) == _escape(orig)):
                            new_line = m.group(1) + m.group(2) + m.group(3) + safe_trans + m.group(5)
                            if new_line != line:
                                lines[lineno] = new_line
                                file_ok += 1
                            else:
                                file_err += 1
                        else:
                            file_err += 1
                except ValueError:
                    file_err += 1
            elif path_str.startswith("regex_tr:"):
                # BUG FIX (внешний аудит): extractor уже записывает точный
                # offset вхождения (m.start() в godot_extractor.py), но
                # injector его игнорировал и делал text.replace(target, ...)
                # по ВСЕМУ файлу — если один и тот же tr("X") встречается в
                # файле дважды с РАЗНЫМИ переводами (разные записи entries),
                # первая замена меняет ВСЕ вхождения разом, и вторая запись
                # уже не находит свою цель (target больше нет в тексте) —
                # видимый ✗-сбой, не тихая порча, но легитимный второй
                # перевод так и не применяется. Теперь используем offset для
                # точечной замены ИМЕННО этого вхождения — не трогая другие
                # occurrences того же текста, у каждого из которых может
                # быть свой перевод. Если offset не совпал (старый
                # extract_index без offset, или файл изменился) — откатываемся
                # на прежнее поведение (глобальная замена) с явным warning,
                # чтобы не сломать проекты, извлечённые до этого фикса.
                text = "".join(lines)
                safe_trans = _escape(trans)
                target = f'tr("{_escape(orig)}")'
                replacement = f'tr("{safe_trans}")'
                offset = None
                if path_str[len("regex_tr:"):].isdigit():
                    offset = int(path_str[len("regex_tr:"):])
                if offset is not None and text[offset:offset + len(target)] == target:
                    text = text[:offset] + replacement + text[offset + len(target):]
                    lines = [text]
                    file_ok += 1
                elif target in text:
                    text = text.replace(target, replacement)
                    lines = [text]
                    file_ok += 1
                    _log(f"  ⚠ {src_rel}: regex_tr offset не совпал — применена "
                         f"глобальная замена (могла задеть другие вхождения "
                         f"того же текста)", log_callback)
                else:
                    file_err += 1

        if file_ok > 0:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=fpath.parent, suffix=".tmp", prefix=".rpgmtl_")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(tmp_path, fpath)
                files_modified += 1
                _log(f"  ✓ {src_rel}: вставлено {file_ok}", log_callback)
            except Exception as e:
                _log(f"  ✗ Ошибка записи {src_rel}: {e}", log_callback)
                total_errors += file_ok
        
        total_injected += file_ok
        total_errors += file_err
        
        if progress_callback:
            progress_callback(idx + 1, len(items), -1.0, src_rel)

    _log(f"[Godot Inject] ✓ Готово: файлов={files_modified}, строк={total_injected}, ошибок={total_errors}", log_callback)
    if files_modified > 0:
        try:
            _godot_marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_godot_marker, "injected")
        except Exception as e:
            _log(f"  ⚠ Не удалось записать маркер инъекции: {e}", log_callback)
    return {
        "files_modified": files_modified,
        "total_injected": total_injected,
        "total_errors": total_errors,
    }

