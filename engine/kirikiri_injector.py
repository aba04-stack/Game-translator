"""
kirikiri_injector.py — вставка переводов в Kirikiri/KAG .ks файлы.

Стратегия: замена текста по номеру строки, с сохранением всех тегов.
"""

import json
from engine.file_utils import atomic_write_text
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

_TAG_RE     = re.compile(r'\[/?[^\]\n]+\]')
# BUG FIX: was [^"'"'\\["'"'\n] — duplicate in char class
_DIALOG_RE  = re.compile(r'^([^\u300c\u300e\[\n]{1,20})[\u300c\u300e](.*?)[\u300d\u300f]')


def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


def _replace_text_in_line(original_line: str, old_text: str, new_text: str) -> str:
    """
    Заменить текст в строке KAG, сохранив все теги и структуру.

    BUG FIX: предыдущая реализация использовала `original_line[len(stripped):]`
    для получения суффикса тегов, что неверно — stripped не является
    подстрокой original_line в той же позиции, т.к. теги были удалены.
    Правильный подход: искать old_text прямо в original_line.
    """
    # 1. Диалог в японских кавычках: 名前「old_text」→ 名前「new_text」
    for open_q, close_q in [('「', '」'), ('『', '』')]:
        old_quoted = f"{open_q}{old_text}{close_q}"
        if old_quoted in original_line:
            new_quoted = f"{open_q}{new_text}{close_q}"
            return original_line.replace(old_quoted, new_quoted, 1)

    # 2. Нарратор / обычный текст: old_text есть в строке как есть
    if old_text in original_line:
        return original_line.replace(old_text, new_text, 1)

    return original_line  # не нашли — не меняем


