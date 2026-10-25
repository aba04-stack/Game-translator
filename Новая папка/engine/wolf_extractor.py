"""
wolf_extractor.py — извлечение текста из Wolf RPG Editor игр.

Wolf RPG Editor хранит текст в бинарных файлах:
  Data/BasicData/DataBase.wolf   — база данных (персонажи, предметы)
  Data/BasicData/CommonEvent.dat — общие события
  Data/MapData/Map*.mps          — карты с событиями

Строки хранятся как: [4 байта LE длина][bytes в CP932 или UTF-8]
Текстовые команды в событиях имеют коды 101, 401 (Message).
"""

import hashlib
from engine.file_utils import atomic_write_json
from engine.file_utils import find_dir_ci
import struct
from pathlib import Path
from typing import Callable

from .text_filter import should_translate

# ── Константы ─────────────────────────────────────────────────────────────────

# Magic bytes Wolf RPG файлов
WOLF_DB_MAGIC  = b"WOLF RPG Editor"
WOLF_DB_MAGIC2 = b"WOLF RPG Editor Extension"
MPS_MAGIC      = bytes([0x9F, 0x12, 0x09, 0x91])
DAT_MAGIC      = bytes([0x00, 0x00, 0x00, 0x00])

# Минимальная длина строки для перевода
MIN_STR_LEN = 2
MAX_STR_LEN = 2048

# Команды событий (те же числа что RPG Maker)
CMD_MESSAGE     = 101
CMD_MESSAGE_CTD = 401   # продолжение сообщения
CMD_CHOICES     = 102
CMD_CHOICE_TEXT = 10    # текст выбора в Wolf


def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


# ── Чтение бинарных строк ─────────────────────────────────────────────────────

def _decode_str(data: bytes) -> str | None:
    """Попытаться декодировать байты как CP932 или UTF-8."""
    for enc in ('utf-8', 'cp932', 'shift_jis'):
        try:
            s = data.decode(enc)
            return s
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _encode_str(text: str, original_bytes: bytes) -> bytes:
    """Перекодировать строку в исходную кодировку."""
    # Определяем исходную кодировку
    for enc in ('utf-8', 'cp932', 'shift_jis'):
        try:
            original_bytes.decode(enc)
            return text.encode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return text.encode('utf-8', errors='replace')


def _has_japanese(text: str) -> bool:
    """Проверить есть ли японские символы в строке."""
    for ch in text:
        cp = ord(ch)
        # Hiragana, Katakana, CJK Unified Ideographs, CJK Extension
        if (0x3040 <= cp <= 0x309F or  # Hiragana
            0x30A0 <= cp <= 0x30FF or  # Katakana
            0x4E00 <= cp <= 0x9FFF or  # CJK
            0xFF01 <= cp <= 0xFF60 or  # Fullwidth
            0x3000 <= cp <= 0x303F):   # CJK punctuation
            return True
    return False


class BinaryReader:
    """Простой ридер бинарных данных."""
    def __init__(self, data: bytes):
        self.data = data
        self.pos  = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def read_u8(self) -> int:
        if self.remaining() < 1:
            return 0
        v = self.data[self.pos]
        self.pos += 1
        return v

    def read_u32(self) -> int:
        if self.remaining() < 4:
            return 0
        v = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_wolf_str(self) -> tuple[int, str | None]:
        """Читать Wolf-строку: 4-байтная длина + байты. Возвращает (offset, text)."""
        if self.remaining() < 4:
            return -1, None
        offset = self.pos
        length = self.read_u32()
        if length == 0:
            return offset, ""
        if length > MAX_STR_LEN or length > self.remaining():
            self.pos -= 4  # откатываемся
            return -1, None
        raw = self.read(length)
        text = _decode_str(raw)
        return offset, text

    def peek_u32(self) -> int:
        if self.remaining() < 4:
            return 0
        return struct.unpack_from('<I', self.data, self.pos)[0]

    def skip(self, n: int):
        self.pos = min(self.pos + n, len(self.data))

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.data)


# ── Сканирование строк (heuristic) ────────────────────────────────────────────

