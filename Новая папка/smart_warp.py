"""
smart_warp.py — умный перенос текста для RPG Maker MV/MZ.
Разбивает текст по ширине с учётом длины строки и переноса по словам/предложениям.
"""

import re
from font_analyzer import measure_text_width, load_font_table

DEFAULT_LINE_WIDTH_PX = 400
DEFAULT_MAX_LINES = 4
DEFAULT_FONT_SIZE = 26

RPGMAKER_CODES = re.compile(
    r"(?:\x1b)?\\(?:"
    r"[NnVvCcIiGgPpTtSsFfBbRr](?:\[[^\]]*\])?"
    r"|[A-Z]{2,}(?:\[[^\]]*\])?"
    r"|[\\nrtbfv]"
    r"|[!.<>^{}|]"
    r"|u[0-9A-Fa-f]{4}"
    r"|x[0-9A-Fa-f]{2}"
    r")"
)
SENTENCE_ENDS = re.compile(r"(?<=[.!?])\s+")
NEWLINE_TAGS = re.compile(r"\\n", re.IGNORECASE)


def _visible_width(text: str, table: dict[str, int] | None = None) -> int:
    """Измерить ширину текста, игнорируя escape-коды RPG Maker."""
    clean = RPGMAKER_CODES.sub("", text)
    return measure_text_width(clean, table)


def _split_tokens(text: str) -> list[str]:
    """Разбить текст на токены (слова + пробелы + коды)."""
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        m = RPGMAKER_CODES.match(text, pos)
        if m:
            tokens.append(m.group())
            pos = m.end()
            continue
        end = pos
        while end < len(text) and not RPGMAKER_CODES.match(text, end) and text[end] not in (" ", "\n"):
            end += 1
        if end > pos:
            tokens.append(text[pos:end])
            pos = end
            continue
        if text[pos] in (" ", "\n"):
            tokens.append(text[pos])
            pos += 1
            continue
        tokens.append(text[pos])
        pos += 1
    return tokens


def wrap_line(
    text: str,
    max_width_px: int = DEFAULT_LINE_WIDTH_PX,
    table: dict[str, int] | None = None,
) -> list[str]:
    """
    Разбить одну строку текста на строки, не превышающие max_width_px.
    Возвращает список строк.
    """
    if table is None:
        table = load_font_table()

    tokens = _split_tokens(text)
    lines: list[str] = []
    current: list[str] = []
    current_width = 0

    for token in tokens:
        if token == "\n":
            lines.append("".join(current).rstrip())
            current = []
            current_width = 0
            continue

        token_width = _visible_width(token, table)

        if current_width + token_width > max_width_px and current:
            lines.append("".join(current).rstrip())
            current = []
            current_width = 0
            if token.strip() == "":
                continue

        current.append(token)
        current_width += token_width

    if current:
        lines.append("".join(current).rstrip())

    return [line for line in lines if line] or [""]