def inject_kirikiri(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable | None = None,
    backup: bool = True,
    progress_callback: Callable | None = None,
) -> dict:
    """Вставить переводы в .ks файлы."""
    _log("[Kirikiri Inject] Начало вставки", log_callback)

    extract_dir = translated_dir / "extracted"
    trans_files = list(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []
    if not trans_files:
        # BUG FIX: API consistency — возвращаем dict как другие injector-ы.
        _log("[Kirikiri Inject] Файлы с переводом не найдены. Сначала Extract → Translate.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # {ks_relative_path: {line_number: (old_text, new_text)}}
    translations: dict[str, dict[int, tuple[str, str]]] = {}

    for tf in trans_files:
        try:
            entries = json.loads(tf.read_text(encoding="utf-8"))
        except Exception as e:
            _log(f"  ✗ Ошибка чтения {tf.name}: {e}", log_callback)
            continue
        for entry in entries:
            if entry.get("game_type") != "kirikiri":
                continue
            t = entry.get("translated", "").strip()
            if not t:
                continue
            # BUG FIX: "file" now holds the safe JSON name (e.g. "scenario_scene01.json").
            # The actual .ks path is stored in "_source_ks" added by the extractor.
            ks_rel = entry.get("_source_ks", "") or entry.get("file", "")
            line_no = entry.get("path", "")
            orig    = entry.get("original", "")
            if not ks_rel or not line_no or not orig:
                continue
            try:
                lineno = int(line_no)
            except ValueError:
                continue
            translations.setdefault(ks_rel, {})[lineno] = (orig, t)

    if not translations:
        _log("[Kirikiri Inject] Нет строк с переводом.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # ── Идемпотентность ─────────────────────────────────────────
    _kk_backup_dir = game_dir / "_backup_kirikiri_original"
    _kk_marker = _kk_backup_dir / ".rpgmtl_injected"
    if _kk_marker.exists():
        _log("[Kirikiri Inject] Маркер предыдущей инъекции. Восстанавливаем оригиналы...", log_callback)
        # Поддерживаем оба формата бэкапа:
        #  • Старый (плоский): все .ks в корне backup_dir с _ вместо /
        #  • Новый (зеркало): backup_dir/<оригинальная структура>
        # Определяем формат по наличию поддиректорий.
        has_subdirs = any(f.parent != _kk_backup_dir for f in _kk_backup_dir.rglob("*.ks"))
        if has_subdirs:
            # Новый зеркальный формат — восстанавливаем все файлы
            for bf in _kk_backup_dir.rglob("*"):
                if not bf.is_file() or bf.name.startswith(".rpgmtl"):
                    continue
                try:
                    rel = bf.relative_to(_kk_backup_dir)
                    dest = game_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(bf, dest)
                except Exception as e:
                    _log(f"  ✗ Восстановление {bf.name}: {e}", log_callback)
        else:
            # Старый плоский формат — только файлы из текущих переводов
            safe_to_rel = {
                ks_rel.replace("/", "_").replace("\\", "_"): ks_rel
                for ks_rel in translations
            }
            for bf in _kk_backup_dir.glob("*.ks"):
                ks_rel = safe_to_rel.get(bf.name)
                if ks_rel:
                    try:
                        dest = game_dir / ks_rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(bf, dest)
                    except Exception as e:
                        _log(f"  ✗ Восстановление {bf.name}: {e}", log_callback)
        try:
            _kk_marker.unlink(missing_ok=True)
        except Exception:
            pass

    # Бэкап — используем зеркальную структуру (новый формат)
    if backup:
        backup_dir = game_dir / "_backup_kirikiri_original"
        if not backup_dir.exists():
            backup_dir.mkdir(parents=True)
            for ks_rel in translations:
                src = game_dir / ks_rel
                if src.exists():
                    dest = backup_dir / ks_rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dest)
                    except (OSError, shutil.Error):
                        pass
            _log(f"[Kirikiri Inject] Бэкап создан: {backup_dir}", log_callback)

    files_modified = 0
    total_injected = 0
    total_errors   = 0

    items = list(translations.items())
    for file_idx, (ks_rel, line_map) in enumerate(items):
        ks_path = game_dir / ks_rel
        if not ks_path.exists():
            _log(f"  Файл не найден: {ks_rel}", log_callback)
            total_errors += 1
            continue

        # Читаем в исходной кодировке
        for enc in ('utf-8-sig', 'utf-8', 'shift_jis', 'cp932'):
            try:
                content = ks_path.read_text(encoding=enc, errors='strict')
                used_enc = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            content  = ks_path.read_text(encoding='cp932', errors='replace')
            used_enc = 'cp932'

        lines   = content.splitlines(keepends=True)
        changed = False
        file_ok = 0
        file_err = 0

        for lineno, (old_text, new_text) in line_map.items():
            idx = lineno - 1
            if idx < 0 or idx >= len(lines):
                file_err += 1
                continue
            new_line = _replace_text_in_line(lines[idx].rstrip('\r\n'), old_text, new_text)
            # Восстанавливаем перевод строки
            eol = lines[idx][len(lines[idx].rstrip('\r\n')):]
            if new_line != lines[idx].rstrip('\r\n'):
                lines[idx] = new_line + eol
                changed = True
                file_ok += 1
            else:
                file_err += 1

        if changed:
            try:
                # BUG FIX: атомарная запись — предотвращает повреждение .ks файла при сбое.
                # BUG FIX #2: write_enc не был присвоен — NameError при записи.
                write_enc = used_enc if used_enc in ("utf-8-sig", "cp932", "shift_jis") else "utf-8"
                joined = "".join(lines)
                # BUG FIX (найдено при углублённом аудите Kirikiri): errors="replace"
                # молча превращал в "?" любой символ, не представимый в исходной
                # кодировке файла. Базовые кириллические буквы cp932/shift_jis
                # ВООБЩЕ-ТО умеют кодировать (расширенный блок) — но обычная
                # русская типографика (тире "—", кавычки-«ёлочки») в них НЕ
                # входит и ТИХО превращалась в "?" при каждой инъекции перевода
                # с таким текстом (а ИИ-перевод на русский естественно ставит
                # именно такие символы). Проверяем, что перевод переживает
                # round-trip через исходную кодировку без потерь; если нет —
                # переключаемся на utf-8-sig (BOM), который extractor уже
                # распознаёт и пробует ПЕРВЫМ (см. комментарий "Kirikiri файлы
                # часто в Shift-JIS или UTF-8 с BOM" при чтении) — то есть это
                # не изобретённый, а уже ожидаемый в этой экосистеме формат.
                if write_enc != "utf-8-sig":
                    try:
                        lossless = joined.encode(write_enc).decode(write_enc) == joined
                    except UnicodeEncodeError:
                        lossless = False
                    if not lossless:
                        _log(
                            f"  ⚠ {ks_rel}: перевод содержит символы, непредставимые в "
                            f"исходной кодировке ({write_enc}) — файл сохранён как "
                            f"UTF-8 с BOM вместо потери символов (Kirikiri поддерживает "
                            f"этот формат).",
                            log_callback,
                        )
                        write_enc = "utf-8-sig"
                encoded = joined.encode(write_enc, errors="replace")
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=ks_path.parent, suffix=".tmp", prefix=".rpgmtl_"
                )
                try:
                    with os.fdopen(tmp_fd, "wb") as f:
                        f.write(encoded)
                    os.replace(tmp_path, ks_path)
                except Exception:
                    try: os.unlink(tmp_path)
                    except Exception: pass
                    raise
                files_modified += 1
                _log(f"  ✓ {ks_rel}: вставлено={file_ok}, ошибок={file_err}", log_callback)
            except Exception as e:
                _log(f"  ✗ Ошибка записи {ks_rel}: {e}", log_callback)
                total_errors += 1
        else:
            _log(f"  — {ks_rel}: нет изменений (ошибок={file_err})", log_callback)

        total_injected += file_ok
        total_errors   += file_err

        if progress_callback:
            progress_callback(file_idx + 1, len(items), -1.0, ks_rel)

    _log(
        f"[Kirikiri Inject] ✓ файлов={files_modified}, "
        f"строк={total_injected}, ошибок={total_errors}",
        log_callback,
    )
    if files_modified > 0:
        try:
            _kk_marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_kk_marker, "injected")
        except Exception as e:
            # BUG FIX: сбой записи маркера идемпотентности раньше проходил
            # немо — сам перевод уже вставлен успешно, но на следующем
            # запуске без маркера инструмент может не понять, что инъекция
            # уже была, и повторно попытаться (см. те же паттерны в других
            # движках — фикс единый для всех).
            _log(f"  ⚠ Не удалось записать маркер инъекции: {e}", log_callback)
    return {
        "files_modified": files_modified,
        "total_injected": total_injected,
        "total_errors":   total_errors,
    }
