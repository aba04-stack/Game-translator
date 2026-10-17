"""
text_filter.py — фильтрация текста для перевода.
Определяет, нужно ли переводить строку.
"""

from __future__ import annotations

import re

# RPG Maker / visual novel escape codes that must be preserved.
RPGM_ESCAPE = re.compile(
    r"(?:\x1b)?\\(?:"
    r"[NnVvCcIiGgPpTtSsFfBbRr](?:\[[^\]]*\])?"
    r"|[A-Z]{2,}(?:\[[^\]]*\])?"
    r"|[\\nrtbfv]"
    r"|[!.<>^{}|]"
    r"|u[0-9A-Fa-f]{4}"
    r"|x[0-9A-Fa-f]{2}"
    r")"
)

# KAG (Kirikiri Action Game) tags: [l] [r] [p] [cm] [endscene attr=val] etc.
# These are pure control tags, never translatable text.
KAG_TAG_RE = re.compile(r'\[[a-z_][a-z0-9_]*(?:\s[^\]]{0,80})?\]', re.IGNORECASE)

# Alias used by older code paths.
RPGM_CODE_INLINE = RPGM_ESCAPE

# Text that should never be translated.
SKIP_EXACT = {
    "", "null", "undefined", "true", "false",
    ".", "!", "?", ",", ":", ";", "-", "—", "…",
}

# Characters that indicate source language text still remains.
HAS_CJK = re.compile(r"[぀-ヿ一-鿿가-힯]")
# BUG FIX: полноширинная латиница (Ａ-Ｚ, ａ-ｚ — U+FF21-FF3A/U+FF41-FF5A)
# отсутствовала здесь. Это распространённое японское типографское соглашение
# (fullwidth roman) для визуального выравнивания латиницы с окружающими
# японскими символами — звукоподражания, стилизованные надписи, бренды.
# Строка, состоящая ТОЛЬКО из такой латиницы, молча классифицировалась как
# "no_letters" и никогда не попадала в extract — без единого следа в логах.
HAS_LETTERS = re.compile(r"[a-zA-Zа-яёА-ЯЁ぀-ヿ一-鿿가-힯\uFF21-\uFF3A\uFF41-\uFF5A]")

# Strings made only of whitespace, digits, punctuation, brackets and backslashes.
ONLY_CODES = re.compile(r"^[\s0-9\[\]{}|!<>.^\\]+$")

# Bracketed category-tag identifier: 【tag】value with NO space/punctuation
# after the closing bracket — e.g. "【頬】薄い赤面" ("[Cheek]light-blush").
# See BUG FIX comment at its use site in classify_text_for_translation.
_BRACKET_TAG_RE = re.compile(r"^【[^】]{1,10}】(\S+)$")
_BRACKET_TAG_PUNCT_RE = re.compile(r"[。！？!?、,，.\s]")


def classify_text_for_translation(text: str) -> tuple[bool, str]:
    """Вернуть (нужно_переводить, причина_если_нет)."""
    if not isinstance(text, str):
        return False, "not_string"

    t = text.strip()
    if not t:
        return False, "empty"

    if t.lower() in SKIP_EXACT:
        return False, "skip_exact"

    if len(t) == 1 and not HAS_LETTERS.search(t):
        return False, "too_short"

    # BUG FIX: real-world crash — an MZ Plugin Command argument value like
    # "巨乳JK_立ち絵" (an image/character-sprite identifier used internally by
    # a picture/expression-management plugin, e.g. as a dynamically created
    # JS property/variable name) was being extracted and translated as if it
    # were narrative text. The plugin's own script elsewhere referenced this
    # exact identifier as a bare, unquoted JS token; once RPGMTL translated
    # and re-injected the argument value, that reference could no longer
    # find it, and the game crashed at runtime with
    # "ReferenceError: 巨乳JK_立ち絵 is not defined". Natural Japanese dialogue,
    # names, and system text essentially never contain a literal ASCII
    # underscore — it is a near-universal marker of a filename, key, or
    # identifier, not prose. Reject any string containing one.
    if "_" in t:
        return False, "identifier_like"

    # BUG FIX: real-world crash — a face/expression-management plugin's
    # lookup key like "【頬】薄い赤面" ("[Cheek]light-blush": a bracketed
    # category tag immediately followed by a specific state name, no space,
    # no punctuation — the standard convention for "layered facial
    # expression" asset systems in RPG Maker/KAG-based games) was extracted
    # and translated as narrative text. The plugin's internal animation
    # registry still expected the original Japanese key elsewhere, so after
    # translation it could no longer find a match: "Error: Animation not
    # found: 【Щёки】Лёгкий румянец" at runtime — the requested expression
    # silently failed to play instead of crashing outright.
    #
    # Narrowly scoped to avoid rejecting legitimate stylistic use of 【】:
    # only strings that (a) START with the bracket pair at position 0 and
    # (b) have a non-empty remainder with NO sentence punctuation or
    # whitespace anywhere and (c) are short overall. Mid-sentence emphasis
    # ("先生は【重要】と言った。") and full sentences that merely open with
    # a 【label】 ("【注意】このダンジョンは危険です。") keep their
    # trailing punctuation/spacing and are correctly left translatable.
    _m = _BRACKET_TAG_RE.match(t)
    if _m and len(t) <= 30 and not _BRACKET_TAG_PUNCT_RE.search(_m.group(1)):
        return False, "bracket_tag_identifier"

    clean = RPGM_ESCAPE.sub("", t).strip()
    # Also strip KAG-style [tag] and [tag attr=val] control markers
    clean = KAG_TAG_RE.sub("", clean).strip()
    if not clean:
        return False, "only_codes"

    if ONLY_CODES.fullmatch(clean):
        return False, "only_codes"

    if not HAS_LETTERS.search(clean):
        return False, "no_letters"

    # BUG FIX: short all-caps Latin abbreviations (HP, MP, EXP, ATK, DEF, etc.)
    # are universal RPG stat labels — they don't need translation.
    # Match 1-5 uppercase ASCII letters with optional trailing digit (HP2, SP1).
    if re.fullmatch(r'[A-Z]{1,5}[0-9]?', clean):
        return False, "abbreviation"

    # Тот же случай, но полноширинной латиницей (ＭＰ, ＨＰ２ и т.п.) — то
    # же стилистическое соглашение, что описано в BUG FIX у HAS_LETTERS выше.
    if re.fullmatch(r'[\uFF21-\uFF3A]{1,5}[\uFF10-\uFF19]?', clean):
        return False, "abbreviation"

    return True, "ok"


def should_translate(text: str) -> bool:
    """Вернуть True, если строку нужно переводить."""
    ok, _reason = classify_text_for_translation(text)
    return ok


def clean_for_translation(text: str) -> str:
    """Минимальная очистка — убрать лишние пробелы, сохранить структуру."""
    return text.strip()


def is_japanese(text: str) -> bool:
    """Проверить, содержит ли текст японские символы."""
    return bool(HAS_CJK.search(text))
