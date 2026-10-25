"""
godot_extractor.py — извлечение текста из проектов на движке Godot Engine.

Поддерживает открытые файлы проекта:
- .tscn (Сцены, свойства text = "...")
- .tres (Ресурсы, строковые данные)
- .gd (Скрипты, вызовы tr("..."))
- .dialogue (Плагин Dialogic)
"""

from engine.file_utils import atomic_write_json
import re
import hashlib
from pathlib import Path
from typing import Callable

from .text_filter import should_translate

# Паттерны для Godot
# Сцены и ресурсы (text = "Какой-то текст")
_TSCN_TEXT_RE = re.compile(r'^(?:\s*)(text|dialog_text|bbcode_text|title)\s*=\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
# Скрипты GDScript: tr("Текст")
_GD_TR_RE = re.compile(r'tr\(\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*\)')
# Диалоги (Dialogic)
_DIALOGUE_RE = re.compile(r'^(?:~.*?|([A-Za-z0-9_]+)\s*:)?\s*(.*?[a-zA-Zа-яёА-ЯЁ぀-ヿ一-鿿].*)$')

def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)

def _safe_name(rel_path: str) -> str:
    """Безопасное имя файла, защита от слишком длинных путей (MAX_PATH bug)."""
    safe = rel_path.replace("/", "__").replace("\\", "__")
    if len(safe) > 80:
        h = hashlib.md5(safe.encode('utf-8')).hexdigest()[:8]
        # 36 + 2 + 8 + 2 + 30 = 78 chars ≤ 80
        safe = f"{safe[:36]}__{h}__{safe[-30:]}"
    return safe

