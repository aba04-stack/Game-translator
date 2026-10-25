"""
deduplicator.py — дедупликация строк перед переводом.

Ключевая оптимизация: в RPG Maker играх до 70% строк повторяются
(системные сообщения, имена, стандартные фразы).
Переводим каждую уникальную строку ОДИН РАЗ, сохраняем в кэш,
применяем ко всем вхождениям.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Callable


def _log(msg: str, cb: Callable[[str], None] | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


def text_key(text: str) -> str:
    """Нормализованный ключ строки для дедупликации."""
    return text.strip()


class DeduplicationMap:
    """
    Хранит маппинг: original_text → список (file, path) где встречается.
    Позволяет переводить каждый уникальный текст ровно один раз.
    """

    def __init__(self):
        self._map: dict[str, list[tuple[str, str]]] = {}
        self._translations: dict[str, str] = {}

    def add(self, text: str, file: str, path: str):
        if not text or not text.strip():
            return  # пустые строки не дедуплицируем
        key = text_key(text)
        if key not in self._map:
            self._map[key] = []
        self._map[key].append((file, path))

    def unique_texts(self) -> list[str]:
        return list(self._map.keys())

    def set_translation(self, original: str, translated: str):
        self._translations[text_key(original)] = translated

    def get_translation(self, original: str) -> str | None:
        return self._translations.get(text_key(original))

    def occurrences(self, original: str) -> list[tuple[str, str]]:
        return self._map.get(text_key(original), [])

    @property
    def total_strings(self) -> int:
        return sum(len(v) for v in self._map.values())

    @property
    def unique_count(self) -> int:
        return len(self._map)

    @property
    def translated_count(self) -> int:
        return len(self._translations)

    @property
    def savings_pct(self) -> float:
        if self.total_strings == 0:
            return 0.0
        return round((1 - self.unique_count / self.total_strings) * 100, 1)


def build_dedup_map(
    all_entries: list[dict],
    log_cb: Callable[[str], None] | None = None,
) -> DeduplicationMap:
    """Построить карту дедупликации из списка всех записей."""
    dm = DeduplicationMap()
    for entry in all_entries:
        text = entry.get("original", "")
        file_ = entry.get("file", "")
        path = entry.get("path", "")
        if text and file_ and path:
            dm.add(text, file_, path)

    _log(
        f"[Dedup] Всего строк: {dm.total_strings}, "
        f"уникальных: {dm.unique_count}, "
        f"экономия: {dm.savings_pct:.0f}%",
        log_cb,
    )
    return dm


def apply_dedup_to_files(
    dm: DeduplicationMap,
    extract_dir: Path,
    log_cb: Callable[[str], None] | None = None,
) -> int:
    """
    Применить переводы из DeduplicationMap к файлам.
    Возвращает количество заполненных записей.
    """
    file_data: dict[str, list[dict]] = {}
    # BUG FIX: раньше для КАЖДОГО вхождения каждого уникального текста ниже
    # делался линейный поиск нужной entry по path внутри ВСЕХ entries файла
    # (вложенный `for entry in entries: if entry.get("path") == path...`).
    # Для файла с N строками это O(N) на каждое вхождение, а вхождений в
    # том же файле обычно порядка N — суммарно O(N²). На синтетическом
    # тесте (один файл, N строк, ~40% уникальных — типичная дедупликация):
    # N=3000 → 0.2с, N=6000 → 0.8с, N=12000 → 3.0с, N=24000 → 11с — время
    # растёт в ~4 раза при удвоении N, классическая квадратичная картина.
    # Для больших игр (объёмный CommonEvents.json/большая карта на тысячи
    # строк в одном файле) это реально заметно на глаз. Строим path→entry
    # индекс один раз на файл (O(N)) — дальше поиск O(1), весь проход O(N).
    path_index: dict[str, dict[str, dict]] = {}

    for jf in extract_dir.glob("*.json"):
        if jf.name.endswith("_translated.json"):
            continue
        try:
            with open(jf, "r", encoding="utf-8") as f:
                entries = json.load(f)
            file_data[jf.name] = entries
            idx: dict[str, dict] = {}
            for entry in entries:
                p = entry.get("path")
                if p is not None:
                    # setdefault, не idx[p]=entry — сохраняем то же поведение
                    # "первый выигрывает при коллизии path", что и было у
                    # исходного линейного поиска (в норме path уникален
                    # внутри файла, это чисто защитный случай).
                    idx.setdefault(p, entry)
            path_index[jf.name] = idx
        except Exception as e:
            _log(f"  [Dedup] ✗ Ошибка чтения {jf.name}: {e}", log_cb)

    applied = 0
    changed_files: set[str] = set()
    for original in dm.unique_texts():
        translated = dm.get_translation(original)
        if not translated:
            continue
        for file_, path in dm.occurrences(original):
            entry = path_index.get(file_, {}).get(path)
            if entry is None:
                continue
            # Та же проверка, что была в линейном поиске — path совпал,
            # но на всякий случай сверяем и original (защита от рассинхрона
            # между dedup-картой и содержимым файла).
            if entry.get("original", "").strip() == original:
                entry["translated"] = translated
                applied += 1
                changed_files.add(file_)

    for fname, entries in file_data.items():
        out_path = extract_dir / f"{Path(fname).stem}_translated.json"
        # BUG FIX: skip writing if nothing changed AND file already exists
        if fname not in changed_files and out_path.exists():
            continue
        # BUG FIX: атомарная запись — прямой open() при сбое оставлял пустой JSON
        content = json.dumps(entries, ensure_ascii=False, indent=2).encode("utf-8")
        tmp_fd, tmp_path = tempfile.mkstemp(dir=extract_dir, suffix=".tmp", prefix=".rpgmtl_")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(content)
            os.replace(tmp_path, out_path)
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            _log(f"  [Dedup] ✗ Ошибка записи {out_path.name}: {e}", log_cb)

    _log(f"[Dedup] Применено переводов: {applied} из {dm.total_strings}", log_cb)
    return applied
