"""
kirikiri_extractor.py — извлечение текста из Kirikiri/KAG игр.

Kirikiri (KiriKiri2, KAG3) — движок для японских VN.
Скрипты: .ks файлы (могут быть в .xp3 архивах).

Форматы текста:
  田中「こんにちは！」[l][r]         — диалог с именем
  それは素晴らしいことです。[l][r]    — нарратор
  [face storage="a.png"]名前「テキスト」 — с командой перед текстом
"""

from engine.file_utils import atomic_write_json
import re
from pathlib import Path
from typing import Callable

from .text_filter import should_translate

# ── Паттерны ──────────────────────────────────────────────────────────────────

# Тег KAG: [tag ...] или [/tag]
_TAG_RE    = re.compile(r'\[/?[^\]\n]+\]')
# Команда на всю строку: @command или строка только из тегов
_CMD_RE    = re.compile(r'^\s*[@;*]')
# Диалог с именем: 名前「テキスト」 или 名前『テキスト』[l][r]
# BUG FIX: was [^「『\[「\n] — duplicate 「 in the class, now [^「『\[\n]
_DIALOG_RE = re.compile(r'^([^「『\[\n]{1,20})[「『](.*?)[」』]')
# Имя в отдельном теге: [name text="田中"]
_NAMETAG_RE = re.compile(r'\[name\s+text=["\']?([^"\'>\]\s]+)["\']?\]', re.IGNORECASE)


def _strip_tags(line: str) -> str:
    """Убрать все KAG-теги из строки."""
    return _TAG_RE.sub('', line).strip()


def _is_command_line(line: str) -> bool:
    """True если строка — чистая команда без текста."""
    stripped = line.strip()
    if not stripped or _CMD_RE.match(stripped):
        return True
    # Строка состоит только из тегов и пробелов
    if _TAG_RE.sub('', stripped).strip() == '':
        return True
    return False


def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


