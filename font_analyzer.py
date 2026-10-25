"""
font_analyzer.py — измерение ширины символов для RPG Maker шрифтов.
Генерирует JSON-таблицу ширин символов.
"""

import json
from pathlib import Path

from engine.file_utils import atomic_write_json

DEFAULT_FONT_TABLE_PATH = Path(__file__).parent / "font_widths.json"

DEFAULT_CHAR_WIDTH = 14

# BUG FIX: эти два множества были объявлены, но нигде не использовались —
# get_char_width() ниже падал сразу в грубый 2-уровневый fallback
# (cp > 0x2000 → 24, иначе плоские 14 для ВСЕГО остального, включая всю
# кириллицу). Учитывая, что перевод у нас в основном идёт на русский, а
# Smart Warp (apply_warp=True по умолчанию, см. config.py) активно
# перено­сит строки почти для всех движков — это был не периферийный
# нюанс, а основной путь измерения ширины для большинства реального
# переведённого текста. Теперь оба множества реально используются (см.
# get_char_width) и дополнены кириллицей по общепринятым типографским
# пропорциям начертаний (те же соображения, что уже применены к M/W/m/w
# и i/l/j выше — не точные пиксели конкретного шрифта, а разумная
# категория "уже/шире среднего").
NARROW_CHARS = set("il|!.,;:' \t\n\r" + "ьъІіЬЪ")
WIDE_CHARS = set("mwMW@" + "шщжфмыюШЩЖФМЫЮ")

BUILT_IN_WIDTHS: dict[str, int] = {
    " ": 6,
    "!": 6,
    '"': 8,
    "#": 14,
    "$": 12,
    "%": 14,
    "&": 14,
    "'": 6,
    "(": 8,
    ")": 8,
    "*": 10,
    "+": 14,
    ",": 6,
    "-": 8,
    ".": 6,
    "/": 10,
    "0": 12, "1": 12, "2": 12, "3": 12, "4": 12,
    "5": 12, "6": 12, "7": 12, "8": 12, "9": 12,
    ":": 6,
    ";": 6,
    "<": 14,
    "=": 14,
    ">": 14,
    "?": 10,
    "@": 16,
    "A": 14, "B": 14, "C": 14, "D": 14, "E": 12,
    "F": 12, "G": 14, "H": 14, "I": 6,  "J": 10,
    "K": 14, "L": 12, "M": 16, "N": 14, "O": 14,
    "P": 12, "Q": 14, "R": 14, "S": 12, "T": 12,
    "U": 14, "V": 14, "W": 16, "X": 14, "Y": 12,
    "Z": 12,
    "[": 8,
    "\\": 10,
    "]": 8,
    "^": 14,
    "_": 12,
    "`": 8,
    "a": 12, "b": 12, "c": 10, "d": 12, "e": 12,
    "f": 8,  "g": 12, "h": 12, "i": 6,  "j": 6,
    "k": 12, "l": 6,  "m": 16, "n": 12, "o": 12,
    "p": 12, "q": 12, "r": 8,  "s": 10, "t": 8,
    "u": 12, "v": 12, "w": 16, "x": 12, "y": 12,
    "z": 10,
    "{": 8,
    "|": 6,
    "}": 8,
    "~": 14,
}


def load_font_table(path: Path | None = None) -> dict[str, int]:
    """Загрузить таблицу ширин из JSON-файла. Если файл не найден — вернуть встроенную."""
    target = path or DEFAULT_FONT_TABLE_PATH
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): int(v) for k, v in data.items()}
        except Exception as e:
            print(f"[FontAnalyzer] Ошибка загрузки таблицы {target}: {e}")
    return dict(BUILT_IN_WIDTHS)


def save_font_table(table: dict[str, int], path: Path | None = None) -> None:
    """Сохранить таблицу ширин в JSON-файл."""
    target = path or DEFAULT_FONT_TABLE_PATH
    atomic_write_json(target, dict(sorted(table.items())))
    print(f"[FontAnalyzer] Таблица сохранена: {target}")


def get_char_width(char: str, table: dict[str, int] | None = None) -> int:
    """Получить ширину одного символа в пикселях."""
    if table is None:
        table = BUILT_IN_WIDTHS
    if char in table:
        return table[char]
    # BUG FIX: NARROW_CHARS/WIDE_CHARS были объявлены, но никогда не
    # проверялись здесь — любой символ не из основной таблицы (вся
    # кириллица, если только не передана своя table) сразу падал в грубый
    # cp > 0x2000 тест, полностью игнорируя эти категории.
    if char in NARROW_CHARS:
        return 8
    if char in WIDE_CHARS:
        return 18
    cp = ord(char)
    if cp > 0x2000:
        return 24
    return DEFAULT_CHAR_WIDTH


def measure_text_width(text: str, table: dict[str, int] | None = None) -> int:
    """Измерить полную ширину строки в пикселях."""
    return sum(get_char_width(c, table) for c in text)


def analyze_string(text: str, table: dict[str, int] | None = None) -> dict:
    """Вернуть детальный анализ строки."""
    chars = []
    total = 0
    for c in text:
        w = get_char_width(c, table)
        total += w
        chars.append({"char": c, "width": w, "cumulative": total})
    return {
        "text": text,
        "total_width": total,
        "char_count": len(text),
        "average_char_width": round(total / len(text), 2) if text else 0,
        "chars": chars,
    }


def generate_default_table() -> dict[str, int]:
    """Сгенерировать и сохранить таблицу ширин по умолчанию."""
    table = dict(BUILT_IN_WIDTHS)
    save_font_table(table)
    return table


if __name__ == "__main__":
    print("=== Font Analyzer ===")
    table = generate_default_table()
    test = "Hello, World! This is a test."
    result = analyze_string(test, table)
    print(f"Строка: {result['text']}")
    print(f"Полная ширина: {result['total_width']} px")
    print(f"Символов: {result['char_count']}")
    print(f"Средняя ширина: {result['average_char_width']} px/символ")