def wrap_paragraph(
    text: str,
    max_width_px: int = DEFAULT_LINE_WIDTH_PX,
    max_lines: int | None = None,
    table: dict[str, int] | None = None,
    line_separator: str = "\\n",
) -> str:
    """
    Полный перенос параграфа для RPG Maker.
    Возвращает строку с разделителями line_separator.

    BUG FIX (найдено внешним аудитом, подтверждено эмпирически): раньше
    max_lines по умолчанию был DEFAULT_MAX_LINES=4, и pipeline._apply_warp()
    вызывал эту функцию НЕ передавая max_lines явно — то есть каждый
    перевод длиннее 4 строк по ширине max_width_px молча обрезался с "…",
    теряя содержимое безвозвратно при инъекции (для реалистичной строки
    466 символов на выходе оставалось 108 — потеря ~77%). max_lines=None
    означает "не обрезать вообще" — это теперь безопасный default.
    Обрезка остаётся доступна ЯВНО (max_lines=N), и именно так её
    по-прежнему используют test_new_features.py/test_roundtrip.py.
    """
    if table is None:
        table = load_font_table()

    parts = NEWLINE_TAGS.split(text)
    all_lines: list[str] = []

    for part in parts:
        wrapped = wrap_line(part.strip(), max_width_px, table)
        all_lines.extend(wrapped)

    # BUG FIX: wrap_line("") returns [""] as a sentinel — extend() adds it to all_lines.
    # These empty strings inflate len(all_lines), cause premature max_lines truncation,
    # and produce leading/trailing \\n separators in the output (blank lines in dialog).
    all_lines = [line for line in all_lines if line]
    if not all_lines:
        return text

    if max_lines is not None and len(all_lines) > max_lines:
        all_lines = all_lines[:max_lines]
        last = all_lines[-1].rstrip()
        if not last.endswith("…") and not last.endswith("..."):
            ellipsis = "…"
            combined = last + ellipsis
            if table and _visible_width(combined, table) > max_width_px:
                # BUG FIX: trim by whole tokens — never cut into RPG escape codes.
                tokens = _split_tokens(last)
                while tokens and _visible_width("".join(tokens).rstrip() + ellipsis, table) > max_width_px:
                    tokens.pop()
                last = "".join(tokens).rstrip()
            all_lines[-1] = last + ellipsis

    return line_separator.join(all_lines)


def wrap_by_sentences(
    text: str,
    max_width_px: int = DEFAULT_LINE_WIDTH_PX,
    max_lines: int = DEFAULT_MAX_LINES,
    table: dict[str, int] | None = None,
    line_separator: str = "\\n",
) -> str:
    """
    Перенос с приоритетом разбивки по предложениям.
    Пытается завершить строку на границе предложения, если это возможно.
    """
    if table is None:
        table = load_font_table()

    sentences = SENTENCE_ENDS.split(text.strip())
    lines: list[str] = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = (current + " " + sentence).strip() if current else sentence
        candidate_lines = wrap_line(candidate, max_width_px, table)
        if len(candidate_lines) <= 1:
            current = candidate
        else:
            if current:
                lines.extend(wrap_line(current, max_width_px, table))
            current = sentence

    if current:
        lines.extend(wrap_line(current, max_width_px, table))

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        if not last.endswith("…") and not last.endswith("..."):
            ellipsis = "…"
            if table and _visible_width(last + ellipsis, table) > max_width_px:
                tokens = _split_tokens(last)
                while tokens and _visible_width("".join(tokens).rstrip() + ellipsis, table) > max_width_px:
                    tokens.pop()
                last = "".join(tokens).rstrip()
            lines[-1] = last + ellipsis

    return line_separator.join(lines)


def analyze_wrapping(
    text: str,
    max_width_px: int = DEFAULT_LINE_WIDTH_PX,
    table: dict[str, int] | None = None,
) -> dict:
    """Детальный анализ переноса: показывает все строки и их ширины."""
    if table is None:
        table = load_font_table()

    lines = wrap_line(text, max_width_px, table)
    result = []
    for line in lines:
        w = _visible_width(line, table)
        result.append({
            "text": line,
            "width_px": w,
            "overflow": w > max_width_px,
            "fill_pct": round(w / max_width_px * 100, 1),
        })
    return {
        "original": text,
        "max_width_px": max_width_px,
        "line_count": len(lines),
        "lines": result,
    }


if __name__ == "__main__":
    test = (
        "This is a very long sentence that should be wrapped across multiple lines "
        "because it exceeds the maximum allowed width for RPG Maker dialog boxes."
    )
    print("=== Smart Warp Test ===")
    result = analyze_wrapping(test, max_width_px=400)
    for i, line in enumerate(result["lines"], 1):
        overflow = " [OVERFLOW]" if line["overflow"] else ""
        print(f"  Line {i}: {line['text']!r}")
        print(f"    Width: {line['width_px']}px / {result['max_width_px']}px ({line['fill_pct']}%){overflow}")
    print()
    wrapped = wrap_paragraph(test, max_width_px=400, max_lines=4)
    print("Wrapped output:")
    print(wrapped)