def _scan_strings(data: bytes, fname: str, low_confidence: bool = False) -> list[dict]:
    """
    Эвристический сканер: ищет length-prefixed строки с японским текстом.
    Возвращает список записей с байтовым смещением.

    low_confidence: сканер не понимает структуру файла — не знает, к какой
    команде/полю принадлежит строка. Для .wolf-файлов (DataBase.wolf,
    SysDataBase.wolf — таблицы БД: анимации, предметы, система) это реальный
    риск: строка может оказаться не диалогом, а именем ресурса/анимации,
    которое движок ищет по точному оригинальному значению — перевод такой
    строки молча ломает игру (репорт пользователя: "анимации не работают"
    после перевода). CommonEvent.dat/Map*.mps — это в основном реальные
    диалоговые команды, там риск ниже. См. фильтр в pipeline.translate().
    """
    entries = []
    pos = 0
    n = len(data)

    while pos < n - 4:
        length = struct.unpack_from('<I', data, pos)[0]
        if MIN_STR_LEN <= length <= MAX_STR_LEN and pos + 4 + length <= n:
            raw = data[pos + 4: pos + 4 + length]
            text = _decode_str(raw)
            if text and _has_japanese(text) and should_translate(text):
                # Проверяем что перед строкой нет мусора (байт > 0x1F или ASCII)
                entries.append({
                    "file":            fname,
                    "path":            str(pos),   # байтовое смещение
                    "original":        text,
                    "translated":      "",
                    "context":         "wolf_string",
                    "code":            0,
                    "game_type":       "wolf",
                    "_raw_len":        length,
                    "_encoding":       _detect_encoding(raw),
                    "_low_confidence": low_confidence,
                })
                # Перепрыгиваем строку
                pos += 4 + length
                continue
        pos += 1

    return entries


def _detect_encoding(raw: bytes) -> str:
    for enc in ('utf-8', 'cp932', 'shift_jis'):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return 'cp932'


# ── Парсер событий Wolf RPG (.mps и .dat) ─────────────────────────────────────
# NOTE: _parse_wolf_commands is defined but currently not called by _extract_file
# or _parse_mps_events — both use the safer heuristic _scan_strings instead.
# Retained for future structured extraction but should not be called without
# verifying correct binary offset parsing for each Wolf RPG version.

def _parse_wolf_commands(reader: BinaryReader, fname: str,
                          base_path: str) -> list[dict]:
    """
    (Unused — structural event parser, kept for future use.)
    Читать список команд Wolf RPG события.
    Команды: [код 4b][кол-во строк 4b][строки...][кол-во чисел 4b][числа...]
    """
    entries = []
    cmd_count = reader.read_u32()
    if cmd_count > 10000:
        return entries

    i = 0
    while i < cmd_count:
        if reader.remaining() < 4:
            break
        code = reader.read_u32()
        if reader.remaining() < 4:
            break
        str_count = reader.read_u32()
        if str_count > 100:
            break

        texts = []
        for _ in range(str_count):
            offset, text = reader.read_wolf_str()
            if text is not None:
                texts.append((offset, text))
            else:
                # Что-то пошло не так, пропускаем оставшиеся строки
                break

        num_count = reader.read_u32() if reader.remaining() >= 4 else 0
        if num_count > 1000:
            break
        reader.skip(num_count * 4)

        # Извлекаем текст из текстовых команд
        if code in (CMD_MESSAGE, CMD_MESSAGE_CTD) and texts:
            for offset, text in texts:
                if text and should_translate(text):
                    entries.append({
                        "file":       fname,
                        "path":       str(offset),
                        "original":   text,
                        "translated": "",
                        "context":    "wolf_dialog",
                        "code":       code,
                        "game_type":  "wolf",
                        "_raw_len":   len(text.encode('cp932', errors='replace')),
                        "_encoding":  "cp932",
                    })
        elif code == CMD_CHOICES and texts:
            for offset, text in texts:
                if text and should_translate(text):
                    entries.append({
                        "file":       fname,
                        "path":       str(offset),
                        "original":   text,
                        "translated": "",
                        "context":    "wolf_choice",
                        "code":       code,
                        "game_type":  "wolf",
                        "_raw_len":   len(text.encode('cp932', errors='replace')),
                        "_encoding":  "cp932",
                    })
        i += 1

    return entries


def _parse_mps_events(data: bytes, fname: str) -> list[dict]:
    """Парсить .mps карту Wolf RPG."""
    entries = []

    # Пропускаем заголовок
    if len(data) < 10:
        return entries

    # Ищем сигнатуру событий: 0x00 0x00 0x00 0x00 после слоёв тайлов
    # Проще всего — используем эвристический сканер
    return _scan_strings(data, fname)


# ── Главный экстрактор ─────────────────────────────────────────────────────────

