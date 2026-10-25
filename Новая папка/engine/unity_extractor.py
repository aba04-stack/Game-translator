"""
unity_extractor.py — безопасное извлечение текста из Unity-проектов.

Поддерживает только текстовые источники, которые можно править без вскрытия
бинарных ассетов:
- JSON
- CSV / TSV
- TXT / MD / XML / YAML / PO как построчные тексты

Это не полный Unity asset ripper. Для .assets/.bundle нужен отдельный слой.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from engine.file_utils import atomic_write_json
from pathlib import Path
from typing import Callable, Iterable

from .text_filter import should_translate

TEXT_EXTS = {".json", ".csv", ".tsv", ".txt", ".md", ".xml", ".yaml", ".yml", ".po"}
SKIP_KEYS = {
    # BUG FIX: "name" was in SKIP_KEYS but character/item names ARE translatable.
    # "type" removed too — skill/item types may have display text.
    "id", "guid", "uuid", "path", "file", "filename", "version",
    "locale", "language", "lang", "hash", "checksum", "font", "size", "color",
    "sprite", "icon", "asset", "bundle", "address", "key", "keyid", "idkey",
}

# BUG FIX: text-between-tags for XML — matches ">content<" so we only ever
# touch genuine leaf text nodes, never tag names or attribute values.
# Occurrence-index (nth match, left-to-right) is used as a stable locator
# for injection, avoiding a full XML tree re-serialization (which loses
# comments and reformats the file — see unity_injector.py's BUG FIX note
# on _inject_xml_text for the empirical confirmation).
_XML_TEXT_RE = re.compile(r">([^<>]+)<")

# BUG FIX: PO/gettext msgid/msgstr line — only msgstr's quoted VALUE is
# translatable; msgid is the lookup key and must never be touched (same
# class of bug as translating an RPG Maker script identifier: translating
# the key desyncs it from whatever looks it up). Handles the common
# single-line case; multi-line continuation strings are left untouched
# (extraction simply won't match them — safer than mishandling).
_PO_MSGSTR_RE = re.compile(r'^(\s*msgstr(?:\[\d+\])?\s+")((?:[^"\\]|\\.)*)("\s*)$')

# BUG FIX: simple flat "key: value" YAML line — only the value after the
# first unquoted colon is translatable; the key must be preserved verbatim
# or the YAML structure breaks. Deliberately narrow (single-line scalar
# values only, no block scalars/lists/nested mappings) — safer to under-
# extract than to mishandle a form we can't safely round-trip.
_YAML_KV_RE = re.compile(r"^(\s*[\w.\-]+\s*:\s*)(\S.*)$")


def _log(msg: str, cb: Callable[[str], None] | None = None) -> None:
    print(msg, flush=True)
    if cb:
        cb(msg)


def _safe_name(rel_path: Path) -> str:
    """Convert relative path to a safe flat filename stem (no extension).

    BUG FIX: previously included the extension in the joined parts, causing
    save files to get double extension (.json.json) and a key mismatch in
    apply_dedup_to_files where file_data keys use jf.name (with .json) but
    entries used safe (without .json suffix when rel_path has no ext).
    Now consistently strips the extension from the last component.
    Callers that save to disk should append ".json" explicitly.
    """
    parts = [p for p in rel_path.parts if p not in {"", ".", ".."}]
    if parts:
        # Strip extension from last component to avoid double extension
        # e.g. "Localization_JA.json" → "Localization_JA"
        parts = parts[:-1] + [Path(parts[-1]).stem]
    name = "__".join(parts) if parts else "root"
    if len(name) > 80:
        h = hashlib.md5(name.encode('utf-8')).hexdigest()[:8]
        # 36 + 2 + 8 + 2 + 30 = 78 chars ≤ 80
        name = f"{name[:36]}__{h}__{name[-30:]}"
    return name


def _looks_unity_project(game_dir: Path) -> bool:
    if not game_dir.exists() or not game_dir.is_dir():
        return False
    if (game_dir / "ProjectSettings" / "ProjectVersion.txt").exists():
        return True
    if (game_dir / "Assets").is_dir() and (game_dir / "ProjectSettings").is_dir():
        return True
    try:
        return any(p.is_dir() and p.name.endswith("_Data") for p in game_dir.iterdir())
    except Exception:
        return False


def _candidate_roots(game_dir: Path) -> list[Path]:
    roots: list[Path] = []

    for p in [game_dir / "Assets", game_dir / "StreamingAssets", game_dir / "Resources", game_dir / "Localization", game_dir / "Localizations"]:
        if p.is_dir():
            roots.append(p)

    for child in game_dir.iterdir() if game_dir.exists() else []:
        if not child.is_dir() or not child.name.endswith("_Data"):
            continue
        for sub in ("StreamingAssets", "Resources", "Managed"):
            p = child / sub
            if p.is_dir():
                roots.append(p)
        for subname in ("StreamingAssets", "Resources", "Localization", "Localizations"):
            for p in child.rglob(subname):
                if p.is_dir():
                    roots.append(p)

    for subname in ("StreamingAssets", "Resources", "Localization", "Localizations"):
        for p in game_dir.rglob(subname):
            if p.is_dir():
                roots.append(p)

    seen: set[str] = set()
    uniq: list[Path] = []
    for r in roots:
        key = str(r.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def _walk_json(obj: object, file_name: str, source_path: str, path: str = "", out: list[dict] | None = None) -> list[dict]:
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in SKIP_KEYS:
                continue
            child = f"{path}.{k}" if path else str(k)
            _walk_json(v, file_name, source_path, child, out)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child = f"{path}[{i}]" if path else f"[{i}]"
            _walk_json(item, file_name, source_path, child, out)
    elif isinstance(obj, str) and should_translate(obj):
        out.append({
            # BUG FIX: was just file_name (no ext), causing mismatch with apply_dedup_to_files
            # which keys file_data by jf.name (with .json). Now file = safe_stem + ".json".
            "file": file_name + ".json" if not file_name.endswith(".json") else file_name,
            "source_path": source_path,
            "path": path or "value",
            "original": obj,
            "translated": "",
            "context": "unity_json",
            "game_type": "unity",
        })
    return out


def _extract_json(path: Path, rel: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    return _walk_json(data, _safe_name(rel), rel.as_posix())


def _extract_csv(path: Path, rel: Path) -> list[dict]:
    safe = _safe_name(rel)
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []

    delimiter = ","
    try:
        sniff = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        delimiter = sniff.delimiter
    except Exception:
        pass

    # BUG FIX: csv.reader(text.splitlines(), ...) split the file into physical
    # lines BEFORE the CSV parser ever saw it. Row *count* survived (csv.reader
    # keeps consuming list items as continuations until a quote closes), but a
    # quoted cell containing a real newline (multi-line dialogue, common in
    # Unity localization CSVs) had that embedded "\n" silently dropped when
    # the fragments were rejoined — corrupting the extracted text before it
    # was even sent to translation. io.StringIO(text) iterates the same way
    # Python iterates a real file object, so multi-line quoted fields round-
    # trip correctly.
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    out: list[dict] = []
    for r_idx, row in enumerate(rows):
        if r_idx == 0:
            continue
        for c_idx, cell in enumerate(row):
            if c_idx == 0:
                continue
            if isinstance(cell, str) and should_translate(cell):
                out.append({
                    # BUG FIX: add .json suffix for dedup key consistency
                    "file": safe + ".json",
                    "source_path": rel.as_posix(),
                    "path": f"row[{r_idx}].col[{c_idx}]",
                    "original": cell,
                    "translated": "",
                    "context": "unity_csv",
                    "game_type": "unity",
                })
    return out


def _extract_line_text(path: Path, rel: Path) -> list[dict]:
    """Для .txt/.md — построчный текст без структуры, где вся строка
    ЯВЛЯЕТСЯ переводимым содержимым (нет ключей/тегов, которые надо сохранить)."""
    safe = _safe_name(rel)
    out: list[dict] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if isinstance(line, str) and should_translate(line):
            out.append({
                # BUG FIX: add .json suffix for dedup key consistency
               "file": safe + ".json",
                "source_path": rel.as_posix(),
                "path": f"line[{idx}]",
                "original": line,
                "translated": "",
                "context": "unity_text",
                "game_type": "unity",
            })
    return out


def _extract_xml(path: Path, rel: Path) -> list[dict]:
    """
    BUG FIX: раньше .xml маршрутизировался в _extract_line_text — ЦЕЛАЯ
    строка (включая теги и атрибуты, например
    '<Dialogue id="1" speaker="Yuusha">こんにちは！</Dialogue>') извлекалась
    и переводилась одним куском без структурной защиты разметки от ИИ, а
    при инъекции строка заменялась целиком — теги/атрибуты терялись
    полностью. Теперь извлекаем ТОЛЬКО текст между > и < (path.col
    указывает occurrence-индекс — какое по счёту вхождение ">текст<" в
    файле), никогда не трогая имена тегов или значения атрибутов.
    """
    safe = _safe_name(rel)
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[dict] = []
    for idx, m in enumerate(_XML_TEXT_RE.finditer(text)):
        content = m.group(1)
        stripped = content.strip()
        if not stripped or content != stripped:
            continue  # пропускаем чистый whitespace/отступы между тегами
        if not should_translate(stripped):
            continue
        out.append({
            "file": safe + ".json",
            "source_path": rel.as_posix(),
            "path": f"xmltext[{idx}]",
            "original": stripped,
            "translated": "",
            "context": "unity_xml",
            "game_type": "unity",
        })
    return out


def _extract_po(path: Path, rel: Path) -> list[dict]:
    """
    BUG FIX: раньше .po маршрутизировался в _extract_line_text — строка
    'msgid "hello"' извлекалась и МОГЛА быть переведена (msgid — это КЛЮЧ
    поиска перевода в gettext, переводить его нельзя — тот же класс бага,
    что и с идентификаторами RPG Maker/Wolf). А для 'msgstr "こんにちは"'
    вся строка заменялась переводом целиком, теряя обёртку msgstr "...".
    Теперь извлекается ТОЛЬКО значение msgstr, msgid никогда не трогается.
    """
    safe = _safe_name(rel)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict] = []
    for idx, line in enumerate(lines):
        m = _PO_MSGSTR_RE.match(line)
        if not m:
            continue
        value = m.group(2)
        # .po хранит текст с C-style escape (\", \\, \n) — раскодируем перед
        # отправкой в перевод, закодируем обратно при инъекции.
        decoded = value.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        if not should_translate(decoded):
            continue
        out.append({
            "file": safe + ".json",
            "source_path": rel.as_posix(),
            "path": f"po[{idx}]",
            "original": decoded,
            "translated": "",
            "context": "unity_po",
            "game_type": "unity",
        })
    return out


def _extract_yaml(path: Path, rel: Path) -> list[dict]:
    """
    BUG FIX: раньше .yaml/.yml маршрутизировался в _extract_line_text —
    строка 'greeting: こんにちは' извлекалась и переводилась целиком; при
    инъекции строка заменялась ОДНИМ переводом, теряя ключ 'greeting:'
    полностью — YAML переставал парситься как валидная структура. Теперь
    извлекается только значение после первого ':' простых плоских пар
    ключ-значение; ключ всегда сохраняется на инъекции. Намеренно не
    обрабатывает блочные скаляры/списки/вложенные структуры — это
    безопаснее, чем попытка их наивно распарсить построчно и ошибиться.
    """
    safe = _safe_name(rel)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict] = []
    for idx, line in enumerate(lines):
        m = _YAML_KV_RE.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        if value.startswith(("#", "&", "*", "|", ">", "!")):
            continue  # якоря/блочные скаляры/теги — не наша забота
        # Снимаем обрамляющие кавычки, если есть, чтобы отправить в перевод
        # чистый текст, а не '"текст"' с кавычками внутри.
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')
        candidate = value[1:-1] if quoted else value
        if "#" in candidate and not quoted:
            candidate = candidate.split("#", 1)[0].rstrip()
        if not should_translate(candidate):
            continue
        out.append({
            "file": safe + ".json",
            "source_path": rel.as_posix(),
            "path": f"yaml[{idx}]",
            "original": candidate,
            "translated": "",
            "context": "unity_yaml",
            "game_type": "unity",
        })
    return out


def _iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in TEXT_EXTS:
            yield p


def extract_unity(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
) -> dict:
    _log(f"[Unity] Начало извлечения: {game_dir}", log_callback)
    _log("[Unity] ⚠ ВНИМАНИЕ: Поддерживаются только открытые файлы (JSON, CSV, TXT).", log_callback)
    _log("[Unity] ⚠ AssetBundles, Addressables и .assets не извлекаются.", log_callback)
    
    game_dir = Path(game_dir)
    output_dir = Path(output_dir)
    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    if not _looks_unity_project(game_dir):
        raise FileNotFoundError(f"Похоже, это не Unity-проект: {game_dir}\nОжидались Assets/ProjectSettings или *_Data.")

    roots = _candidate_roots(game_dir)
    _log(f"[Unity] Корневых папок-кандидатов: {len(roots)}", log_callback)

    files: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for p in _iter_files(root):
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                files.append(p)
    files.sort()
    _log(f"[Unity] Текстовых файлов-кандидатов: {len(files)}", log_callback)

    all_entries: list[dict] = []
    per_file: dict[str, list[dict]] = {}

    for idx, file_path in enumerate(files):
        if progress_callback:
            progress_callback(idx + 1, len(files), -1.0, file_path.name)
        rel = file_path.relative_to(game_dir)
        ext = file_path.suffix.lower()
        try:
            if ext == ".json":
                entries = _extract_json(file_path, rel)
            elif ext in {".csv", ".tsv"}:
                entries = _extract_csv(file_path, rel)
            elif ext == ".xml":
                entries = _extract_xml(file_path, rel)
            elif ext == ".po":
                entries = _extract_po(file_path, rel)
            elif ext in {".yaml", ".yml"}:
                entries = _extract_yaml(file_path, rel)
            else:
                entries = _extract_line_text(file_path, rel)
        except Exception as e:
            _log(f"  [Unity] ✗ {rel.as_posix()}: {e}", log_callback)
            continue

        if entries:
            safe = _safe_name(rel)
            per_file[safe] = entries
            all_entries.extend(entries)
            _log(f"  [Unity] ✓ {rel.as_posix()}: {len(entries)} строк", log_callback)

    for safe, entries in per_file.items():
        atomic_write_json(extract_dir / f"{safe}.json", entries)

    index = {
        "game_dir": str(game_dir),
        "engine": "unity",
        "total_strings": len(all_entries),
        "files": {k + ".json": len(v) for k, v in per_file.items()},
        # BUG FIX: source keys now include .json to match file_data keys in dedup
        "sources": {k + ".json": (v[0].get("source_path", "") if v else "") for k, v in per_file.items()},
    }
    atomic_write_json(output_dir / "extract_index.json", index)

    _log(f"[Unity] ✓ Извлечено строк: {len(all_entries)} из {len(per_file)} файлов", log_callback)
    return index
