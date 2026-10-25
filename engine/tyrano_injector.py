"""
tyrano_injector.py — вставка переводов в TyranoScript (.ks файлы).
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from engine.file_utils import atomic_write_text

# KAG tag pattern: [tag attr="val"] — compiled once at module load
_TAG_RE_INJ = re.compile(r"\[[^\]]*\]")

def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)

def inject_tyrano(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable | None = None,
    backup: bool = True,
    progress_callback: Callable | None = None,
) -> dict:
    _log("[Tyrano Inject] Начало вставки переводов", log_callback)

    extract_dir = translated_dir / "extracted"
    trans_files = list(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []
    if not trans_files:
        # BUG FIX: API consistency
        _log("[Tyrano Inject] Файлы с переводом не найдены. Сначала Extract → Translate.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # BUG FIX: hardcoded data/scenario — теперь те же 3 пути что и в extractor
    _SCENARIO_CANDIDATES = [
        game_dir / "data" / "scenario",
        game_dir / "tyrano" / "scenario",
        game_dir / "scenario",
    ]
    scenario_dir = next((p for p in _SCENARIO_CANDIDATES if p.is_dir()), None)
    if scenario_dir is None:
        msg = f"[Tyrano Inject] Папка scenario не найдена в {game_dir}"
        _log(msg, log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # ── Идемпотентность ──────────────────────────────────────
    _tyrano_backup = game_dir / "_backup_tyrano_original"
    _tyrano_marker = _tyrano_backup / ".rpgmtl_injected"
    if _tyrano_marker.exists():
        _log("[Tyrano Inject] Маркер предыдущей инъекции. Восстанавливаем оригиналы...", log_callback)
        for bf in _tyrano_backup.rglob("*"):
            if bf.name.startswith(".rpgmtl"):
                continue
            if bf.is_file():
                try:
                    rel = bf.relative_to(_tyrano_backup)
                    dest = scenario_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(bf, dest)
                except Exception as e:
                    _log(f"  ✗ {e}", log_callback)
        # BUG FIX: remove marker after restore so next Inject doesn't redundantly
        # re-restore from backup before injecting.
        try:
            _tyrano_marker.unlink(missing_ok=True)
        except Exception:
            pass

    # Бэкап
    if backup:
        backup_dir = game_dir / "_backup_tyrano_original"
        if not backup_dir.exists():
            backup_dir.mkdir(parents=True)
            for f in scenario_dir.rglob("*.ks"):
                rel = f.relative_to(scenario_dir)
                dest = backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(f, dest)
                except (OSError, shutil.Error):
                    pass
            _log(f"[Tyrano Inject] Бэкап создан: {backup_dir}", log_callback)

    # Группируем по .ks файлу
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    skipped_bad_path: list[tuple[str, str]] = []
    for tf in trans_files:
        try:
            entries = json.loads(tf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for entry in entries:
            if entry.get("game_type") != "tyrano":
                continue
            t = entry.get("translated", "").strip()
            if not t:
                continue

            ks_name = entry.get("_source_ks", "") or entry.get("file", "").replace(".json", ".ks")
            path_str = entry.get("path", "")
            orig = entry.get("original", "")
            full_line = entry.get("full_line", "")  # BUG FIX: was "_full_line" (filtered)
            if ks_name and path_str.startswith("line:"):
                try:
                    lineno = int(path_str.split(":")[1])
                    by_file.setdefault(ks_name, []).append((lineno, orig, full_line, t))
                except ValueError:
                    # BUG FIX: раньше запись с повреждённым/неожиданным
                    # path ("line:XYZ" вместо "line:5") молча терялась —
                    # перевод для этой строки просто никогда не
                    # инжектился, без единого сообщения об этом.
                    skipped_bad_path.append((ks_name, path_str))

    files_modified = 0
    total_injected = 0
    total_errors = 0

    items = list(by_file.items())

    for idx, (ks_name, translations) in enumerate(items):
        ks_path = scenario_dir / ks_name
        if not ks_path.exists():
            _log(f"  ✗ Файл не найден: {ks_name}", log_callback)
            total_errors += len(translations)
            continue

        try:
            # BUG FIX (тот же класс, что в Kirikiri/extractor выше): жёсткое
            # чтение как UTF-8 не читает shift_jis/cp932 TyranoBuilder-
            # проекты (часто конвертированные из Kirikiri, тот же .ks-
            # синтаксис) — определяем кодировку так же, как extractor.
            for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
                try:
                    content = ks_path.read_text(encoding=enc, errors="strict")
                    used_enc = enc
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                content = ks_path.read_text(encoding="utf-8", errors="replace")
                used_enc = "utf-8"
            lines = content.splitlines(keepends=True)
        except Exception as e:
            _log(f"  ✗ Ошибка чтения {ks_name}: {e}", log_callback)
            total_errors += len(translations)
            continue

        file_ok = 0
        file_err = 0

        for lineno, orig, full_line, translated in translations:
            if lineno >= len(lines):
                file_err += 1
                continue

            eol = "\n" if lines[lineno].endswith("\n") else ""

            # BUG FIX: injection only ever consulted full_line/orig (snapshots
            # captured at Extract time) to rebuild the line — it never checked
            # whether the .ks file's CURRENT line still matches that snapshot.
            # If the file changed between Extract and Inject, a translation of
            # the OLD text was written over whatever is there now, silently
            # (same class of bug as _set_nested in engine/injector.py).
            # tyrano_extractor.py builds full_line/original from line.strip()
            # (see extract_tyrano) — mirror that here so EOL/whitespace don't
            # cause false mismatches.
            expected = (full_line or orig)
            if expected and lines[lineno].strip() != expected:
                file_err += 1
                continue

            if full_line:
                # Inline KAG tags must not be moved around by a translation.
                # Prefix/suffix tags are safe to rebuild; tags embedded inside
                # the original text are unsafe unless the translated text kept
                # the exact tag tokens. Otherwise skip instead of risking a
                # different command order or broken scene behaviour.
                tag_matches = list(_TAG_RE_INJ.finditer(full_line))
                if tag_matches:
                    prefix_count = 0
                    pos = 0
                    while prefix_count < len(tag_matches) and tag_matches[prefix_count].start() == pos:
                        pos = tag_matches[prefix_count].end()
                        prefix_count += 1
                    suffix_start = len(tag_matches)
                    end_pos = len(full_line)
                    while suffix_start > prefix_count and tag_matches[suffix_start - 1].end() == end_pos:
                        end_pos = tag_matches[suffix_start - 1].start()
                        suffix_start -= 1
                    interior = [m.group() for m in tag_matches[prefix_count:suffix_start]]
                    if interior and any(tag not in translated for tag in interior):
                        file_err += 1
                        _log(
                            f"[Tyrano Inject] ⚠ {ks_name}:{lineno} — inline-теги "
                            "не сохранены в переводе; строка пропущена для защиты игры",
                            log_callback,
                        )
                        continue

                # BUG FIX: восстанавливаем строку из тегов + переведённого текста.
                # full_line = "[tag]оригинал[l][r]"
                # Находим все теги и собираем строку: prefix_tags + translated + suffix_tags
                tag_matches = list(_TAG_RE_INJ.finditer(full_line))
                if tag_matches:
                    # ── prefix: теги в самом начале строки ──
                    pos = 0
                    prefix_parts = []
                    i = 0
                    while i < len(tag_matches) and tag_matches[i].start() == pos:
                        prefix_parts.append(tag_matches[i].group())
                        pos = tag_matches[i].end()
                        i += 1
                    # ── suffix: теги в самом конце строки ──
                    j = len(tag_matches) - 1
                    end_pos = len(full_line)
                    suffix_list = []
                    while j >= i and tag_matches[j].end() == end_pos:
                        suffix_list.insert(0, tag_matches[j].group())
                        end_pos = tag_matches[j].start()
                        j -= 1
                    # ── middle: теги между текстом (ruby, color, и т.п.) ──
                    # Позиционная вставка в переведённый текст невозможна —
                    # переносим теги В КОНЕЦ текста перед suffix-тегами.
                    # Это сохраняет сброс цвета, паузы и т.д. без потери тегов.
                    middle = "".join(tag_matches[k].group() for k in range(i, j + 1))
                    prefix = "".join(prefix_parts)
                    suffix = "".join(suffix_list)
                    new_line = prefix + translated + middle + suffix + eol
                else:
                    new_line = translated + eol
            else:
                # Старый формат (original = полная строка)
                new_line = translated + eol

            lines[lineno] = new_line
            file_ok += 1
                
        if file_ok > 0:
            # BUG FIX (тот же класс, что в Kirikiri): пишем в ту же
            # кодировку, что была прочитана, но если перевод не переживает
            # round-trip без потерь (например русская типографика "—"/«»
            # непредставима в shift_jis/cp932) — переключаемся на UTF-8 с
            # BOM вместо молчаливой потери символов.
            write_enc = used_enc if used_enc in ("utf-8-sig", "cp932", "shift_jis") else "utf-8"
            joined = "".join(lines)
            if write_enc not in ("utf-8", "utf-8-sig"):
                try:
                    lossless = joined.encode(write_enc).decode(write_enc) == joined
                except UnicodeEncodeError:
                    lossless = False
                if not lossless:
                    _log(
                        f"  ⚠ {ks_name}: перевод содержит символы, непредставимые в "
                        f"исходной кодировке ({write_enc}) — файл сохранён как "
                        f"UTF-8 с BOM вместо потери символов.",
                        log_callback,
                    )
                    write_enc = "utf-8-sig"
            tmp_fd, tmp_path = tempfile.mkstemp(dir=ks_path.parent, suffix=".tmp", prefix=".rpgmtl_")
            try:
                with os.fdopen(tmp_fd, "w", encoding=write_enc) as f:
                    f.write(joined)
                os.replace(tmp_path, ks_path)
                files_modified += 1
                _log(f"  ✓ {ks_name}: вставлено {file_ok}", log_callback)
            except Exception as e:
                _log(f"  ✗ Ошибка записи {ks_name}: {e}", log_callback)
                total_errors += file_ok
        
        total_injected += file_ok
        total_errors += file_err
        
        if progress_callback:
            progress_callback(idx + 1, len(items), -1.0, ks_name)

    if skipped_bad_path:
        _log(
            f"[Tyrano Inject] ⚠ Пропущено {len(skipped_bad_path)} строк(и) с "
            f"нераспознанным path (не число после 'line:') — переводы для них не вставлены: "
            f"{skipped_bad_path[:5]}{'...' if len(skipped_bad_path) > 5 else ''}",
            log_callback,
        )
    _log(f"[Tyrano Inject] ✓ Готово: файлов={files_modified}, строк={total_injected}, ошибок={total_errors}", log_callback)
    if files_modified > 0:
        try:
            _tyrano_marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_tyrano_marker, "injected")
        except Exception as e:
            _log(f"  ⚠ Не удалось записать маркер инъекции: {e}", log_callback)
    return {
        "files_modified": files_modified,
        "total_injected": total_injected,
        "total_errors": total_errors,
    }