class WolfExtractor:
    def __init__(self, game_dir: Path, log_cb: Callable | None = None):
        self.game_dir = Path(game_dir)
        self.log_cb   = log_cb
        self.entries: list[dict] = []

    def _log(self, msg: str):
        _log(msg, self.log_cb)

    def _find_data_dirs(self) -> tuple[Path | None, Path | None]:
        """Найти BasicData/ и MapData/ папки."""
        basic = None
        mapdata = None
        for base in [*([d] if (d := find_dir_ci(self.game_dir, "Data")) else []), self.game_dir / "data",
                     self.game_dir / "DATA"]:
            if not base.is_dir():
                continue
            b = base / "BasicData"
            if not b.exists():
                b = base / "basicdata"
            if b.exists():
                basic = b
            m = base / "MapData"
            if not m.exists():
                m = base / "mapdata"
            if m.exists():
                mapdata = m
            if basic:
                break
        # Если структуры нет — ищем рекурсивно
        if not basic:
            for f in self.game_dir.rglob("DataBase.wolf"):
                basic = f.parent
                break
        if not mapdata:
            for f in self.game_dir.rglob("Map*.mps"):
                mapdata = f.parent
                break
        return basic, mapdata

    def _extract_file(self, path: Path):
        """Извлечь строки из одного файла."""
        try:
            data = path.read_bytes()
        except Exception as e:
            self._log(f"  ✗ Ошибка чтения {path.name}: {e}")
            return

        try:
            rel = str(path.relative_to(self.game_dir))
        except ValueError:
            rel = path.name

        # .wolf = DataBase.wolf/SysDataBase.wolf — таблицы БД (см. docstring _scan_strings)
        low_confidence = path.suffix.lower() == ".wolf"

        # Используем эвристический сканер для всех типов
        found = _scan_strings(data, rel, low_confidence=low_confidence)
        if found:
            n_lc = sum(1 for e in found if e.get("_low_confidence"))
            suffix = f" (из них {n_lc} требуют проверки — см. лог Translate)" if n_lc else ""
            self._log(f"[Wolf]   → {len(found)} строк из {path.name}{suffix}")
        self.entries.extend(found)

    def extract(self, progress_cb: Callable | None = None) -> list[dict]:
        basic, mapdata = self._find_data_dirs()
        if not basic and not mapdata:
            raise FileNotFoundError(
                f"Wolf RPG данные не найдены в {self.game_dir}\n"
                "Ожидается: Data/BasicData/*.wolf и Data/MapData/*.mps"
            )

        files = []
        if basic:
            # DataBase.wolf, CommonEvent.dat, SysDataBase.wolf
            for pattern in ["*.wolf", "*.dat"]:
                files.extend(sorted(basic.glob(pattern)))
        if mapdata:
            files.extend(sorted(mapdata.glob("*.mps")))

        self._log(f"[Wolf] Файлов для обработки: {len(files)}")

        for i, f in enumerate(files):
            if progress_cb:
                progress_cb(i + 1, len(files), -1.0, f.name)
                
            try:
                # Читаем файл один раз
                data = f.read_bytes()
                file_hash = hashlib.sha256(data).hexdigest()
            except Exception as e:
                self._log(f"  ✗ Ошибка чтения {f.name}: {e}")
                continue
            self._log(f"[Wolf] Обработка: {f.name}")
            
            # Сохраняем хэш перед экстракцией
            start_len = len(self.entries)
            self._extract_file(f)
            # Добавляем хэш ко всем извлеченным из этого файла строкам
            for j in range(start_len, len(self.entries)):
                self.entries[j]["_file_hash"] = file_hash

        # Дедупликация по (file, offset)
        seen = set()
        unique = []
        for e in self.entries:
            key = (e["file"], e["path"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        self.entries = unique

        self._log(f"[Wolf] ✓ Итого: {len(self.entries)} строк")
        return self.entries


def extract_wolf(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable | None = None,
    progress_callback: Callable | None = None,
) -> dict:
    """Главная функция извлечения для Wolf RPG Editor."""
    _log(f"[Wolf] Начало извлечения: {game_dir}", log_callback)

    ext = WolfExtractor(game_dir, log_callback)
    entries = ext.extract(progress_cb=progress_callback)

    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list[dict]] = {}
    for e in entries:
        # Убираем служебные поля (начинающиеся с _) перед сохранением,
        # кроме критичных для инжекта/карантина (_encoding, _source_path,
        # _file_hash, _low_confidence — без него pipeline.translate() не
        # сможет отфильтровать low-confidence строки, см. _scan_strings)
        clean = {}
        for k, v in e.items():
            if not k.startswith('_') or k in ('_encoding', '_source_path', '_file_hash', '_low_confidence'):
                clean[k] = v
        by_file.setdefault(clean["file"], []).append(clean)

    for fname, file_entries in by_file.items():
        # BUG FIX: was fname.replace(...) + ".json" which created DOUBLE extension
        # e.g. "Data/BasicData/DataBase.wolf" → "Data_BasicData_DataBase.wolf.json"
        # apply_dedup_to_files keys file_data by jf.name, so entries["file"] must match.
        # Strip original extension first, then add .json once.
        base = fname.replace("/", "_").replace("\\", "_").replace(" ", "_")
        stem = base.rsplit(".", 1)[0] if "." in base else base
        safe = stem + ".json"
        # Update file field in entries so deduplicator can match them
        for e in file_entries:
            e["file"] = safe
            e["_source_path"] = fname   # preserve original path for injector
        out = extract_dir / safe
        atomic_write_json(out, file_entries)

    index = {
        "game_dir":      str(game_dir),
        "game_type":     "wolf",
        "total_strings": len(entries),
        "files":         {f: len(e) for f, e in by_file.items()},
    }
    atomic_write_json(output_dir / "extract_index.json", index)
    _log(f"[Wolf] ✓ Сохранено: {len(entries)} строк", log_callback)
    return index