class GodotExtractor:
    def __init__(self, game_dir: Path, log_cb: Callable | None = None):
        self.game_dir = Path(game_dir)
        self.log_cb = log_cb
        self.entries: list[dict] = []

    def _log(self, msg: str):
        _log(msg, self.log_cb)

    def _unescape(self, s: str) -> str:
        """Раскодировать basic escape-последовательности из GDScript-строки.

        BUG FIX: та же ошибка порядка, что была в renpy_extractor._unquote —
        обработка \\n раньше \\\\ приводила к тому, что экранированный бэкслеш,
        за которым сразу следует буква "n" (например, путь "C:\\network" в
        исходнике GDScript, т.е. один бэкслеш + "network"), портился в
        реальный перевод строки вместо literal backslash+"n". Защищаем \\\\
        через плейсхолдер до обработки \\n / \\".
        """
        s = s.replace('\\\\', '\x00RPGMTL_BACKSLASH\x00')
        s = s.replace('\\n', '\n').replace('\\"', '"')
        return s.replace('\x00RPGMTL_BACKSLASH\x00', '\\')

    def _extract_tscn_tres(self, path: Path, rel_path: str):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            self._log(f"  ✗ Ошибка чтения {path.name}: {e}")
            return

        count = 0
        for i, line in enumerate(lines):
            m = _TSCN_TEXT_RE.match(line)
            if m:
                prop, val_raw = m.groups()
                val = self._unescape(val_raw)
                if should_translate(val):
                    self.entries.append({
                        "file": rel_path,
                        "path": f"line:{i}",
                        "original": val,
                        "translated": "",
                        "context": f"godot_{prop}",
                        "game_type": "godot"
                    })
                    count += 1
        if count:
            self._log(f"  [Godot] Найдено {count} строк в {path.name}")

    def _extract_dialogue(self, path: Path, rel_path: str):
        """Парсить .dialogue файлы плагина Dialogic.

        Формат:
          character_name: Реплика персонажа
          ~ label
          if condition
          - Вариант меню
          Нарратив без имени

        BUG FIX: ранее .dialogue был в exts но никогда не обрабатывался —
        мёртвый код. Теперь реализован.
        """
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            self._log(f"  ✗ Ошибка чтения {path.name}: {e}")
            return

        count = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("~"):
                continue
            # Вариант выбора: "- текст"
            if stripped.startswith("- "):
                text = stripped[2:].strip()
                ctx = "godot_dialog_choice"
            else:
                # "Character: текст" или просто "текст"
                if ":" in stripped:
                    speaker, _, text = stripped.partition(":")
                    speaker = speaker.strip()
                    text = text.strip()
                    # Игнорируем служебные строки типа "if:", "elif:", "elif_if:"
                    if speaker.lower() in ("if", "elif", "elif_if", "else", "end", "join", "leave", "signal"):
                        continue
                    ctx = f"say:{speaker}"
                else:
                    text = stripped
                    ctx = "godot_dialog_narration"

            text = self._unescape(text)
            if should_translate(text):
                self.entries.append({
                    "file": rel_path,
                    "path": f"line:{i}",
                    "original": text,
                    "translated": "",
                    "context": ctx,
                    "game_type": "godot",
                })
                count += 1
        if count:
            self._log(f"  [Godot] Найдено {count} строк диалога в {path.name}")

    def _extract_gd(self, path: Path, rel_path: str):
        """Извлечь строки из GDScript вызовов tr(\"...\")."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        count = 0
        for m in _GD_TR_RE.finditer(text):
            val = self._unescape(m.group(1))
            if should_translate(val):
                self.entries.append({
                    "file": rel_path,
                    "path": f"regex_tr:{m.start()}",
                    "original": val,
                    "translated": "",
                    "context": "godot_script",
                    "game_type": "godot"
                })
                count += 1
        if count:
            self._log(f"  [Godot] Найдено {count} строк в {path.name}")

    def extract(self, progress_cb: Callable | None = None) -> list[dict]:
        # Проверяем, распакован ли .pck
        if not any(self.game_dir.glob("*.tscn")) and not any(self.game_dir.rglob("*.tscn")):
            if any(self.game_dir.glob("*.pck")):
                self._log("[Godot] ⚠ ВНИМАНИЕ: Найден файл .pck. Для перевода Godot-игры необходимо распаковать .pck (например, через GodotPCKExplorer).")
                return []

        # Ищем все поддерживаемые файлы
        exts = {".tscn", ".tres", ".gd", ".dialogue"}
        files = [p for p in self.game_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts and "_backup" not in p.parts]
        
        self._log(f"[Godot] Файлов для сканирования: {len(files)}")

        for idx, path in enumerate(files):
            if progress_cb:
                progress_cb(idx + 1, len(files), -1.0, "")
                
            try:
                rel = str(path.relative_to(self.game_dir))
            except ValueError:
                rel = path.name

            ext = path.suffix.lower()
            if ext in {".tscn", ".tres"}:
                self._extract_tscn_tres(path, rel)
            elif ext == ".gd":
                self._extract_gd(path, rel)
            elif ext == ".dialogue":
                self._extract_dialogue(path, rel)

        # Локальная дедупликация в пределах файла для чистоты
        unique = []
        seen = set()
        for e in self.entries:
            k = (e["file"], e["path"], e["original"])
            if k not in seen:
                seen.add(k)
                unique.append(e)
        self.entries = unique

        return self.entries


def extract_godot(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable | None = None,
    progress_callback: Callable | None = None,
) -> dict:
    _log(f"[Godot] Начало извлечения: {game_dir}", log_callback)

    ext = GodotExtractor(game_dir, log_callback)
    entries = ext.extract(progress_cb=progress_callback)

    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list[dict]] = {}
    for e in entries:
        by_file.setdefault(e["file"], []).append(e)

    for src_rel, file_entries in by_file.items():
        safe_name = _safe_name(src_rel) + ".json"
        
        for e in file_entries:
            e["file"] = safe_name
            e["_source_path"] = src_rel
            
        out = extract_dir / safe_name
        atomic_write_json(out, file_entries)

    index = {
        "game_dir": str(game_dir),
        "game_type": "godot",
        "total_strings": len(entries),
        "files": {f: len(e) for f, e in by_file.items()},
    }
    atomic_write_json(output_dir / "extract_index.json", index)
    _log(f"[Godot] ✓ Сохранено: {len(entries)} строк", log_callback)
    return index