class KirikiriExtractor:
    def __init__(self, game_dir: Path, log_cb: Callable | None = None):
        self.game_dir = Path(game_dir)
        self.log_cb   = log_cb
        self.entries: list[dict] = []

    def _log(self, msg: str):
        _log(msg, self.log_cb)

    def _find_script_dir(self) -> Path | None:
        """Найти папку со скриптами .ks."""
        candidates = [
            self.game_dir / "data" / "scenario",
            self.game_dir / "data" / "Scenario",
            self.game_dir / "scenario",
            self.game_dir / "Scenario",
            self.game_dir / "script",
            self.game_dir / "data",
            self.game_dir,
        ]
        for c in candidates:
            if c.exists() and list(c.glob("*.ks")):
                return c
        # Рекурсивный поиск
        # BUG FIX: exclude _backup_* directories from scan
        found = [p for p in self.game_dir.rglob("*.ks")
                 if not any(part.startswith("_backup") for part in p.parts)]
        if found:
            return found[0].parent
        return None

    def _extract_ks(self, ks_path: Path):
        """Извлечь текст из одного .ks файла."""
        try:
            # Kirikiri файлы часто в Shift-JIS или UTF-8 с BOM
            for enc in ('utf-8-sig', 'utf-8', 'shift_jis', 'cp932'):
                try:
                    lines = ks_path.read_text(encoding=enc, errors='strict').splitlines()
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                lines = ks_path.read_text(encoding='cp932', errors='replace').splitlines()
        except Exception as e:
            self._log(f"  ✗ Ошибка чтения {ks_path.name}: {e}")
            return

        try:
            rel = str(ks_path.relative_to(self.game_dir))
        except ValueError:
            rel = ks_path.name

        pending_name = ""  # имя говорящего из [name] тега

        for lineno, raw_line in enumerate(lines, 1):
            line = raw_line.rstrip('\r\n')

            # Пропускаем команды и комментарии
            if _is_command_line(line):
                # Но проверяем [name text="..."] тег
                nm = _NAMETAG_RE.search(line)
                if nm:
                    pending_name = nm.group(1)
                continue

            # Ищем диалог вида  名前「テキスト」
            dm = _DIALOG_RE.match(_TAG_RE.sub('', line).lstrip())
            if dm:
                name = dm.group(1).strip()
                text = dm.group(2).strip()
                if should_translate(text):
                    self.entries.append({
                        "file":       rel,
                        "path":       str(lineno),
                        "original":   text,
                        "translated": "",
                        "context":    f"dialog:{name}",
                        "code":       0,
                        "game_type":  "kirikiri",
                        "_raw_line":  line,
                    })
                pending_name = ""
                continue

            # Убираем теги — что осталось это текст
            text = _strip_tags(line)
            if not text:
                continue

            # Пропускаем строки которые выглядят как пути/технические
            if text.startswith(('http', 'www', './', '../')):
                continue

            if should_translate(text):
                ctx = f"dialog:{pending_name}" if pending_name else "narrator"
                self.entries.append({
                    "file":       rel,
                    "path":       str(lineno),
                    "original":   text,
                    "translated": "",
                    "context":    ctx,
                    "code":       0,
                    "game_type":  "kirikiri",
                    "_raw_line":  line,
                })
                pending_name = ""

    def extract(self, progress_cb: Callable | None = None) -> list[dict]:
        script_dir = self._find_script_dir()
        if not script_dir:
            raise FileNotFoundError(
                f"Скрипты .ks не найдены в {self.game_dir}\n"
                "Если игра использует .xp3 архивы — распакуйте их сначала."
            )

        # BUG FIX: exclude _backup_* dirs
        ks_files = sorted(
            p for p in script_dir.rglob("*.ks")
            if not any(part.startswith("_backup") for part in p.parts)
        )
        self._log(f"[Kirikiri] Найдено .ks файлов: {len(ks_files)}")

        for i, ks in enumerate(ks_files):
            if progress_cb:
                progress_cb(i + 1, len(ks_files), -1.0, ks.name)
            self._log(f"[Kirikiri] Обработка: {ks.name}")
            before = len(self.entries)
            self._extract_ks(ks)
            added = len(self.entries) - before
            self._log(f"[Kirikiri]   → {added} строк")

        self._log(f"[Kirikiri] ✓ Итого: {len(self.entries)} строк из {len(ks_files)} файлов")
        return self.entries


def extract_kirikiri(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable | None = None,
    progress_callback: Callable | None = None,
) -> dict:
    """Главная функция извлечения для Kirikiri/KAG."""
    _log(f"[Kirikiri] Начало извлечения: {game_dir}", log_callback)

    ext = KirikiriExtractor(game_dir, log_callback)
    entries = ext.extract(progress_cb=progress_callback)

    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list[dict]] = {}
    for e in entries:
        by_file.setdefault(e["file"], []).append(e)

    for fname, file_entries in by_file.items():
        # Убираем _raw_line — не нужен в JSON
        clean = [{k: v for k, v in e.items() if k != "_raw_line"}
                 for e in file_entries]
        # BUG FIX: was fname.replace(...) + ".json" → double extension e.g. "scene.ks.json"
        # Strip the original extension first so saved filename has single .json extension.
        base = fname.replace("/", "_").replace("\\", "_")
        stem = base.rsplit(".", 1)[0] if "." in base else base
        safe = stem + ".json"
        # Update file field to match the saved filename (for dedup key consistency)
        for e in clean:
            e["_source_ks"] = e["file"]  # preserve original path for injector
            e["file"] = safe
        out = extract_dir / safe
        atomic_write_json(out, clean)

    index = {
        "game_dir":      str(game_dir),
        "game_type":     "kirikiri",
        "total_strings": len(entries),
        "files":         {f: len(e) for f, e in by_file.items()},
    }
    atomic_write_json(output_dir / "extract_index.json", index)
    _log(f"[Kirikiri] ✓ Сохранено: {len(entries)} строк", log_callback)
    return index
