"""
unity_injector.py — безопасная вставка переводов обратно в Unity текстовые файлы.

Поддерживает те же текстовые типы, что и extractor:
- JSON
- CSV / TSV
- XML — только текст между тегами (structure-aware, не построчно)
- PO/gettext — только значение msgstr, msgid никогда не трогается
- YAML — только значение простой плоской пары "ключ: значение"
- TXT / MD — построчный текст (нет структуры для сохранения)
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import tempfile
import xml.sax.saxutils as saxutils
from pathlib import Path
from typing import Any, Callable

from engine.file_utils import atomic_write_text


def _log(msg: str, cb: Callable[[str], None] | None = None) -> None:
    print(msg, flush=True)
    if cb:
        cb(msg)


def _load_json(path: Path) -> object:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return json.load(f)


def _write_json(path: Path, data: object) -> None:
    """Атомарная запись JSON через temp-файл.

    BUG FIX: прямой open(path, 'w') при прерывании оставлял пустой файл.
    Теперь пишем во temp, затем os.replace (атомарно).
    """
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".rpgmtl_")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _load_extract_index(extract_dir: Path) -> dict[str, str]:
    """Загрузить карту safe_name -> source_path из extract_index.json."""
    index_path = extract_dir.parent / "extract_index.json"
    if not index_path.exists():
        return {}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sources = data.get("sources", {})
        if isinstance(sources, dict):
            return {str(k): str(v) for k, v in sources.items() if v}
    except Exception:
        pass
    return {}


def _resolve_source_path(trans_file: Path, entries: list[dict], source_map: dict[str, str]) -> str:
    """Попробовать восстановить исходный путь файла для вставки."""
    # BUG FIX: "source_patch" was a typo / unintentional duplicate of "source_path".
    # Removed to avoid matching a field that was never written by the extractor.
    for key in ("source_path", "sourcepath", "sourcePath", "SourcePath"):
        val = entries[0].get(key, "") if entries else ""
        if isinstance(val, str) and val.strip():
            return val.strip()

    stem = trans_file.name
    if stem.endswith("_translated.json"):
        safe_name = stem[:-len("_translated.json")]
    elif stem.endswith(".json"):
        safe_name = stem[:-len(".json")]
    else:
        safe_name = trans_file.stem

    # BUG FIX: try both safe_name and safe_name+".json" since index keys changed
    for key in (safe_name, safe_name + ".json"):
        if key in source_map and source_map[key].strip():
            return source_map[key].strip()

    # Последний шанс: иногда safe_name уже совпадает с относительным путём без расширения.
    # Это лучше, чем падать сразу — вызов ниже всё равно проверит существование файла.
    return safe_name


def _guess_original_path(game_dir: Path, source_path: str) -> Path | None:
    """Найти исходный файл даже если путь сохранён относительно подпапки data/www/data."""
    rel = Path(str(source_path).replace("\\", "/").lstrip("/"))
    candidates: list[Path] = []

    def add_candidate(path: Path) -> None:
        try:
            if not path.exists() or not path.is_file():
                return
            # Backup files are not game resources. Including them in the
            # recursive fallback makes a legitimate second Inject look
            # ambiguous (live file + backup copy) and can block injection.
            if any(part.startswith("_backup_") for part in path.relative_to(game_dir).parts):
                return
            candidates.append(path)
        except (OSError, ValueError):
            pass

    # 1) Прямой путь от корня игры.
    add_candidate(game_dir / rel)

    # 2) Если путь без расширения — пробуем типичные расширения.
    if rel.suffix == "":
        for ext in (".json", ".csv", ".tsv", ".txt", ".md", ".xml", ".yaml", ".yml", ".po"):
            add_candidate(game_dir / rel.with_suffix(ext))

    # 3) Ищем в типичных папках движка.
    search_roots = [
        game_dir,
        game_dir / "data",
        game_dir / "Data",
        game_dir / "www",
        game_dir / "www" / "data",
        game_dir / "Assets",
        game_dir / "StreamingAssets",
        game_dir / "Localization",
        game_dir / "Localizations",
    ]
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        add_candidate(root / rel)
        if rel.suffix == "":
            for ext in (".json", ".csv", ".tsv", ".txt", ".md", ".xml", ".yaml", ".yml", ".po"):
                add_candidate(root / rel.with_suffix(ext))

    # 4) Точный поиск по имени файла, если путь сохранён только как Actors / Map001 / etc.
    names_to_try = {rel.name}
    if rel.suffix == "":
        names_to_try.update({f"{rel.name}{ext}" for ext in (".json", ".csv", ".tsv", ".txt", ".md", ".xml", ".yaml", ".yml", ".po")})

    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        for name in names_to_try:
            for p in root.rglob(name):
                if p.is_file():
                    add_candidate(p)
                    if candidates:
                        break
            if candidates:
                break
        if candidates:
            break

    # Если найдено несколько разных файлов с одинаковым именем, нельзя
    # угадывать: перевод может попасть не в тот ресурс. Требуем однозначный
    # источник. Это особенно важно для Unity, где одинаковые JSON/CSV имена
    # часто встречаются в разных Asset/Localization папках.
    unique = []
    seen = set()
    for c in candidates:
        try:
            key = str(c.resolve())
        except OSError:
            key = str(c)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        return None
    return None


def _tokenize_path(path: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    token_re = re.compile(r"\[(\d+)\]|([^.\[\]]+)")
    for m in token_re.finditer(path.strip(".")):
        if m.group(1) is not None:
            parts.append(("index", m.group(1)))
        elif m.group(2):
            parts.append(("key", m.group(2)))
    return parts


def _set_json_value(root: Any, path: str, value: str, original: str = "") -> bool:
    """
    BUG FIX: писала по path вслепую — без проверки, что значение, которое
    сейчас там лежит, всё ещё совпадает с тем, что видел extractor (тот же
    риск и тот же фикс, что и в _set_nested из engine/injector.py — см.
    комментарий там для полного объяснения). Если original передан и
    текущее значение — строка, не совпадающая с ним, запись пропускается.
    """
    parts = _tokenize_path(path)
    if not parts:
        return False
    obj = root
    for kind, key in parts[:-1]:
        try:
            obj = obj[int(key)] if kind == "index" else obj[key]
        except Exception:
            return False
    last_kind, last_key = parts[-1]
    try:
        key_to_use = int(last_key) if last_kind == "index" else last_key
        if original:
            try:
                current = obj[key_to_use]
            except Exception:
                current = None
            if isinstance(current, str) and current != original:
                return False  # источник изменился с момента Extract — не рискуем
        obj[key_to_use] = value
        return True
    except Exception:
        return False


def _set_csv_cell(rows: list[list[str]], path: str, value: str, original: str = "") -> bool:
    """БАГ-FIX: та же проверка original, что и в _set_json_value выше."""
    m = re.fullmatch(r"row\[(\d+)\]\.col\[(\d+)\]", path)
    if not m:
        return False
    r = int(m.group(1))
    c = int(m.group(2))
    if r < 0 or r >= len(rows):
        return False
    if original and c < len(rows[r]) and rows[r][c] != original:
        return False  # источник изменился с момента Extract — не рискуем
    while c >= len(rows[r]):
        rows[r].append("")
    rows[r][c] = value
    return True


def _set_line(lines: list[str], path: str, value: str, original: str = "") -> bool:
    """БАГ-FIX: та же проверка original, что и в _set_json_value выше."""
    m = re.fullmatch(r"line\[(\d+)\]", path)
    if not m:
        return False
    idx = int(m.group(1))
    if idx < 0 or idx >= len(lines):
        return False
    if original and lines[idx] != original:
        return False  # источник изменился с момента Extract — не рискуем
    lines[idx] = value
    return True


_XML_TEXT_RE = re.compile(r">([^<>]+)<")


def _inject_xml_text(text: str, path: str, original: str, translation: str) -> str | None:
    """
    BUG FIX: раньше .xml-строки инжектировались целиком через _set_line,
    заменяя ВСЮ строку файла (включая теги/атрибуты) одним переводом —
    гарантированная порча XML-структуры. Теперь находим то же самое
    occurrence-индексированное ">текст<" вхождение, что и extractor, и
    заменяем ТОЛЬКО содержимое между > и <, оставляя теги и атрибуты
    нетронутыми. Работаем на СЫРОМ тексте файла (не через ElementTree.write,
    который переформатирует файл и молча удаляет XML-комментарии) —
    сохраняется 100% байт файла вне переведённого фрагмента.
    """
    m = re.fullmatch(r"xmltext\[(\d+)\]", path)
    if not m:
        return None
    target_idx = int(m.group(1))
    matches = list(_XML_TEXT_RE.finditer(text))
    if target_idx < 0 or target_idx >= len(matches):
        return None
    m2 = matches[target_idx]
    content = m2.group(1)
    if content.strip() != original:
        return None  # текст сдвинулся/не совпадает — не рискуем испортить файл
    escaped = saxutils.escape(translation)
    start, end = m2.span(1)
    return text[:start] + escaped + text[end:]


_PO_MSGSTR_RE = re.compile(r'^(\s*msgstr(?:\[\d+\])?\s+")((?:[^"\\]|\\.)*)("\s*)$')


def _inject_po_msgstr(lines: list[str], path: str, original: str, translation: str) -> bool:
    """
    BUG FIX: раньше .po msgstr-строка заменялась целиком через _set_line
    (теряя обёртку 'msgstr "..."'), а msgid МОГ быть переведён вовсе (что
    ломает поиск перевода по ключу в gettext). Теперь трогаем только
    значение конкретной msgstr-строки, восстанавливая C-style escape.
    """
    m = re.fullmatch(r"po\[(\d+)\]", path)
    if not m:
        return False
    idx = int(m.group(1))
    if idx < 0 or idx >= len(lines):
        return False
    m2 = _PO_MSGSTR_RE.match(lines[idx])
    if not m2:
        return False
    decoded_current = m2.group(2).replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    if decoded_current != original:
        return False
    encoded = translation.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    lines[idx] = m2.group(1) + encoded + m2.group(3)
    return True


_YAML_KV_RE = re.compile(r"^(\s*[\w.\-]+\s*:\s*)(\S.*)$")


_YAML_BOOL_NULL_RE = re.compile(
    r"^(?:y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|"
    r"on|On|ON|off|Off|OFF|null|Null|NULL|~)$"
)
_YAML_NUMBER_RE = re.compile(
    r"^[-+]?(?:0x[0-9a-fA-F]+|0o[0-7]+|\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|"
    r"\d+(?:[eE][-+]?\d+)?|\.inf|-\.inf|\.nan)$",
    re.IGNORECASE,
)


def _yaml_plain_scalar_needs_quoting(text: str) -> bool:
    """
    BUG FIX (P1-15, внешний аудит, подтверждено разбором YAML 1.1 scalar
    resolution): исходная проверка needs_quotes смотрела только на наличие
    "особых символов" (:#{}[]&*!|>'"%@` и т.п.) ГДЕ-ТО в строке. Но перевод
    БЕЗ единого такого символа всё равно может быть опасен как plain
    scalar — например перевод "5" (число как текст) без кавычек читается
    YAML-парсером как ЦЕЛОЕ ЧИСЛО 5, не строка "5"; "yes"/"no"/"true"/
    "false"/"null"/"~" читаются как bool/null. Если игра ожидает в этом
    поле строку, тип значения после Inject меняется молча. PyYAML не
    используется (не объявлен как зависимость, и весь проект сознательно
    делает точечные построчные правки без полного parse/re-serialize) —
    вместо этого явно проверяем совпадение с основными YAML 1.1
    scalar-паттернами (bool/null/число), которые резолвятся до Unity/PyYAML
    парсером, а не только "особые символы где-то в строке".
    """
    if text == "" or text.strip() != text:
        return True
    return bool(_YAML_BOOL_NULL_RE.match(text) or _YAML_NUMBER_RE.match(text))


def _inject_yaml_value(lines: list[str], path: str, original: str, translation: str) -> bool:
    """
    BUG FIX: раньше .yaml 'ключ: значение' заменялось целиком через
    _set_line, теряя 'ключ:' полностью — YAML переставал парситься.
    Теперь заменяем только значение, сохраняя ключ и его отступ/двоеточие.
    Если исходное значение было в кавычках — заново оборачиваем в кавычки
    того же типа с экранированием, иначе пишем как plain scalar.
    """
    m = re.fullmatch(r"yaml\[(\d+)\]", path)
    if not m:
        return False
    idx = int(m.group(1))
    if idx < 0 or idx >= len(lines):
        return False
    m2 = _YAML_KV_RE.match(lines[idx])
    if not m2:
        return False
    raw_value = m2.group(2).strip()
    quoted = len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in ("'", '"')
    current = raw_value[1:-1] if quoted else raw_value
    if not quoted and "#" in current:
        current = current.split("#", 1)[0].rstrip()
    if current != original:
        return False
    if quoted:
        q = raw_value[0]
        if q == '"':
            new_value = '"' + translation.replace("\\", "\\\\").replace('"', '\\"') + '"'
        else:
            new_value = "'" + translation.replace("'", "''") + "'"
    else:
        needs_quotes = (
            any(c in translation for c in ':#{}[]&*!|>\'"%@`')
            or _yaml_plain_scalar_needs_quoting(translation)
        )
        new_value = ('"' + translation.replace("\\", "\\\\").replace('"', '\\"') + '"') if needs_quotes else translation
    lines[idx] = m2.group(1) + new_value
    return True


def _backup_file(src: Path, backup_root: Path, game_dir: Path) -> None:
    rel = src.relative_to(game_dir)
    dst = backup_root / rel
    # BUG FIX: previously this unconditionally copied the CURRENT contents of
    # src over dst on every call. After the first successful Inject, src
    # already holds translated text, so any subsequent Inject run silently
    # clobbered the backup with that translated content — permanently losing
    # the true original the "Restore Original" feature depends on. Every
    # other engine's backup is create-once; do the same here.
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except (OSError, shutil.Error):
        pass


def inject_unity(
    game_dir: Path,
    translated_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    backup: bool = True,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
) -> dict:
    _log("[Unity] Начало вставки переводов", log_callback)
    game_dir = Path(game_dir)
    translated_dir = Path(translated_dir)
    extract_dir = translated_dir if translated_dir.name == "extracted" else translated_dir / "extracted"

    source_map = _load_extract_index(extract_dir)
    if source_map:
        _log(f"[Unity] Индекс источников загружен: {len(source_map)} файлов", log_callback)

    trans_files = sorted(extract_dir.glob("*_translated.json"))
    if not trans_files:
        _log(f"[Unity Inject] Файлы с переводом не найдены: {extract_dir}", log_callback)
        return {"files_modified": 0, "total_injected": 0, "total_errors": 0}

    backup_root = game_dir / "_backup_unity_original"

    # ── Идемпотентность ──────────────────────────────────────
    # BUG FIX: unity_injector was the only engine with no marker of a previous
    # injection. Combined with the _backup_file bug above, re-running Inject
    # a second time had no way to know originals were already replaced, and
    # the (by-then-corrupted) backup meant there was nothing to restore from
    # anyway. Bring Unity in line with every other engine: on a previous
    # injection, restore all backed-up files before doing anything else.
    _unity_marker = backup_root / ".rpgmtl_injected"
    if _unity_marker.exists():
        _log("[Unity] Маркер предыдущей инъекции. Восстанавливаем оригиналы...", log_callback)
        for bf in backup_root.rglob("*"):
            if not bf.is_file() or bf.name.startswith(".rpgmtl"):
                continue
            try:
                rel = bf.relative_to(backup_root)
                dest = game_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bf, dest)
            except Exception as e:
                _log(f"  ✗ {e}", log_callback)
        try:
            _unity_marker.unlink(missing_ok=True)
        except Exception:
            pass

    if backup and not backup_root.exists():
        backup_root.mkdir(parents=True)
        _log(f"[Unity] Создан бэкап: {backup_root}", log_callback)

    total_injected = 0
    total_errors = 0
    files_modified = 0

    for i, trans_file in enumerate(trans_files):
        if progress_callback:
            progress_callback(i + 1, len(trans_files), -1.0, trans_file.name)
        try:
            with open(trans_file, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception as e:
            _log(f"  [Unity] ✗ {trans_file.name}: {e}", log_callback)
            total_errors += 1
            continue

        if not isinstance(entries, list) or not entries:
            continue

        source_path = _resolve_source_path(trans_file, entries, source_map)
        if not source_path:
            _log(f"  [Unity] ✗ {trans_file.name}: не удалось определить source_path", log_callback)
            total_errors += 1
            continue

        orig_path = _guess_original_path(game_dir, source_path)
        if orig_path is None:
            _log(f"  [Unity] ✗ оригинал не найден: {source_path} (файл: {trans_file.name})", log_callback)
            total_errors += 1
            continue

        if backup:
            try:
                _backup_file(orig_path, backup_root, game_dir)
            except Exception as e:
                _log(f"  [Unity] ✗ backup {source_path}: {e}", log_callback)
                total_errors += 1
                continue

        ext = orig_path.suffix.lower()
        try:
            if ext == ".json":
                data = _load_json(orig_path)
                applied = 0
                for entry in entries:
                    translated = entry.get("translated", "")
                    if translated and _set_json_value(data, entry.get("path", ""), translated, entry.get("original", "")):
                        applied += 1
                if applied > 0:
                    _write_json(orig_path, data)
            elif ext in {".csv", ".tsv"}:
                text = orig_path.read_text(encoding="utf-8", errors="replace")
                delimiter = "\t" if ext == ".tsv" else ","
                try:
                    sniff = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
                    delimiter = sniff.delimiter
                except Exception:
                    pass
                # BUG FIX: same class of bug as unity_extractor.py's _extract_csv —
                # text.splitlines() breaks any quoted multi-line cell into separate
                # "lines" before csv.reader ever sees them. Row *count* survives
                # (csv.reader keeps consuming list items until the quote closes),
                # but the embedded newline itself is silently dropped from that
                # cell's content. Since injection writes the *entire* rows table
                # back out — not just the cells it changes — any other multi-line
                # cell elsewhere in the file got its newline stripped on every
                # inject pass, even if nothing targeted that cell. io.StringIO(text)
                # parses the same way a real file object would and round-trips
                # untouched cells byte-for-byte.
                rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
                applied = 0
                for entry in entries:
                    translated = entry.get("translated", "")
                    if translated and _set_csv_cell(rows, entry.get("path", ""), translated, entry.get("original", "")):
                        applied += 1
                # Пишем CSV только если реально применили хотя бы один перевод.
                if applied > 0:
                    buf = io.StringIO()
                    csv.writer(buf, delimiter=delimiter).writerows(rows)
                    encoded = buf.getvalue().encode("utf-8")
                    tmp_fd, tmp_path = tempfile.mkstemp(dir=orig_path.parent, suffix=".tmp", prefix=".rpgmtl_")
                    try:
                        with os.fdopen(tmp_fd, "wb") as f:
                            f.write(encoded)
                        os.replace(tmp_path, orig_path)
                    except Exception:
                        try: os.unlink(tmp_path)
                        except Exception: pass
                        raise
            elif ext == ".xml":
                text = orig_path.read_text(encoding="utf-8", errors="replace")
                applied = 0
                for entry in entries:
                    translated = entry.get("translated", "")
                    if not translated:
                        continue
                    result = _inject_xml_text(text, entry.get("path", ""), entry.get("original", ""), translated)
                    if result is not None:
                        text = result
                        applied += 1
                if applied > 0:
                    encoded = text.encode("utf-8")
                    tmp_fd, tmp_path = tempfile.mkstemp(dir=orig_path.parent, suffix=".tmp", prefix=".rpgmtl_")
                    try:
                        with os.fdopen(tmp_fd, "wb") as f:
                            f.write(encoded)
                        os.replace(tmp_path, orig_path)
                    except Exception:
                        try: os.unlink(tmp_path)
                        except Exception: pass
                        raise
            elif ext == ".po":
                lines = orig_path.read_text(encoding="utf-8", errors="replace").splitlines()
                applied = 0
                for entry in entries:
                    translated = entry.get("translated", "")
                    if translated and _inject_po_msgstr(lines, entry.get("path", ""), entry.get("original", ""), translated):
                        applied += 1
                # BUG FIX (P1-14, внешний аудит): .csv и .xml выше корректно
                # пишут только при applied > 0 — эти две ветки писали
                # безусловно, даже если НИ ОДНА запись не применилась.
                # read_text().splitlines() + "\n".join() без единого реального
                # изменения всё равно мог поменять line endings (CRLF -> LF)
                # и конечный перенос строки файла — несогласованность с
                # соседними ветками, не осознанное решение.
                if applied > 0:
                    encoded = "\n".join(lines).encode("utf-8")
                    tmp_fd, tmp_path = tempfile.mkstemp(dir=orig_path.parent, suffix=".tmp", prefix=".rpgmtl_")
                    try:
                        with os.fdopen(tmp_fd, "wb") as f:
                            f.write(encoded)
                        os.replace(tmp_path, orig_path)
                    except Exception:
                        try: os.unlink(tmp_path)
                        except Exception: pass
                        raise
            elif ext in {".yaml", ".yml"}:
                lines = orig_path.read_text(encoding="utf-8", errors="replace").splitlines()
                applied = 0
                for entry in entries:
                    translated = entry.get("translated", "")
                    if translated and _inject_yaml_value(lines, entry.get("path", ""), entry.get("original", ""), translated):
                        applied += 1
                # BUG FIX (P1-14, внешний аудит): см. идентичный комментарий
                # у .po веткой выше.
                if applied > 0:
                    encoded = "\n".join(lines).encode("utf-8")
                    tmp_fd, tmp_path = tempfile.mkstemp(dir=orig_path.parent, suffix=".tmp", prefix=".rpgmtl_")
                    try:
                        with os.fdopen(tmp_fd, "wb") as f:
                            f.write(encoded)
                        os.replace(tmp_path, orig_path)
                    except Exception:
                        try: os.unlink(tmp_path)
                        except Exception: pass
                        raise
            else:
                lines = orig_path.read_text(encoding="utf-8", errors="replace").splitlines()
                applied = 0
                for entry in entries:
                    translated = entry.get("translated", "")
                    if translated and _set_line(lines, entry.get("path", ""), translated, entry.get("original", "")):
                        applied += 1
                # BUG FIX: атомарная запись текстовых файлов

                encoded = "\n".join(lines).encode("utf-8")
                tmp_fd, tmp_path = tempfile.mkstemp(dir=orig_path.parent, suffix=".tmp", prefix=".rpgmtl_")
                try:
                    with os.fdopen(tmp_fd, "wb") as f:
                        f.write(encoded)
                    os.replace(tmp_path, orig_path)
                except Exception:
                    try: os.unlink(tmp_path)
                    except Exception: pass
                    raise

            if applied:
                files_modified += 1
                total_injected += applied
                _log(f"  [Unity] ✓ {source_path}: {applied} строк", log_callback)
        except Exception as e:
            _log(f"  [Unity] ✗ {source_path}: {e}", log_callback)
            total_errors += 1

    summary = {
        "total_injected": total_injected,
        "total_errors": total_errors,
        "files_modified": files_modified,
        "engine": "unity",
        "backup_dir": str(backup_root),
    }
    _log(f"[Unity] Итог: injected={total_injected}, errors={total_errors}, files={files_modified}", log_callback)
    if files_modified > 0:
        try:
            _unity_marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_unity_marker, "injected")
        except Exception as e:
            _log(f"  ⚠ Не удалось записать маркер инъекции: {e}", log_callback)
    return summary
