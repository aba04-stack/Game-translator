"""
renpy_injector.py — вставка переводов обратно в .rpy файлы Ren'Py.
Использует line_start из extracted JSON для точной замены строк.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from engine.file_utils import atomic_write_text


def _log(msg: str, cb: Callable[[str], None] | None = None) -> None:
    print(msg, flush=True)
    if cb:
        cb(msg)


def _requote(q: str, text: str) -> str:
    """Обернуть text в кавычки q, экранировав нужное.

    Порядок важен: сначала дублируем обратные слеши, затем кавычку.
    Это симметрично _unquote (который сначала раскрывает \\\\, потом \\n).
    """
    # 1. \\ → \\\\ (must be first, otherwise we'd double-escape new \\ we create)
    escaped = text.replace("\\", "\\\\")
    # 2. Escape the quote char that wraps the string
    escaped = escaped.replace(q, f"\\{q}")
    # 3. Actual newlines and tabs → escape sequences
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f"{q}{escaped}{q}"


def _quote_pattern(q: str) -> re.Pattern:
    return re.compile(f"{re.escape(q)}(?:[^{re.escape(q)}\\\\]|\\\\.)*{re.escape(q)}")


def _find_quoted(line: str, q: str) -> list:
    """Все непересекающиеся строки в кавычках q в line (список match-объектов)."""
    return list(_quote_pattern(q).finditer(line))


def inject_renpy(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    backup: bool = True,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
) -> dict:
    """
    Вставить переводы из translated_dir обратно в .rpy файлы.

    translated_dir — папка extracted/, содержащая *_translated.json.
    Каждый entry имеет поля: file (относит. путь), line_start, original,
    translated, quote_char.
    """
    game_dir = Path(game_dir)
    translated_dir = Path(translated_dir)

    # Папка с .rpy файлами
    game_sub = game_dir / "game"
    rpy_base = game_sub if game_sub.is_dir() else game_dir

    # ── Идемпотентность ────────────────────────────────────────
    # BUG FIX: без этой проверки повторный inject применял переводы
    # поверх уже переведённых .rpy файлов, дублируя текст.
    injection_marker = game_dir / "_backup_renpy_original" / ".rpgmtl_injected"
    if injection_marker.exists():
        backup_dir = game_dir / "_backup_renpy_original"
        _log("[RenPy Inject] Обнаружен маркер предыдущей инъекции. Восстанавливаем оригиналы...", log_callback)
        restored = 0
        for bf in backup_dir.rglob("*.rpy"):
            if ".rpgmtl" in bf.name:
                continue
            try:
                rel = bf.relative_to(backup_dir)
                dest = rpy_base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bf, dest)
                restored += 1
            except Exception as e:
                _log(f"  ✗ Восстановление {bf.name}: {e}", log_callback)
        _log(f"[RenPy Inject] Оригиналы восстановлены: {restored} файлов.", log_callback)
        # BUG FIX: remove marker after restore so next Inject doesn't redundantly re-restore
        try:
            injection_marker.unlink(missing_ok=True)
        except Exception:
            pass

    # ── Бэкап ──────────────────────────────────────────────────
    if backup:
        backup_dir = game_dir / "_backup_renpy_original"
        if not backup_dir.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            # BUG FIX: was rglob("*.rpy") which included files inside _backup_* dirs,
            # causing infinite recursive backup copies and OSError: File name too long.
            # Now explicitly skip any directory starting with "_backup".
            for rpy in rpy_base.rglob("*.rpy"):
                if any(part.startswith("_backup") for part in rpy.parts):
                    continue
                try:
                    rel = rpy.relative_to(rpy_base)
                except ValueError:
                    rel = Path(rpy.name)
                dest = backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rpy, dest)
            _log(f"[RenPy Inject] Бэкап создан: {backup_dir}", log_callback)

    # ── Загрузка переведённых файлов ───────────────────────────
    # BUG FIX: was looking in translated_dir root, but apply_dedup_to_files
    # writes to translated_dir/extracted/ — match Wolf/Kirikiri convention.
    extract_dir = translated_dir / "extracted"
    trans_files = list(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []
    if not trans_files:
        # fallback: check root (for manually-placed files)
        trans_files = list(translated_dir.glob("*_translated.json"))
    if not trans_files:
        _log("[RenPy Inject] ✗ Нет файлов с переводом", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_skipped": 0, "total_errors": 0}

    # Группируем записи по исходному .rpy файлу
    # Ключ: fname (относительный путь к .rpy от rpy_base)
    rpy_edits: dict[str, list[dict]] = {}

    for trans_file in trans_files:
        try:
            with open(trans_file, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception as e:
            _log(f"[RenPy Inject] ✗ Не удалось прочитать {trans_file.name}: {e}", log_callback)
            continue

        for entry in entries:
            if not entry.get("translated"):
                continue
            # BUG FIX: "file" now stores the .json output name (e.g. "script.json"),
            # not the original .rpy path. Use "_source_rpy" added by the extractor.
            fname = entry.get("_source_rpy", "") or entry.get("file", "")
            if not fname:
                continue
            rpy_edits.setdefault(fname, []).append(entry)

    if not rpy_edits:
        _log("[RenPy Inject] Нет переведённых строк для вставки.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_skipped": 0, "total_errors": 0}

    total_injected = 0
    total_skipped = 0
    total_errors = 0
    files_modified = 0

    for file_idx, (fname, edits) in enumerate(sorted(rpy_edits.items())):
        if progress_callback:
            progress_callback(file_idx + 1, len(rpy_edits), -1.0, fname)

        rpy_path = rpy_base / fname
        if not rpy_path.exists():
            _log(f"[RenPy Inject] ✗ Файл не найден: {fname}", log_callback)
            total_errors += len(edits)
            continue

        try:
            raw = rpy_path.read_text(encoding="utf-8", errors="replace")
            lines = raw.splitlines(keepends=True)
        except Exception as e:
            _log(f"[RenPy Inject] ✗ Ошибка чтения {fname}: {e}", log_callback)
            total_errors += len(edits)
            continue

        # Сортируем по убыванию номера строки — замена снизу вверх
        # чтобы вставка/удаление строк не сдвигала номера
        edits_sorted = sorted(edits, key=lambda e: e.get("line_start", 0), reverse=True)

        file_injected = 0
        file_skipped = 0
        file_errors = 0

        for entry in edits_sorted:
            lineno: int = entry.get("line_start", 0)
            original: str = entry.get("original", "")
            translated: str = entry.get("translated", "")
            q: str = entry.get("quote_char", '"')

            if not translated.strip():
                file_skipped += 1
                continue

            if lineno < 0 or lineno >= len(lines):
                _log(f"[RenPy Inject] ⚠ {fname}:{lineno} — строка вне диапазона", log_callback)
                file_errors += 1
                continue

            raw_line = lines[lineno]

            # Пробуем точное совпадение оригинала
            orig_quoted = _requote(q, original)
            if orig_quoted in raw_line:
                trans_quoted = _requote(q, translated)
                lines[lineno] = raw_line.replace(orig_quoted, trans_quoted, 1)
                file_injected += 1
                continue

            # Точный литерал не совпал — строка могла измениться после
            # Extract (разработчик игры её отредактировал). Слепая замена
            # "первой попавшейся" строки в кавычках небезопасна, когда на
            # строке НЕСКОЛЬКО кандидатов: можно попасть в чужой текст и
            # сломать логику/сценарий — тогда пропускаем, как раньше.
            # Но если кандидат РОВНО ОДИН, позиция однозначна (меняется
            # только контент реплики, а не выбор — какую из нескольких
            # строк в кавычках трогать). Это safe fuzzy candidate:
            # заменяем, но обязательно и заметно логируем как best-effort,
            # а не молча выдаём за подтверждённое точное совпадение.
            candidates = _find_quoted(raw_line, q)
            if len(candidates) == 1:
                m = candidates[0]
                trans_quoted = _requote(q, translated)
                lines[lineno] = raw_line[:m.start()] + trans_quoted + raw_line[m.end():]
                file_injected += 1
                _log(
                    f"[RenPy Inject] ⚠ {fname}:{lineno} — best-effort: точный исходный "
                    f"текст не совпал (строка изменилась после Extract), но найден "
                    f"единственный кандидат в кавычках — заменён без гарантии. "
                    f"Проверьте результат вручную.",
                    log_callback,
                )
                continue

            _log(
                f"[RenPy Inject] ⚠ {fname}:{lineno} — точный исходный текст "
                f"не совпал; строка пропущена для защиты игры",
                log_callback,
            )
            file_skipped += 1

        # Записываем файл только если были изменения
        # BUG FIX: раньше файл всегда перезаписывался, files_modified некорректно рос
        if file_injected > 0:
            try:
                # BUG FIX: атомарная запись через temp-файл.
                # Прямой write_text при сбое оставлял .rpy с нулями — игра ломалась.
                content = "".join(lines).encode("utf-8")
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=rpy_path.parent, suffix=".tmp", prefix=".rpgmtl_"
                )
                try:
                    with os.fdopen(tmp_fd, "wb") as f:
                        f.write(content)
                    os.replace(tmp_path, rpy_path)
                except Exception:
                    try: os.unlink(tmp_path)
                    except Exception: pass
                    raise
                files_modified += 1
                _log(
                    f"[RenPy Inject] ✓ {fname}: "
                    f"вставлено={file_injected}, пропущено={file_skipped}, ошибок={file_errors}",
                    log_callback,
                )
            except Exception as e:
                _log(f"[RenPy Inject] ✗ Ошибка записи {fname}: {e}", log_callback)
                file_errors += file_injected + file_skipped
                file_injected = 0
        else:
            _log(
                f"[RenPy Inject] — {fname}: нет вставок "
                f"(пропущено={file_skipped}, ошибок={file_errors})",
                log_callback,
            )

        total_injected += file_injected
        total_skipped += file_skipped
        total_errors += file_errors

    _log(
        f"[RenPy Inject] ✓ Готово: файлов изменено={files_modified}, "
        f"строк вставлено={total_injected}, пропущено={total_skipped}, ошибок={total_errors}",
        log_callback,
    )
    if files_modified > 0:
        try:
            marker = game_dir / "_backup_renpy_original" / ".rpgmtl_injected"
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(marker, "injected")
        except Exception:
            pass
    return {
        "files_modified": files_modified,
        "total_injected": total_injected,
        "total_skipped": total_skipped,
        "total_errors": total_errors,
    }
