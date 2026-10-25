"""
wolf_injector.py — вставка переводов в Wolf RPG Editor бинарные файлы.

Стратегия: находим строку по байтовому смещению, заменяем
[4b длина][старый текст] → [4b новая длина][новый текст].

ВАЖНО: если новая строка длиннее оригинала, все последующие смещения
в файле сдвигаются. Поэтому обрабатываем замены в обратном порядке
(от конца к началу), чтобы ранние смещения не сдвигались.
"""

import hashlib
import json
from engine.file_utils import atomic_write_text
import os
import struct
import tempfile
import shutil
from pathlib import Path
from typing import Callable

# Тот же верхний предел, что и MAX_STR_LEN в wolf_extractor.py — используется
# только как sanity-bound в post-write верификации ниже (отсекает заведомо
# мусорную длину при перечитывании файла, не как источник истины для extract).
MAX_STR_LEN = 2048


def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


def _encode_wolf_str(text: str, original_encoding: str = "cp932") -> tuple[bytes, str]:
    """
    Закодировать строку в байты Wolf RPG.
    Возвращает (bytes, encoding_used).

    Примечание: CP932 (Windows-31J) включает кириллицу через NEC-расширения
    (ряды 0x83-0x86), поэтому русский текст корректно кодируется и отображается
    если в игре используется шрифт с кириллическим начертанием.
    """
    for enc in (original_encoding, 'cp932', 'utf-8', 'shift_jis'):
        try:
            return text.encode(enc), enc
        except (UnicodeEncodeError, LookupError):
            continue
    return text.encode('utf-8', errors='replace'), 'utf-8'


def _patch_file(data: bytearray, offset: int,
                original: str, translated: str,
                encoding: str = "cp932") -> tuple[bytearray, int, str]:
    """
    Заменить строку в bytearray по смещению.
    Возвращает (новый bytearray, разница в байтах, использованная кодировка).
    Если новый bytearray is data — патч не применён.
    """
    if offset < 0 or offset + 4 > len(data):
        return data, 0, encoding

    # Читаем текущую длину
    orig_len = struct.unpack_from('<I', data, offset)[0]
    if orig_len > 65536 or offset + 4 + orig_len > len(data):
        return data, 0, encoding

    # Проверяем что это действительно нужная строка
    raw_orig = data[offset + 4: offset + 4 + orig_len]
    for enc in ('utf-8', 'cp932', 'shift_jis'):
        try:
            current = raw_orig.decode(enc)
            if current == original:
                break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return data, 0, encoding  # строка не совпадает

    # Кодируем новый текст
    new_raw, enc_used = _encode_wolf_str(translated, encoding)
    new_len = len(new_raw)

    # Собираем новый блок: [4b длина][bytes]
    new_block = struct.pack('<I', new_len) + new_raw
    old_block_size = 4 + orig_len

    # Заменяем в bytearray
    result = bytearray()
    result.extend(data[:offset])
    result.extend(new_block)
    result.extend(data[offset + old_block_size:])

    delta = new_len - orig_len
    return result, delta, enc_used


def inject_wolf(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable | None = None,
    backup: bool = True,
    progress_callback: Callable | None = None,
) -> dict:
    """Вставить переводы в Wolf RPG бинарные файлы."""
    _log("[Wolf Inject] Начало вставки переводов", log_callback)

    extract_dir = translated_dir / "extracted"
    trans_files = list(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []
    if not trans_files:
        # BUG FIX: другие injector-ы возвращают пустой dict — не raise.
        # Pipeline ловит exceptions, но pipeline.inject() ожидает dict-результат
        # в ветке wolf — несоответствие API давало невнятную ошибку в логе.
        _log("[Wolf Inject] Файлы с переводом не найдены. Сначала Extract → Translate.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # Группируем по файлу: {rel_path: [(offset, original, translated, encoding)]}
    by_file: dict[str, list[tuple[int, str, str, str]]] = {}

    file_hashes: dict[str, str] = {}
    for tf in trans_files:
        try:
            entries = json.loads(tf.read_text(encoding="utf-8"))
        except Exception as e:
            _log(f"  ✗ {tf.name}: {e}", log_callback)
            continue

        _file_hashes_local: dict[str, str] = {}
        for entry in entries:
            # Сбор хэшей для проверки (один хэш на файл, не на запись)
            if "_file_hash" in entry and "_source_path" in entry:
                file_hashes[entry["_source_path"]] = entry["_file_hash"]
                _file_hashes_local[entry["_source_path"]] = entry["_file_hash"]

        for entry in entries:
            if entry.get("game_type") != "wolf":
                continue
            t = entry.get("translated", "").strip()
            if not t:
                continue
            fname    = entry.get("file", "")
            src_path = entry.get("_source_path", "").strip() or fname
            path_str = entry.get("path", "")
            orig     = entry.get("original", "")
            enc      = entry.get("_encoding", "cp932")
            if not src_path or not path_str or not orig:
                continue
            try:
                offset = int(path_str)
            except ValueError:
                continue
            by_file.setdefault(src_path, []).append((offset, orig, t, enc))

    # Проверка хэшей: одно чтение на файл, не на запись. Если файл изменился —
    # пропускаем только этот файл, не прерываем всю инъекцию.
    skipped_changed: set[str] = set()
    for src_path, expected_hash in file_hashes.items():
        game_file = game_dir / src_path
        if not game_file.exists():
            continue
        current_hash = hashlib.sha256(game_file.read_bytes()).hexdigest()
        if current_hash != expected_hash:
            _log(
                f"  ✗ {src_path}: файл изменён после Extract (хэши не совпадают). "
                f"Файл пропущен. Сделайте Extract заново и повторите Inject.",
                log_callback,
            )
            skipped_changed.add(src_path)
    for sp in skipped_changed:
        by_file.pop(sp, None)

    if not by_file:
        _log("[Wolf Inject] Нет строк с переводом.", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    # ── Идемпотентность ────────────────────────────────────────
    _backup_dir_wolf = game_dir / "_backup_wolf_original"
    _wolf_marker = _backup_dir_wolf / ".rpgmtl_injected"
    if _wolf_marker.exists():
        _log("[Wolf Inject] Маркер предыдущей инъекции. Восстанавливаем оригиналы...", log_callback)
        has_subdirs = any(
            f.parent != _backup_dir_wolf
            for f in _backup_dir_wolf.rglob("*")
            if f.is_file() and not f.name.startswith(".rpgmtl")
        )
        restored = 0
        if has_subdirs:
            # Новый зеркальный формат
            for bf in _backup_dir_wolf.rglob("*"):
                if not bf.is_file() or bf.name.startswith(".rpgmtl"):
                    continue
                try:
                    rel = bf.relative_to(_backup_dir_wolf)
                    dest = game_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(bf, dest)
                    restored += 1
                except Exception as e:
                    _log(f"  ✗ Восстановление {bf.name}: {e}", log_callback)
        else:
            # Старый плоский формат — только по текущему by_file
            for rel_path in by_file:
                safe = rel_path.replace("/", "_").replace("\\", "_")
                bf = _backup_dir_wolf / safe
                if bf.exists():
                    try:
                        shutil.copy2(bf, game_dir / rel_path)
                        restored += 1
                    except Exception as e:
                        _log(f"  ✗ Восстановление {safe}: {e}", log_callback)
        if restored:
            _log(f"[Wolf Inject] Оригиналы восстановлены: {restored} файлов.", log_callback)
        try:
            _wolf_marker.unlink(missing_ok=True)
        except Exception:
            pass

    # Создаём бэкап — зеркальная структура (новый формат)
    if backup:
        backup_dir = game_dir / "_backup_wolf_original"
        if not backup_dir.exists():
            backup_dir.mkdir(parents=True)
            for rel_path in by_file:
                src = game_dir / rel_path
                if src.exists():
                    dest = backup_dir / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dest)
                    except (OSError, shutil.Error):
                        pass
            _log(f"[Wolf Inject] Бэкап создан: {backup_dir}", log_callback)

    files_modified = 0
    total_injected = 0
    total_errors   = 0

    items = list(by_file.items())
    for file_idx, (rel_path, translations) in enumerate(items):
        game_file = game_dir / rel_path
        if not game_file.exists():
            _log(f"  Файл не найден: {rel_path}", log_callback)
            total_errors += 1
            continue

        try:
            data = bytearray(game_file.read_bytes())
        except Exception as e:
            _log(f"  ✗ Ошибка чтения {rel_path}: {e}", log_callback)
            total_errors += 1
            continue

        # Сортируем по смещению в обратном порядке — от конца к началу
        # Это критично: замены в конце не сдвигают смещения в начале
        sorted_trans = sorted(translations, key=lambda x: x[0], reverse=True)

        file_ok  = 0
        file_err = 0
        applied_checks: list[tuple[int, str]] = []  # (offset, expected_translated) для post-write проверки

        utf8_fallback_warned = False
        for offset, original, translated, enc in sorted_trans:
            new_data, delta, enc_used = _patch_file(data, offset, original, translated, enc)
            if new_data is not data:
                data = new_data
                file_ok += 1
                applied_checks.append((offset, translated))
                # Предупреждаем один раз на файл, если перевод сохранён в UTF-8
                # вместо исходной CP932 — Wolf RPG Editor может не отобразить текст.
                if enc_used == 'utf-8' and enc not in ('utf-8',) and not utf8_fallback_warned:
                    _log(
                        f"  ⚠ {rel_path}: кириллица закодирована как UTF-8 "
                        f"(исходная кодировка {enc} не поддерживает этот текст). "
                        f"Wolf RPG Editor может отображать '?' вместо букв.",
                        log_callback,
                    )
                    utf8_fallback_warned = True
            else:
                file_err += 1
                _log(f"    ⚠ offset={offset}: строка не найдена или не совпадает", log_callback)

        if file_ok > 0:
            try:
                # BUG FIX: прямой write_bytes обрезает файл при сбое питания/диска.
                # Атомарная замена: пишем во временный файл, затем rename.
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=game_file.parent, suffix=".tmp", prefix=".rpgmtl_"
                )
                try:
                    with os.fdopen(tmp_fd, "wb") as f:
                        f.write(bytes(data))
                    os.replace(tmp_path, game_file)
                except Exception:
                    try: os.unlink(tmp_path)
                    except Exception: pass
                    raise
                files_modified += 1
                _log(f"  ✓ {rel_path}: вставлено={file_ok}, ошибок={file_err}", log_callback)

                # Post-inject проверка: смещения при обратном порядке патчинга
                # (см. комментарий у sorted_trans выше) не сдвигаются друг
                # относительно друга, поэтому можно честно проверить, что на
                # диске по каждому применённому offset реально лежит перевод,
                # который мы хотели записать — а не только "запись не упала".
                # Ловит расхождение в самой логике патчинга (напр. будущая
                # регрессия), best-effort — сама инъекция уже совершена.
                try:
                    on_disk = game_file.read_bytes()
                    verify_failed = 0
                    for off, expected_text in applied_checks:
                        ok_slot = False
                        if off + 4 <= len(on_disk):
                            ln = struct.unpack_from('<I', on_disk, off)[0]
                            if 0 <= ln <= MAX_STR_LEN and off + 4 + ln <= len(on_disk):
                                raw = on_disk[off + 4: off + 4 + ln]
                                for enc_try in ('utf-8', 'cp932', 'shift_jis'):
                                    try:
                                        if raw.decode(enc_try) == expected_text:
                                            ok_slot = True
                                            break
                                    except (UnicodeDecodeError, LookupError):
                                        continue
                        if not ok_slot:
                            verify_failed += 1
                    if verify_failed:
                        _log(
                            f"  ⚠ {rel_path}: post-write проверка НЕ подтвердила {verify_failed} "
                            f"из {len(applied_checks)} записанных строк — возможна порча файла. "
                            f"Бэкап оригинала: {game_dir / '_backup_wolf_original' / rel_path}",
                            log_callback,
                        )
                except Exception as e:
                    # Верификация — best-effort, не должна ронять инъекцию (перевод
                    # уже сохранён), но сбой самой проверки не должен быть немым —
                    # иначе баг в ней (как NameError на MAX_STR_LEN) невозможно заметить.
                    _log(f"    (post-write проверка не выполнена: {e})", log_callback)
            except Exception as e:
                _log(f"  ✗ Ошибка записи {rel_path}: {e}", log_callback)
                total_errors += 1
        else:
            _log(f"  — {rel_path}: нет изменений", log_callback)

        total_injected += file_ok
        total_errors   += file_err

        if progress_callback:
            progress_callback(file_idx + 1, len(items), -1.0, game_file.name)

    _log(
        f"[Wolf Inject] ✓ файлов={files_modified}, "
        f"строк={total_injected}, ошибок={total_errors}",
        log_callback,
    )
    if files_modified > 0:
        try:
            _wolf_marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_wolf_marker, "injected")
        except Exception as e:
            _log(f"  ⚠ Не удалось записать маркер инъекции: {e}", log_callback)
    return {
        "files_modified": files_modified,
        "total_injected": total_injected,
        "total_errors":   total_errors,
    }
