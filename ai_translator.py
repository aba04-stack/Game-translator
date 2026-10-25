"""
ai_translator.py — контекстно-осведомлённый пакетный AI-переводчик.

Лучшие практики от топ-инструментов:
- Glossary injection в системный промпт (Translator++)
- Context-aware batching: speaker + dialog вместе (SakuraTranslator)
- Numbered response parsing с fallback (MTool)
- Retry с экспоненциальной задержкой
- Защита RPG Maker escape-кодов от порчи
- Умный half-batch retry при полном сбое
- Валидация RPG-кодов после перевода
- Rate limiting между батчами
- Кооперативная отмена (cancel_event)
"""

import time
import re
from dataclasses import dataclass, field
from typing import Callable

from error_utils import console_print

RPG_CODE_PATTERN = re.compile(
    r"(?:\x1b)?\\(?:"
    r"[NnVvCcIiGgPpTtSsFfBbRr](?:\[[^\]]*\])?"  # \N[1], \V[3], \C[2], ...
    r"|[A-Z]{2,}(?:\[[^\]]*\])?"               # \NW[Name], \MC[...], ...
    r"|[\\nrtbfv]"                               # \\, \n, \r, \t, \b, \f, \v
    r"|[!.<>^{}|]"                                 # \!, \., \<, \>, \^, \{, \}, \|
    r"|u[0-9A-Fa-f]{4}"                            # \uFFFF
    r"|x[0-9A-Fa-f]{2}"                            # \xFF
    r")"
)

# Engine-aware protected syntax. RPG Maker already has a dedicated parser above;
# these additional patterns cover the formats that commonly appear in Unity,
# Godot, Ren'Py, Tyrano/Kirikiri and localization tables. They are deliberately
# conservative: only unmistakable formatting/interpolation tokens are protected.
_ENGINE_TOKEN_PATTERNS = {
    "unity": re.compile(r"(?:\{\{[^{}]+\}\}|\{[A-Za-z_][\w.]*\}|\{\d+(?::[^{}]+)?\}|\$\{[^{}]+\}|%[+#0\- ]*\d*(?:\.\d+)?[diuoxXfFeEgGcs]|</?[A-Za-z][^<>]*>|\\[nrt])"),
    "godot": re.compile(r"(?:\{[A-Za-z_][\w.]*\}|%[sdif]|\[(?:/?(?:b|i|u|s|code|url|center|left|right|fill|indent)|color(?:=[^\]]+)?)\]|\\[nrt])"),
    "renpy": re.compile(r"(?:\{[^{}\n]+\}|\[[A-Za-z_][\w.]*\]|\\[nrt]|\$[A-Za-z_][\w.]*)"),
    "tyrano": re.compile(r"(?:\[[A-Za-z_][^\]]*\]|\\[A-Za-z_][\w-]*(?:\([^)]*\))?)"),
    "kirikiri": re.compile(r"(?:\[[A-Za-z_][^\]]*\]|\\[A-Za-z_][\w-]*(?:\([^)]*\))?)"),
    "wolf": re.compile(r"(?:\\[A-Za-z_][\w-]*(?:\[[^\]]*\])?)"),
    # BUG FIX: прежний шаблон `\\[A-Za-z][^\s]{0,20}` после обратного слэша «съедал»
    # до 20 следующих не-пробельных символов. В японском тексте (пробелов нет)
    # токен `\C[2]勇者\C[0]が来た` захватывал ВЕСЬ текст, а в русском переводе
    # с пробелами заканчивался раньше — списки токенов расходились даже у
    # идеального перевода: ложный critical в QA, блокировка Inject и отказ
    # принять строку при переводе для КАЖДОЙ строки с кодами в VX Ace-играх.
    # Теперь токен — это именно код: `\C[2]`, `\FS[22]`, одиночная буква (`\G`,
    # `\N`) или спецсимвол (`\{ \} \$ \. \| \! \> \< \^ \\`).
    "vxace": re.compile(r"(?:\\(?:[A-Za-z]+\[[^\]\s]*\]|[A-Za-z]|[{}$.|!<>^\\]))"),
    # BUG FIX (найдено по прямому запросу — проверить, подходит ли режим
    # каждому движку): rpgmv/rpgmz были ЕДИНСТВЕННЫМИ из 9 движков БЕЗ
    # записи в этом словаре. _validate_codes (отдельная, более ранняя
    # проверка внутри _validate_engine_tokens) покрывает КЛАССИЧЕСКИЕ RPG
    # Maker коды (\C[n], \N[n] и т.п.) — но множество популярных плагинов
    # (Yanfly Engine Plugins, VisuStella MZ и другие, которыми пользуется
    # огромная доля реальных MV/MZ игр) вводят СВОИ плейсхолдеры поверх
    # движка: позиционные %1/%2 (например "Welcome, %1! You have %2 gold.")
    # и JS-template-подобные {playerName}. Ни то, ни другое не входит в
    # RPGMAKER_CODES — перевод, полностью потерявший такой плейсхолдер,
    # проходил _validate_engine_tokens как валидный, эмпирически
    # подтверждено. Добавляем тот же уровень защиты, что уже был у ВСЕХ
    # остальных 8 движков.
    "rpgmv": re.compile(r"(?:%\d+|\{[A-Za-z_][\w.]*\}|\\[nrt])"),
    "rpgmz": re.compile(r"(?:%\d+|\{[A-Za-z_][\w.]*\}|\\[nrt])"),
}


def _engine_token_pattern(game_type: str):
    return _ENGINE_TOKEN_PATTERNS.get((game_type or "").lower())


def _extract_engine_tokens(text: str, game_type: str) -> list[str]:
    """Return protected non-RPG tokens in source order."""
    pattern = _engine_token_pattern(game_type)
    if not pattern:
        return []
    return [m.group(0) for m in pattern.finditer(text)]


def _validate_engine_tokens(original: str, translated: str, game_type: str) -> bool:
    """Validate RPG escape codes plus engine-specific placeholders/tags."""
    if not _validate_codes(original, translated):
        return False
    return _extract_engine_tokens(original, game_type) == _extract_engine_tokens(translated, game_type)

# Placeholder format §N§: § (U+00A7) is not Latin/Japanese/RPG-code.
# AI models reliably leave § untouched. 5× shorter than __RPGMTL_CODE_XXXX__.
PLACEHOLDER_PREFIX = "§"
PLACEHOLDER_SUFFIX = "§"
_PLACEHOLDER_RE       = re.compile(r"§\d+§")
# Fix AI artifacts around §N§ tokens:
#   §0 §  §0\n§  § 0§  § 0 §  §0§  →  §0§
_SPLIT_PLACEHOLDER_RE = re.compile(r"§\s*(\d+)\s*§")

# ── Cleaning patterns for model artifacts ────────────────────────────────────
_THINK_RE        = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MD_BOLD_RE      = re.compile(r"\*{1,3}(.+?)\*{1,3}")   # **bold** / *italic* / ***both***
_MD_CODE_RE      = re.compile(r"`([^`]*)`")              # `inline code`
_COMMENTARY_START_RE = re.compile(
    r"^(Note[:\s]|Translation[:\s]|Примечание[:\s]|"
    r"Here\s+(?:is|are)\b|I\s+(?:translated|used)\b|This\s+(?:is|means)\b|"
    r"Translator'?s?\s+note|Above|Additionally|—\s*Note)",
    re.IGNORECASE,
)
# _parse_numbered: formats produced by AI instead of the requested [N] brackets
_NUM_BRACKET_RE  = re.compile(r"^\[(\d+)\]\s*(.*)")        # [1] text
_NUM_LIST_RE     = re.compile(r"^(\d+)[.)]\s+(.*)")         # 1. text  /  1) text
_NUM_PREFIX_RE   = re.compile(r"^(?:\[\d+\]\s*|\d+[.)]\s+)")  # strip either prefix
# AI sometimes writes § О § (Cyrillic О) or § 0 § (spaces) — normalize all variants
# Covers: §\s*[0-9О0Ο]+\s*§ where О is Cyrillic, 0 is fullwidth, Ο is Greek
_PLACEHOLDER_FUZZY_RE = re.compile(
    r"§\s*([0-9\uff10-\uff19]+|[О0Ο]+|\d+)\s*§",
    re.UNICODE,
)
# Digit normalization map for fullwidth/look-alike chars
_DIGIT_MAP = str.maketrans("０１２３４５６７８９ОοΟ", "0123456789000")


def _normalize_placeholders(text: str) -> str:
    """Normalize §N§ look-alike variants AI might produce:
    §0§, § 0 §, §０§, §О§(Cyrillic), §Ο§(Greek) → §0§"""
    def _fix(m: re.Match) -> str:
        raw = m.group(1).translate(_DIGIT_MAP)
        digits = re.sub(r"[^0-9]", "", raw)
        return f"§{digits}§" if digits else m.group(0)
    return _PLACEHOLDER_FUZZY_RE.sub(_fix, text)


def _clean_translation(text: str) -> str:
    """Remove model artifacts from a single translated string:
    - <think>…</think> blocks (DeepSeek-R1, Qwen3)
    - Markdown bold/italic/code (DeepSeek-chat, GPT-4o sometimes)
    - Trailing commentary lines the model appended after the translation
    - Normalize §N§ look-alikes (§ 0 §, §О§, §０§ → §0§)
    """
    # 0. Normalize §N§ variants BEFORE any other processing
    text = _normalize_placeholders(text)
    # 1. Strip think blocks
    text = _THINK_RE.sub("", text)
    # 2. Strip markdown formatting but keep inner text
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    # 3. Trim trailing commentary (model sometimes explains its translation)
    lines = text.split("\n")
    clean: list[str] = []
    for line in lines:
        if _COMMENTARY_START_RE.match(line.strip()):
            break
        clean.append(line)
    return "\n".join(clean).strip()


# ── Модели по умолчанию — ЕДИНЫЙ источник правды ─────────────────────────────
# Раньше имя модели было продублировано в TranslatorConfig.get_model, QUALITY_MODES
# и GUI — и расходилось. Проверено по официальным страницам 2026-10-22:
#   claude   — platform.claude.com/docs/en/models/overview: актуальны claude-haiku-5-5 /
#              claude-sonnet-5-5 / claude-opus-5-5 / claude-fable-5-1; claude-3-5-*-latest
#              в списке доступных моделей нет.
#   gemini   — gemini-2.0-flash закрыт 2026-06-01 (замена gemini-3.6-flash),
#              gemini-1.5-pro закрыт 2025-09-24.
#   groq     — llama-3.1-8b-instant и llama-3.3-70b-versatile закрыты 2026-08-16
#              (замены openai/gpt-oss-20b и openai/gpt-oss-120b), llama-3.1-70b-versatile
#              снят ещё раньше (console.groq.com/docs/deprecations).
#   deepseek — алиасы deepseek-chat / deepseek-reasoner закрыты 2026-07-24, замены
#              deepseek-v4-flash / deepseek-v4-pro (по вторичным источникам, ссылающимся
#              на документацию DeepSeek).
#   openai   — gpt-4o / gpt-4o-mini ещё работают, но OpenAI объявила о снятии
#              2027-01-14. Идентификатор преемника однозначно подтвердить не удалось,
#              поэтому дефолт НЕ менялся — см. MODEL_LIFECYCLE ниже.
# Любую модель можно задать вручную в настройках — дефолт нужен, только если поле пустое.
DEFAULT_MODELS = {
    "openai":   "gpt-4o-mini",
    "gemini":   "gemini-3.6-flash",
    "claude":   "claude-haiku-5-5",
    "groq":     "openai/gpt-oss-20b",
    "deepseek": "deepseek-v4-flash",
    "mock":     "mock",
}


@dataclass
class TranslatorConfig:
    provider: str = "openai"
    api_key: str = ""
    model: str = ""
    source_lang: str = "Japanese"
    target_lang: str = "Russian"
    batch_size: int = 25
    max_workers: int = 3  # Количество параллельных потоков
    max_retries: int = 3
    retry_delay: float = 2.0
    request_delay: float = 0.5
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: int = 60
    glossary_block: str = ""
    extra_instructions: str = ""
    use_context: bool = True
    previous_context_block: str = ""  # Для скользящего окна контекста
    base_url: str = ""  # Custom endpoint (LM Studio, Ollama, etc.)
    use_global_tm: bool = True  # Кросс-проектная Translation Memory
    game_type: str = "unknown"
    style_profile: str = ""
    # Общий «ящик» фатальной ошибки: dict копируется ПО ССЫЛКЕ вместе с конфигом
    # (TranslatorConfig(**cfg.__dict__)), поэтому все батчи и потоки одного прогона видят
    # один и тот же. Заполняется при постоянной ошибке API — см. _classify_permanent_error.
    fatal_error_box: dict = field(default_factory=dict, repr=False, compare=False)

    def get_engine_instructions(self) -> str:
        try:
            from engine.translation_profiles import build_instructions
            return build_instructions(self.game_type, self.style_profile)
        except Exception:
            return "ENGINE PROFILE: unknown\nPreserve all game structure, tags, placeholders, IDs and code; translate only human-readable text."

    def get_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, DEFAULT_MODELS["openai"])

    def get_system_prompt(self) -> str:
        parts = [
            "You are a professional translator specializing in Japanese video games.",
            f"Translate from {self.source_lang} to {self.target_lang}.",
            "",
            "CRITICAL RULES — follow every rule exactly or the game will break:",
            r"1. §N§ tokens (§0§, §1§, §2§ …) are PROTECTED GAME CODES."
            r" Copy each one EXACTLY to the same relative position in the translation."
            r" NEVER translate, rename, split, or delete them."
            r" Wrong: '§ 0 §'  '§0 §'  '§О§' (Cyrillic O).  Correct: '§0§'.",
            r"2. Preserve ALL engine escape codes UNCHANGED in the same position:"
            r" \N[1] \C[2] \V[3] \I[4] \B \> \< \! \. \^ \\ | etc."
            r" Example input: '\C[2]勇者\C[0]は言った。'  "
            r"Correct output: '\C[2]Герой\C[0] сказал.'  "
            r"Wrong output: 'Герой сказал.'  (codes stripped — FORBIDDEN)",
            r"3. Preserve line breaks encoded as \n in strings.",
            "4. Output ONLY the translation. No commentary, notes, markdown bold (**), or explanations.",
            "5. Do NOT translate proper nouns listed in the glossary.",
            "6. Match the register and tone (casual speech, formal, dramatic, etc.).",
            "7. If a line is already in the target language or is untranslatable, return it as-is.",
            "8. Lines may include context hints like (Speaker: Name) — use for tone consistency but do NOT output them.",
            "9. Keep character speech styles consistent across all lines.",
            "10. ENGINE-SPECIFIC RULES BELOW OVERRIDE GENERIC ASSUMPTIONS ABOUT FILE FORMAT.",
        ]

        parts.append("")
        parts.append(self.get_engine_instructions())

        if self.glossary_block:
            parts.append("")
            parts.append(self.glossary_block)

        if self.previous_context_block:
            parts.append("\n--- PREVIOUS LINES CONTEXT (FOR REFERENCE ONLY, DO NOT TRANSLATE THESE AGAIN) ---")
            parts.append(self.previous_context_block)
            parts.append("----------------------------------------------------------------------------------")

        if self.extra_instructions:
            parts.append("")
            parts.append(self.extra_instructions)

        return "\n".join(parts)

    def get_polish_system_prompt(self) -> str:
        """Системный промпт для полировочного прохода.

        В отличие от get_system_prompt(), задача модели — не переводить с
        нуля, а УЛУЧШИТЬ уже существующий (структурно корректный) перевод:
        более естественная формулировка при сохранении смысла и тона.
        Явно требуем не трогать черновик, если он уже хорош — иначе
        полировка выродится в "перевод заново" со всеми его рисками.
        """
        parts = [
            "You are a professional editor polishing existing video game translations.",
            f"You will see SOURCE ({self.source_lang}) and DRAFT ({self.target_lang}) pairs.",
            f"Improve each DRAFT so it reads as natural, fluent {self.target_lang}, "
            "while keeping the exact meaning, tone, and register of SOURCE.",
            "If a DRAFT is already natural and accurate, return it completely UNCHANGED.",
            "Do not re-translate from scratch — refine the existing DRAFT.",
            "",
            "CRITICAL RULES — follow every rule exactly or the game will break:",
            r"1. §N§ tokens (§0§, §1§, §2§ …) are PROTECTED GAME CODES."
            r" Copy each one EXACTLY to the same relative position in the translation."
            r" NEVER translate, rename, split, or delete them."
            r" Wrong: '§ 0 §'  '§0 §'  '§О§' (Cyrillic O).  Correct: '§0§'.",
            "2. Output ONLY the improved translation. No commentary, notes, markdown bold (**), or explanations.",
            "3. Do NOT translate proper nouns listed in the glossary.",
            "4. Match the register and tone (casual speech, formal, dramatic, etc.).",
            "5. Keep character speech styles consistent across all lines.",
            "6. Preserve every engine-specific token and syntax exactly as specified by the engine profile.",
        ]
        parts.append("")
        parts.append(self.get_engine_instructions())
        if self.glossary_block:
            parts.append("")
            parts.append(self.glossary_block)
        if self.extra_instructions:
            parts.append("")
            parts.append(self.extra_instructions)
        return "\n".join(parts)


@dataclass
class TranslationResult:
    original: str
    translated: str
    success: bool
    error: str = ""
    from_cache: bool = False


@dataclass
class BatchResult:
    results: list[TranslationResult] = field(default_factory=list)
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    from_cache: int = 0
    elapsed_sec: float = 0.0
    cancelled: bool = False
    fatal_error: str = ""   # постоянная ошибка API (ключ / модель / доступ) — прогон остановлен


@dataclass
class PolishResult:
    """Результат полировки одной строки: черновик (before) → предложенный
    вариант (after). changed=False, если модель сочла черновик уже
    естественным и вернула его без изменений (или если правка была
    отклонена валидацией кодов и мы безопасно откатились к черновику)."""
    original: str
    before: str
    after: str
    changed: bool
    success: bool


@dataclass
class PolishBatchResult:
    results: list[PolishResult] = field(default_factory=list)
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    changed: int = 0
    skipped: int = 0  # исключены из полировки (пустой черновик / уже битые коды)
    elapsed_sec: float = 0.0
    cancelled: bool = False
    fatal_error: str = ""   # постоянная ошибка API (ключ / модель / доступ) — прогон остановлен


def _protect_codes(text: str, game_type: str = "unknown") -> tuple[str, dict[str, str]]:
    """Protect engine syntax before AI translation and restore it afterwards."""
    placeholders: dict[str, str] = {}
    counter = [0]

    def replace(m: re.Match) -> str:
        key = f"{PLACEHOLDER_PREFIX}{counter[0]}{PLACEHOLDER_SUFFIX}"
        placeholders[key] = m.group(0)
        counter[0] += 1
        return key

    # Always protect RPG codes first; then engine-specific tokens.
    protected = RPG_CODE_PATTERN.sub(replace, text)
    pattern = _engine_token_pattern(game_type)
    if pattern:
        protected = pattern.sub(replace, protected)
    return protected, placeholders


def _restore_codes(text: str, placeholders: dict[str, str]) -> str:
    """Восстановить RPG Maker коды из плейсхолдеров §N§.

    AI иногда разбивает §N§ переносом строки (§0\\n§ или §0 \\n§).
    Исправляем до основного поиска, иначе ключ не найдётся → коды теряются.
    """
    if not placeholders:
        return text
    # Heal split placeholders: "§0\n§" → "§0§"  (AI line-wrapped the token)
    text = _SPLIT_PLACEHOLDER_RE.sub(r"§\1§", text)
    for key, original in placeholders.items():
        text = text.replace(key, original)
    return text



def _extract_codes(text: str) -> list[str]:
    """Извлечь все RPG Maker коды из текста."""
    return [m.group(0) for m in RPG_CODE_PATTERN.finditer(text)]


def _strip_codes(text: str) -> str:
    """Удалить все RPG Maker коды, оставив только текст."""
    return RPG_CODE_PATTERN.sub("", text)


def _split_code_tokens(text: str) -> list[tuple[str, str]]:
    """Разбить строку на токены вида ('text'|'code', значение)."""
    tokens: list[tuple[str, str]] = []
    last = 0
    for match in RPG_CODE_PATTERN.finditer(text):
        if match.start() > last:
            tokens.append(("text", text[last:match.start()]))
        tokens.append(("code", match.group(0)))
        last = match.end()
    if last < len(text):
        tokens.append(("text", text[last:]))
    return tokens


def _snap_to_word_boundary(text: str, pos: int, min_pos: int, max_pos: int) -> int:
    """Сдвинуть позицию разреза к ближайшему пробелу в пределах [min_pos, max_pos].

    Возвращает pos без изменений, если рядом нет пробела в допустимых
    границах (например, текст на языке без пробелов между словами, или
    просто одно длинное слово) — резать по границе тогда нечем, и старое
    посимвольное поведение — единственный вариант.
    """
    if pos <= min_pos or pos >= max_pos:
        return pos
    if text[pos - 1].isspace() or text[pos].isspace():
        return pos  # уже на границе слова

    best = None
    best_dist = None
    for i in range(pos, max_pos):
        if text[i].isspace():
            best, best_dist = i + 1, i - pos
            break
    for i in range(pos - 1, min_pos - 1, -1):
        if text[i].isspace():
            dist = pos - i
            if best_dist is None or dist < best_dist:
                best = i + 1
            break

    return best if best is not None else pos


def _split_text_by_weights(text: str, weights: list[int]) -> list[str]:
    """Разделить текст на N частей, сохраняя примерно исходные пропорции.

    BUG FIX: раньше резало строго по пропорции символов, из-за чего код,
    восстанавливаемый _build_translation_with_original_codes, мог оказаться
    посередине слова перевода (например "героя" -> "ге" + код + "роя" —
    цвет/эффект переключался внутри слова). Теперь после расчёта позиции по
    пропорции ищем ближайший пробел рядом и режем по нему — код встаёт между
    словами. Если пробелов рядом нет — используем прежнюю посимвольную
    позицию (например для языков без пробелов между словами).
    """
    parts_count = len(weights)
    if parts_count <= 1:
        return [text]

    if not text:
        return [""] * parts_count

    safe_weights = [max(1, int(w)) for w in weights]
    total = sum(safe_weights) or parts_count
    total_len = len(text)

    cuts: list[int] = []
    used = 0
    for idx, weight in enumerate(safe_weights[:-1], start=1):
        used += weight
        target = round(total_len * used / total)
        min_pos = cuts[-1] if cuts else 0
        max_pos = total_len - (parts_count - idx)
        pos = max(min_pos, min(target, max_pos))
        pos = _snap_to_word_boundary(text, pos, min_pos, max_pos)
        cuts.append(pos)

    out: list[str] = []
    start = 0
    for cut in cuts:
        out.append(text[start:cut])
        start = cut
    out.append(text[start:])
    return out


def _build_translation_with_original_codes(original: str, translated: str) -> str:
    """
    Пересобрать перевод так, чтобы все escape-коды из оригинала сохранились.
    Если AI сломал/потерял коды, берём текст перевода и вставляем оригинальные коды
    в те же позиции между текстовыми сегментами.
    """
    orig_tokens = _split_code_tokens(original)
    orig_codes = [value for kind, value in orig_tokens if kind == "code"]
    if not orig_codes:
        return translated

    if _validate_codes(original, translated):
        return translated

    clean_translation = _strip_codes(translated)
    if not clean_translation.strip():
        # Если перевод почти пустой, лучше сохранить текст без кодов из оригинала,
        # чем потерять всю строку.
        clean_translation = _strip_codes(original)

    text_segments = [value for kind, value in orig_tokens if kind == "text"]
    if not text_segments:
        return translated

    translated_segments = _split_text_by_weights(clean_translation, [len(s) for s in text_segments])

    rebuilt: list[str] = []
    text_idx = 0
    for kind, value in orig_tokens:
        if kind == "text":
            rebuilt.append(translated_segments[text_idx])
            text_idx += 1
        else:
            rebuilt.append(value)

    return "".join(rebuilt)


def _repair_codes(original: str, translated: str) -> str:
    """Попытаться автоматически восстановить потерянные escape-коды."""
    if not translated:
        return translated
    repaired = _build_translation_with_original_codes(original, translated)
    return repaired


def _validate_codes(original: str, translated: str) -> bool:
    """Проверить, что все RPG Maker коды из оригинала присутствуют в переводе
    в том же порядке. Порядок важен: \\C[2]text\\C[0] ≠ \\C[0]text\\C[2]."""
    orig_codes = _extract_codes(original)
    if not orig_codes:
        return True
    trans_codes = _extract_codes(translated)
    return orig_codes == trans_codes   # ordered, not sorted


def _protect_texts(texts: list[str], game_type: str = "unknown") -> tuple[list[str], list[dict[str, str]]]:
    protected_texts: list[str] = []
    all_placeholders: list[dict[str, str]] = []
    for text in texts:
        protected, placeholders = _protect_codes(text, game_type)
        protected_texts.append(protected)
        all_placeholders.append(placeholders)
    return protected_texts, all_placeholders

def _strip_context_hint(text: str) -> str:
    """Удалить контекстную подсказку из начала строки, если AI её повторил.

    BUG FIX: некоторые модели (Groq llama-8b, старые Gemini) игнорируют
    правило 7 промпта и эхо-повторяют (Speaker: Name, Type: Dialog) в переводе.
    Результат в игре: «(Speaker: Alice) Привет!» вместо «Привет!».
    Паттерн консервативен — требует «:» внутри скобок, не трогает
    японские/кириллические скобки и обычный текст в круглых скобках.
    """
    m = re.match(r"^\s*\([^)]{1,80}\)\s*", text)
    if m and ":" in m.group(0):
        return text[m.end():]
    return text


def _validate_and_repair_batch(
    originals: list[str],
    translations: list[str],
    log_callback,
    game_type: str = "unknown",
) -> list[str]:
    """Strip AI artifacts (markdown, think-tags, trailing notes) and log code issues.

    Escape code preservation is handled upstream by the §N§ placeholder system.
    Auto-repair is disabled — if §N§ was lost it means the AI ignored the prompt;
    the right fix is a better prompt or smaller batch_size.
    """
    result: list[str] = list(translations)
    missing_codes = 0
    for i, (orig, trans) in enumerate(zip(originals, result)):
        if not trans:
            continue
        # 1. Strip model artifacts: think-blocks, **markdown**, trailing notes
        trans = _clean_translation(trans)
        # 2. Remove echoed context hints (Speaker: Name) etc.
        trans = _strip_context_hint(trans)
        result[i] = trans
        # 3. Log if codes were lost (for user awareness — no auto-repair)
        if not _validate_engine_tokens(orig, trans, game_type):
            missing_codes += 1
    if missing_codes:
        _log(
            f"  ⚠ Escape-коды не сохранены в {missing_codes}/{len(originals)} строках"
            " — уменьшите batch_size или проверьте вручную",
            log_callback,
        )
    return result



def _build_batch_prompt(texts: list[str], protected_texts: list[str],
                        source_lang: str, target_lang: str) -> str:
    """Build the numbered translation request prompt.

    If any protected text contains §N§ placeholders, appends an explicit
    one-line reminder so the AI never drops or reformats them.
    """
    numbered = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(protected_texts))
    prompt = (
        f"Translate exactly {len(texts)} lines from {source_lang} to {target_lang}.\n"
        "Return ONLY numbered translations [1], [2], ... [N]. No other text.\n"
    )
    # Add §N§ reminder only when placeholders are actually present
    if any("§" in t for t in protected_texts):
        prompt += "Copy every §N§ token (§0§, §1§, …) EXACTLY to the same position.\n"
    prompt += "\n" + numbered
    return prompt


def _protect_pairs_for_polish(
    pairs: list[tuple[str, str]], game_type: str = "unknown"
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Защитить коды в ОРИГИНАЛАХ обычным способом (_protect_codes), и той же
    картой плейсхолдеров защитить коды в ЧЕРНОВИКАХ.

    Полировка вызывается только для пар, уже прошедших _validate_codes
    (см. polish_texts), т.е. набор и порядок кодов в original и draft
    совпадают — поэтому можно переиспользовать ОДНУ карту §N§ → код для
    обеих строк, заменяя в черновике каждый код на его плейсхолдер по
    порядку появления (первое ещё не занятое вхождение).
    """
    protected_pairs: list[tuple[str, str]] = []
    all_placeholders: list[dict[str, str]] = []
    for original, draft in pairs:
        protected_original, placeholders = _protect_codes(original, game_type)
        protected_draft = draft
        for key, code in placeholders.items():
            protected_draft = protected_draft.replace(code, key, 1)
        protected_pairs.append((protected_original, protected_draft))
        all_placeholders.append(placeholders)
    return protected_pairs, all_placeholders


def _build_polish_prompt(
    pairs: list[tuple[str, str]],
    protected_pairs: list[tuple[str, str]],
    target_lang: str,
) -> str:
    """Собрать промпт полировки: пронумерованные пары SOURCE/DRAFT."""
    blocks = [
        f"[{i}]\nSOURCE: {orig}\nDRAFT: {draft}"
        for i, (orig, draft) in enumerate(protected_pairs, start=1)
    ]
    prompt = (
        f"Below are {len(pairs)} SOURCE/DRAFT pairs. DRAFT is a translation into {target_lang}.\n"
        f"Improve each DRAFT for natural, fluent {target_lang} while keeping SOURCE's exact meaning and tone.\n"
        "If a DRAFT is already natural and accurate, return it completely UNCHANGED.\n"
        "Return ONLY numbered improved translations [1], [2], ... [N]. No other text.\n"
    )
    if any("§" in o or "§" in d for o, d in protected_pairs):
        prompt += "Copy every §N§ token (§0§, §1§, …) EXACTLY to the same relative position.\n"
    prompt += "\n" + "\n\n".join(blocks)
    return prompt


def _parse_numbered(raw: str, expected: int) -> list[str]:
    """
    Разобрать пронумерованный ответ [1] text\\n[2] text...
    С несколькими уровнями fallback.
    """
    # Clean model artifacts: think blocks, markdown bold, trailing notes
    raw = _clean_translation(raw)
    
    results = [""] * expected
    if expected == 0:
        return results

    # Попытка 1: построчный [N] формат с поддержкой multi-line значений
    lines = raw.split("\n")
    current_idx: int | None = None
    current_lines: list[str] = []
    found_any = False

    def _flush():
        nonlocal current_lines
        if current_idx is not None and 0 <= current_idx < expected:
            val = _clean_translation("\n".join(current_lines))
            results[current_idx] = val
        current_lines = []

    for line in lines:
        m = _NUM_BRACKET_RE.match(line.strip())
        if m:
            _flush()
            current_idx = int(m.group(1)) - 1
            current_lines = [m.group(2)]
            found_any = True
        elif current_idx is not None:
            current_lines.append(line)
    _flush()

    if found_any:
        return results

    # Попытка 1b: формат нумерованного списка "N. text" или "N) text"
    lines = raw.split("\n")
    current_idx = None
    current_lines = []
    found_any_b = False

    def _flush_b():
        nonlocal current_lines
        if current_idx is not None and 0 <= current_idx < expected:
            val = _clean_translation("\n".join(current_lines))
            results[current_idx] = val
        current_lines = []

    for line in lines:
        m = _NUM_LIST_RE.match(line.strip())
        if m:
            _flush_b()
            current_idx = int(m.group(1)) - 1
            current_lines = [m.group(2)]
            found_any_b = True
        elif current_idx is not None:
            current_lines.append(line)
    _flush_b()

    if found_any_b:
        return results

    # Попытка 2: строки по порядку — убираем [N], N., N) префиксы
    non_empty = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    if non_empty:
        for i, line in enumerate(non_empty[:expected]):
            results[i] = _NUM_PREFIX_RE.sub("", line)
        if any(results):
            return results

    # Попытка 3: весь текст в первый элемент (для одиночных строк)
    if expected == 1:
        results[0] = raw.strip()
        return results

    # Попытка 4: разбиваем по строкам без нумерации — убираем возможный префикс
    if not any(results) and expected > 1:
        all_lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
        for i in range(min(expected, len(all_lines))):
            results[i] = _NUM_PREFIX_RE.sub("", all_lines[i])

    return results


def _log(msg: str, cb: Callable[[str], None] | None = None):
    console_print(msg)      # одна атомарная запись: строки из потоков не склеиваются
    if cb:
        cb(msg)


# ── Параметры сэмплирования: не все модели их принимают ──────────────────────
# Claude 5.x (Haiku / Sonnet / Opus 5.5, Fable 5.1, Opus 5, Sonnet 5) отвергает любой
# temperature, кроме значения по умолчанию, ошибкой HTTP 400
# (platform.claude.com/docs/en/models/haiku-5-5/migration-guide, проверено 2026-10-22).
# Проект ВСЕГДА слал temperature (0.1–0.3) — каждый запрос к таким моделям падал бы.
# Два слоя защиты: (1) заведомо не принимающим моделям параметр не отправляем;
# (2) для любой другой (будущие модели, рассуждающие модели OpenAI-совместимых
# провайдеров) — если API ответил 400 про temperature, повторяем БЕЗ него и
# запоминаем решение на экземпляре провайдера.
_NO_SAMPLING_MODEL_RE = re.compile(
    r"^claude-(?:(?:opus|sonnet|haiku)-(?:[5-9]|\d{2,})|fable|mythos)", re.IGNORECASE
)


def _model_rejects_sampling(model: str) -> bool:
    return bool(_NO_SAMPLING_MODEL_RE.match(model or ""))


def _is_unsupported_temperature_error(exc: BaseException) -> bool:
    """HTTP 400, в тексте которого фигурирует temperature."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 400 and "temperature" in str(exc).lower()


def _create_with_temperature_fallback(owner, create_fn, **kwargs):
    """create_fn(**kwargs); если API отверг temperature — повторить без него.

    owner — экземпляр провайдера: на нём запоминается решение (_omit_temperature),
    чтобы не платить лишним 400 на каждый батч.
    """
    def without_temperature(kw):
        return {k: v for k, v in kw.items() if k != "temperature"}

    if "temperature" in kwargs and (
        getattr(owner, "_omit_temperature", False)
        or _model_rejects_sampling(str(kwargs.get("model", "")))
    ):
        kwargs = without_temperature(kwargs)
    try:
        return create_fn(**kwargs)
    except Exception as e:  # noqa: BLE001 — SDK-исключения у провайдеров разные
        if "temperature" in kwargs and _is_unsupported_temperature_error(e):
            owner._omit_temperature = True
            return create_fn(**without_temperature(kwargs))
        raise


# ── Жизненный цикл моделей ───────────────────────────────────────────────────
# (провайдер, модель) → (дата закрытия ISO | None, замена / примечание).
# Проверено по официальным страницам 2026-10-22. None — модель больше не значится
# в списке доступных у провайдера. Таблица нужна для ПРЕДУПРЕЖДЕНИЯ в логе (мёртвая
# модель иначе проявляется как поток непонятных ошибок API); запросы она не блокирует.
MODEL_LIFECYCLE: dict[tuple[str, str], tuple[str | None, str]] = {
    ("openai", "gpt-4o-mini"): ("2027-01-14", "актуальную замену см. https://developers.openai.com/api/docs/deprecations"),
    ("openai", "gpt-4o"):      ("2027-01-14", "актуальную замену см. https://developers.openai.com/api/docs/deprecations"),
    ("gemini", "gemini-2.0-flash"):      ("2026-06-01", "gemini-3.6-flash"),
    ("gemini", "gemini-2.0-flash-001"):  ("2026-06-01", "gemini-3.6-flash"),
    ("gemini", "gemini-2.0-flash-lite"): ("2026-06-01", "gemini-3.1-flash-lite"),
    ("gemini", "gemini-1.5-pro"):        ("2025-09-24", "gemini-2.5-pro"),
    ("groq", "llama-3.1-8b-instant"):     ("2026-08-16", "openai/gpt-oss-20b"),
    ("groq", "llama-3.3-70b-versatile"):  ("2026-08-16", "openai/gpt-oss-120b"),
    ("groq", "llama-3.1-70b-versatile"):  (None, "openai/gpt-oss-120b"),
    ("deepseek", "deepseek-chat"):     ("2026-07-24", "deepseek-v4-flash"),
    ("deepseek", "deepseek-reasoner"): ("2026-07-24", "deepseek-v4-flash или deepseek-v4-pro"),
    ("claude", "claude-3-5-haiku-latest"):  (None, "claude-haiku-5-5"),
    ("claude", "claude-3-5-sonnet-latest"): (None, "claude-sonnet-5-5"),
}


def model_lifecycle_notice(provider: str, model: str, today=None) -> str | None:
    """Предупреждение, если модель закрыта / скоро будет закрыта; иначе None."""
    entry = MODEL_LIFECYCLE.get(((provider or "").lower(), model or ""))
    if not entry:
        return None
    from datetime import date
    shutdown, note = entry
    if shutdown is None:
        return (f"⚠ Модель {model} ({provider}) больше не значится в списке доступных у провайдера. "
                f"Замена: {note}. Задайте модель в настройках.")
    when = date.fromisoformat(shutdown)
    today = today or date.today()
    if when <= today:
        return (f"⚠ Модель {model} ({provider}) закрыта провайдером с {shutdown} — запросы будут "
                f"отклонены. Замена: {note}. Задайте модель в настройках.")
    return (f"⚠ Модель {model} ({provider}) будет закрыта провайдером {shutdown} "
            f"(через {(when - today).days} дн.): {note}.")



class OpenAIProvider:
    def __init__(self, config: TranslatorConfig):
        self.config = config
        # Lazy SDK loading: constructing a provider must not require the optional
        # SDK until an actual network request is made. This keeps CLI/tests and
        # local/mock providers usable even when the OpenAI package is absent,
        # while translate_batch/polish_batch still report a precise dependency
        # error at the point where it is actually needed.
        self._client = None

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
            kwargs = {"api_key": self.config.api_key, "timeout": self.config.timeout}
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._client = OpenAI(**kwargs)
            return self._client
        except ImportError as exc:
            raise RuntimeError("openai не установлен: pip install openai") from exc

    def translate_batch(self, texts: list[str], log_cb=None) -> list[str]:
        client = self._client_or_raise()

        protected_texts, all_placeholders = _protect_texts(texts, self.config.game_type)

        prompt = _build_batch_prompt(
            texts, protected_texts,
            self.config.source_lang, self.config.target_lang,
        )

        _log(f"  [OpenAI] {len(texts)} строк → {self.config.get_model()}", log_cb)

        resp = _create_with_temperature_fallback(
            self, client.chat.completions.create,
            model=self.config.get_model(),
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            messages=[
                {"role": "system", "content": self.config.get_system_prompt()},
                {"role": "user", "content": prompt},
            ],
        )

        raw = resp.choices[0].message.content or ""
        parsed = _parse_numbered(raw, len(texts))
        # _strip_context_hint is applied centrally in _validate_and_repair_batch;
        # _restore_codes must run here so placeholders survive the repair pass.
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]

    def polish_batch(self, pairs: list[tuple[str, str]], log_cb=None) -> list[str]:
        client = self._client_or_raise()
        protected_pairs, all_placeholders = _protect_pairs_for_polish(pairs, self.config.game_type)
        prompt = _build_polish_prompt(pairs, protected_pairs, self.config.target_lang)
        _log(f"  [OpenAI/Polish] {len(pairs)} строк → {self.config.get_model()}", log_cb)
        resp = _create_with_temperature_fallback(
            self, client.chat.completions.create,
            model=self.config.get_model(),
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            messages=[
                {"role": "system", "content": self.config.get_polish_system_prompt()},
                {"role": "user", "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content or ""
        parsed = _parse_numbered(raw, len(pairs))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]


class GeminiProvider:
    def __init__(self, config: TranslatorConfig):
        self.config = config
        self._use_new_sdk = False
        try:
            from google import genai  # google-genai >= 1.0
            self._client = genai.Client(api_key=config.api_key)
            self._use_new_sdk = True
            # Cache types and HttpOptions once — used in every translate_batch call
            from google.genai import types as _genai_types
            self._genai_types = _genai_types
            try:
                from google.genai.types import HttpOptions as _HO
                self._http_opts = _HO(timeout=config.timeout)
            except (ImportError, TypeError):
                self._http_opts = {"timeout": config.timeout}
        except (ImportError, AttributeError):
            try:
                import google.generativeai as genai  # google-generativeai (legacy)
                genai.configure(api_key=config.api_key)
                self._genai_legacy = genai
                self._use_new_sdk = False
            except ImportError:
                raise RuntimeError(
                    "Gemini SDK не установлен. Установите: pip install google-genai  "
                    "(или pip install google-generativeai для старой версии)"
                )

    def translate_batch(self, texts: list[str], log_cb=None) -> list[str]:
        protected_texts, all_placeholders = _protect_texts(texts, self.config.game_type)
        prompt = _build_batch_prompt(
            texts, protected_texts,
            self.config.source_lang, self.config.target_lang,
        )
        _log(f"  [Gemini] {len(texts)} строк → {self.config.get_model()}", log_cb)

        if self._use_new_sdk:
            resp = self._client.models.generate_content(
                model=self.config.get_model(),
                contents=prompt,
                config=self._genai_types.GenerateContentConfig(
                    system_instruction=self.config.get_system_prompt(),
                    temperature=self.config.temperature,
                    max_output_tokens=self.config.max_tokens,
                    http_options=self._http_opts,
                ),
            )
            try:
                raw = resp.text or ""
            except (ValueError, AttributeError):
                raw = ""
        else:
            # Legacy google-generativeai SDK
            model = self._genai_legacy.GenerativeModel(
                model_name=self.config.get_model(),
                system_instruction=self.config.get_system_prompt(),
                generation_config=self._genai_legacy.GenerationConfig(
                    temperature=self.config.temperature,
                    max_output_tokens=self.config.max_tokens,
                ),
            )
            try:
                resp = model.generate_content(prompt)
                raw = resp.text or ""
            except Exception:
                raw = ""

        parsed = _parse_numbered(raw, len(texts))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]

    def polish_batch(self, pairs: list[tuple[str, str]], log_cb=None) -> list[str]:
        protected_pairs, all_placeholders = _protect_pairs_for_polish(pairs, self.config.game_type)
        prompt = _build_polish_prompt(pairs, protected_pairs, self.config.target_lang)
        _log(f"  [Gemini/Polish] {len(pairs)} строк → {self.config.get_model()}", log_cb)

        if self._use_new_sdk:
            resp = self._client.models.generate_content(
                model=self.config.get_model(),
                contents=prompt,
                config=self._genai_types.GenerateContentConfig(
                    system_instruction=self.config.get_polish_system_prompt(),
                    temperature=self.config.temperature,
                    max_output_tokens=self.config.max_tokens,
                    http_options=self._http_opts,
                ),
            )
            try:
                raw = resp.text or ""
            except (ValueError, AttributeError):
                raw = ""
        else:
            model = self._genai_legacy.GenerativeModel(
                model_name=self.config.get_model(),
                system_instruction=self.config.get_polish_system_prompt(),
                generation_config=self._genai_legacy.GenerationConfig(
                    temperature=self.config.temperature,
                    max_output_tokens=self.config.max_tokens,
                ),
            )
            try:
                resp = model.generate_content(prompt)
                raw = resp.text or ""
            except Exception:
                raw = ""

        parsed = _parse_numbered(raw, len(pairs))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]


class ClaudeProvider:
    def __init__(self, config: TranslatorConfig):
        self.config = config
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=config.api_key, timeout=config.timeout)
        except ImportError:
            raise RuntimeError("anthropic не установлен: pip install anthropic")

    def translate_batch(self, texts: list[str], log_cb=None) -> list[str]:
        client = self._client

        protected_texts, all_placeholders = _protect_texts(texts, self.config.game_type)

        prompt = _build_batch_prompt(
            texts, protected_texts,
            self.config.source_lang, self.config.target_lang,
        )

        _log(f"  [Claude] {len(texts)} строк → {self.config.get_model()}", log_cb)

        msg = _create_with_temperature_fallback(
            self, client.messages.create,
            model=self.config.get_model(),
            max_tokens=self.config.max_tokens,
            # BUG FIX: temperature was missing — all other providers pass it
            temperature=self.config.temperature,
            system=self.config.get_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        # BUG FIX: content[0].text raises AttributeError if the API returns a
        # non-text block (e.g. tool_use). Iterate and pick the first text block.
        raw = next((b.text for b in msg.content if hasattr(b, "text") and b.text), "")
        parsed = _parse_numbered(raw, len(texts))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]

    def polish_batch(self, pairs: list[tuple[str, str]], log_cb=None) -> list[str]:
        client = self._client
        protected_pairs, all_placeholders = _protect_pairs_for_polish(pairs, self.config.game_type)
        prompt = _build_polish_prompt(pairs, protected_pairs, self.config.target_lang)
        _log(f"  [Claude/Polish] {len(pairs)} строк → {self.config.get_model()}", log_cb)
        msg = _create_with_temperature_fallback(
            self, client.messages.create,
            model=self.config.get_model(),
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=self.config.get_polish_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = next((b.text for b in msg.content if hasattr(b, "text") and b.text), "")
        parsed = _parse_numbered(raw, len(pairs))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]


class GroqProvider:
    """Groq — бесплатный, быстрый (Llama, Mixtral)."""
    def __init__(self, config: TranslatorConfig):
        self.config = config
        try:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=config.api_key,
                base_url="https://api.groq.com/openai/v1",
                timeout=config.timeout,
            )
        except ImportError:
            raise RuntimeError("openai не установлен: pip install openai")

    def translate_batch(self, texts: list[str], log_cb=None) -> list[str]:
        client = self._client
        protected_texts, all_placeholders = _protect_texts(texts, self.config.game_type)
        user_prompt = _build_batch_prompt(
            texts, protected_texts,
            self.config.source_lang, self.config.target_lang,
        )
        _log(f"  [Groq] {len(texts)} строк → {self.config.get_model()}", log_cb)
        resp = _create_with_temperature_fallback(
            self, client.chat.completions.create,
            model=self.config.get_model(),
            messages=[
                {"role": "system", "content": self.config.get_system_prompt()},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _parse_numbered(raw, len(texts))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]

    def polish_batch(self, pairs: list[tuple[str, str]], log_cb=None) -> list[str]:
        client = self._client
        protected_pairs, all_placeholders = _protect_pairs_for_polish(pairs, self.config.game_type)
        prompt = _build_polish_prompt(pairs, protected_pairs, self.config.target_lang)
        _log(f"  [Groq/Polish] {len(pairs)} строк → {self.config.get_model()}", log_cb)
        resp = _create_with_temperature_fallback(
            self, client.chat.completions.create,
            model=self.config.get_model(),
            messages=[
                {"role": "system", "content": self.config.get_polish_system_prompt()},
                {"role": "user",   "content": prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _parse_numbered(raw, len(pairs))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]


class DeepSeekProvider:
    """DeepSeek — дешёвый, хорошее качество."""
    def __init__(self, config: TranslatorConfig):
        self.config = config
        try:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=config.api_key,
                base_url="https://api.deepseek.com",
                timeout=config.timeout,
            )
        except ImportError:
            raise RuntimeError("openai не установлен: pip install openai")

    def translate_batch(self, texts: list[str], log_cb=None) -> list[str]:
        client = self._client
        protected_texts, all_placeholders = _protect_texts(texts, self.config.game_type)
        user_prompt = _build_batch_prompt(
            texts, protected_texts,
            self.config.source_lang, self.config.target_lang,
        )
        _log(f"  [DeepSeek] {len(texts)} строк → {self.config.get_model()}", log_cb)
        resp = _create_with_temperature_fallback(
            self, client.chat.completions.create,
            model=self.config.get_model(),
            messages=[
                {"role": "system", "content": self.config.get_system_prompt()},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _parse_numbered(raw, len(texts))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]

    def polish_batch(self, pairs: list[tuple[str, str]], log_cb=None) -> list[str]:
        client = self._client
        protected_pairs, all_placeholders = _protect_pairs_for_polish(pairs, self.config.game_type)
        prompt = _build_polish_prompt(pairs, protected_pairs, self.config.target_lang)
        _log(f"  [DeepSeek/Polish] {len(pairs)} строк → {self.config.get_model()}", log_cb)
        resp = _create_with_temperature_fallback(
            self, client.chat.completions.create,
            model=self.config.get_model(),
            messages=[
                {"role": "system", "content": self.config.get_polish_system_prompt()},
                {"role": "user",   "content": prompt},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _parse_numbered(raw, len(pairs))
        return [_restore_codes(t, ph) for t, ph in zip(parsed, all_placeholders)]


class MockProvider:
    """Офлайн-провайдер: детерминированный «перевод» без сети и без API-ключа.

    Нужен, чтобы проверить ВЕСЬ конвейер (extract → translate → QA → inject) на
    реальной игре, не тратя деньги и не завися от сети, и для CI. Результат —
    исходный текст с префиксом "[<язык>] ": escape-коды остаются нетронутыми,
    поэтому валидация кодов проходит так же, как с настоящим ИИ.
    Это НЕ перевод: для японского источника проверка «остался исходный язык»
    в логе валидации будет срабатывать — так и задумано.
    Полировка возвращает текст без изменений.
    """

    def __init__(self, config: TranslatorConfig):
        self.config = config

    def translate_batch(self, texts: list[str], log_cb=None) -> list[str]:
        _log(f"  [Mock] {len(texts)} строк (офлайн, без сети)", log_cb)
        prefix = f"[{self.config.target_lang}] "
        return [prefix + t for t in texts]

    def polish_batch(self, pairs: list[tuple[str, str]], log_cb=None) -> list[str]:
        _log(f"  [Mock] полировка {len(pairs)} строк: без изменений", log_cb)
        return [draft for _original, draft in pairs]


def _get_provider(config: TranslatorConfig):
    providers = {
        "openai":   OpenAIProvider,
        "gemini":   GeminiProvider,
        "claude":   ClaudeProvider,
        "groq":     GroqProvider,
        "deepseek": DeepSeekProvider,
        "mock":     MockProvider,
    }
    cls = providers.get(config.provider.lower())
    if not cls:
        # BUG FIX: "custom" provider selected in UI → use OpenAIProvider with base_url.
        # Previously caused ValueError crash; now gracefully falls back to OpenAI-compatible.
        if config.provider.lower() == "custom":
            if not config.base_url:
                raise ValueError(
                    "Для провайдера 'custom' необходимо указать Custom URL (LM Studio, Ollama и т.д.)"
                )
            return OpenAIProvider(config)
        raise ValueError(f"Неизвестный провайдер: {config.provider!r}. Допустимые: {list(providers) + ['custom']}")
    return cls(config)


# Метки типов для контекста

# ── Режимы качества ──────────────────────────────────────────────────────────

QUALITY_MODES = {
    "fast": {
        "label": "⚡ Быстрый (дешевле)",
        "models": {"openai":"gpt-4o-mini","gemini":"gemini-3.6-flash",
                   "claude":"claude-haiku-5-5","groq":"openai/gpt-oss-20b","deepseek":"deepseek-v4-flash"},
        "batch_size": 25, "temperature": 0.3, "use_context": False,
        "description": "Максимальная скорость, минимальная стоимость.",
    },
    "balanced": {
        "label": "⚖️ Сбалансированный (рекомендуется)",
        "models": {"openai":"gpt-4o-mini","gemini":"gemini-3.6-flash",
                   "claude":"claude-haiku-5-5","groq":"openai/gpt-oss-120b","deepseek":"deepseek-v4-flash"},
        "batch_size": 20, "temperature": 0.2, "use_context": True,
        "description": "Оптимальное соотношение качества и стоимости.",
    },
    "quality": {
        "label": "💎 Высокое качество",
        "models": {"openai":"gpt-4o","gemini":"gemini-2.5-pro",
                   "claude":"claude-sonnet-5-5","groq":"openai/gpt-oss-120b","deepseek":"deepseek-v4-pro"},
        "batch_size": 10, "temperature": 0.1, "use_context": True,
        "description": "Максимальное качество перевода. Медленнее и дороже.",
    },
}

# Грубая оценка: ОДНА цена за 1K токенов, усреднённая между входом и выходом
# (estimate_cost считает input+output вместе). Только цены, сверенные с официальными
# страницами 2026-10-22 (Anthropic — platform.claude.com/docs/en/models/overview;
# OpenAI gpt-4o-mini $0.15/$0.60 и gpt-4o $2.50/$10.00 за 1M токенов). Haiku 5.5 у
# Anthropic указан как «от $0.10 / $0.50» — это нижняя граница. Для моделей, цену
# которых не удалось проверить (Gemini, Groq, DeepSeek), записей нет: действует
# запасная оценка из estimate_cost, а не устаревшие цифры мёртвых моделей.
_COST_PER_1K_TOKENS = {
    "gpt-4o-mini": 0.000375, "gpt-4o": 0.00625,
    "claude-haiku-5-5": 0.0003, "claude-sonnet-5-5": 0.006,
    "claude-opus-5-5": 0.012, "claude-fable-5-1": 0.030,
    # Модели с бесплатным (ограниченным по частоте запросов) тарифом: как и раньше для
    # Gemini Flash и Groq, показываем «бесплатно». На платном тарифе реальная цена
    # будет выше — оценка здесь нижняя граница, не обещание.
    "gemini-3.6-flash": 0.0,
    "openai/gpt-oss-20b": 0.0, "openai/gpt-oss-120b": 0.0,
    # deepseek-v4-flash: значение перенесено от deepseek-chat (0.0003), НЕ проверено.
    "deepseek-v4-flash": 0.0003,
}


def estimate_cost(texts: list[str], model: str) -> tuple[int, float]:
    """Оценить стоимость перевода. (токенов, USD)"""
    chars = sum(len(t) for t in texts)
    tokens = int(chars / 1.5) * 2   # input+output, японский ~1.5 символа/токен
    cost = (tokens / 1000) * _COST_PER_1K_TOKENS.get(model, 0.001)
    return tokens, cost


_CTX_LABELS: dict[str, str] = {
    # ── RPG Maker MV / MZ ──────────────────────────────────────
    "dialog_text":    "Dialog",
    "speaker_name":   "Speaker name",
    "choice":         "Choice option",
    "narrator":       "Narration",
    "scroll_text":    "Scrolling text",
    "actor_name":     "Character name",
    "item":           "Item name",
    "skill":          "Skill name",
    "map_name":       "Location name",
    "class_name":     "Class name",
    "weapon_name":    "Weapon name",
    "armor_name":     "Armor name",
    "enemy_name":     "Enemy name",
    "state_name":     "Status effect name",
    "tileset_name":   "Tileset name",
    "common_event":   "Common event text",
    # ── Wolf RPG ────────────────────────────────────────────────
    "wolf_dialog":    "Dialog",
    "wolf_choice":    "Choice option",
    "wolf_string":    "Game text",
    # ── RPG Maker VX Ace ───────────────────────────────────────
    "vxace_dialog":   "Dialog",
    "vxace_choice":   "Choice option",
    "vxace_name":     "Character name",
    "vxace_string":   "Game text",
    # ── Kirikiri / KAG ──────────────────────────────────────────
    "kirikiri_text":  "Dialog",
    "kirikiri_choice":"Choice option",
    # ── Ren'Py ──────────────────────────────────────────────────
    "renpy_say":      "Dialog",
    "renpy_narrator": "Narration",
    "renpy_menu":     "Choice option",
    "renpy_string":   "UI text",
    # ── TyranoScript ────────────────────────────────────────────
    "tyrano_dialog":  "Dialog",
    "tyrano_choice":  "Choice option",
    "dialog":         "Dialog",   # generic fallback used by tyrano
    # ── Godot Engine ────────────────────────────────────────────
    "godot_text":     "UI text",
    "godot_dialog":   "Dialog",
    "godot_dialog_choice":    "Choice option",
    "godot_dialog_narration": "Narration",
    "godot_script":   "Game text",
    "godot_bbcode_text": "UI text",
    # ── Unity ───────────────────────────────────────────────────
    # BUG FIX: unity_extractor.py produces six distinct context values
    # (unity_text/unity_json/unity_csv/unity_xml/unity_po/unity_yaml — see
    # TEXT_EXTS and the per-format _extract_* functions there), but only
    # three had labels here. The other three fell through
    # _format_line_with_context()'s fallback chain (it reads this dict
    # directly via _CTX_LABELS.get(ctx, "")) to the raw context string
    # itself (e.g. "unity_xml"), which isn't a crash but is a useless hint
    # in the translation prompt.
    "unity_text":     "UI text",
    "unity_json":     "Game text",
    "unity_csv":      "Game text",
    "unity_xml":      "Game text",
    "unity_po":       "UI text",
    "unity_yaml":     "Game text",
}


_CTX_HINT_RE = re.compile(r"^\s*\([^)]{1,80}\)\s*")

# Паттерны для извлечения server-specified retry-delay из текста ошибки.
# Используется как fallback когда у исключения нет .response.headers (Gemini SDK,
# некоторые обёртки) — большинство API всё равно упоминают точное время в message.
_RETRY_AFTER_TEXT_PATTERNS = [
    re.compile(r"retry[- ]after[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"try again in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"please wait (\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
]


def _extract_retry_after(exc: Exception) -> float | None:
    """
    Извлечь server-specified задержку перед retry из исключения API.

    Приоритет:
      1. HTTP заголовок Retry-After (OpenAI/Anthropic/Groq/DeepSeek SDK — все
         основаны на httpx и пробрасывают .response.headers на RateLimitError).
      2. Текст сообщения об ошибке — Gemini и некоторые другие API не дают
         доступа к заголовкам напрямую, но указывают точное время в message
         (например "Please retry in 20.5s").

    Возвращает None если ни один способ не сработал — вызывающий код тогда
    использует свою экспоненциальную эвристику задержки.
    """
    # 1. HTTP-заголовок (httpx.Response, как у OpenAI/Anthropic/Groq SDK)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is not None:
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
            val = None
            try:
                val = headers.get(key)
            except Exception:
                pass
            if val:
                try:
                    return max(0.0, float(val))
                except (TypeError, ValueError):
                    pass

    # 2. Парсинг из текста сообщения (Gemini и другие)
    msg = str(exc)
    for pat in _RETRY_AFTER_TEXT_PATTERNS:
        m = pat.search(msg)
        if m:
            try:
                return max(0.0, float(m.group(1)))
            except (TypeError, ValueError):
                pass
    return None


def _ctx_label(ctx: str) -> str:
    """Получить человекочитаемую метку для context-значения (с prefix-matching)."""
    if ctx in _CTX_LABELS:
        return _CTX_LABELS[ctx]
    if ctx.startswith(("dialog:", "say:")):
        return "Dialog"
    if ":" in ctx:
        cat, _, field = ctx.partition(":")
        return f"{cat.capitalize()} {field}"
    if ctx.isalpha() and ctx.islower():
        return "Dialog"   # RenPy char variable
    return ctx


def _format_line_with_context(text: str, ctx: str, speaker: str = "") -> str:
    """Форматировать строку с контекстом для AI-промпта.

    Обрабатывает все форматы context-значений от всех движков:
      - "dialog:Alice"    → Kirikiri (имя говорящего)
      - "say:Alice"       → Tyrano, Godot
      - "alice"           → RenPy (переменная персонажа = имя)
      - "actor:name"      → VXAce data field
      - "dialog_text"     → MV/MZ dialog
      - "choice"          → выбор
      - "narrator"        → нарратив без имени
      - "godot_script"    → GDScript tr()
    """
    parts: list[str] = []
    _NOT_SPEAKER = frozenset({"narrator", "choice", "dialog", "menu", "narration", "text"})

    # Явный speaker > контекст
    if speaker:
        parts.append(f"Speaker: {speaker}")

    if not speaker:
        if ctx.startswith(("dialog:", "say:")):
            char = ctx.split(":", 1)[1].strip()
            if char:
                parts.append(f"Speaker: {char}")
        elif ctx.isalpha() and ctx.islower() and 1 < len(ctx) <= 24 and ctx not in _NOT_SPEAKER:
            parts.append(f"Speaker: {ctx.capitalize()}")
        # VXAce data: "actor:name" → "Character name", "item:description" → "Item description"
        elif ":" in ctx:
            category, _, field = ctx.partition(":")
            label = f"{category.capitalize()} {field}"
            parts.append(f"Type: {label}")
        else:
            label = _CTX_LABELS.get(ctx, "")
            if label and label not in ("Dialog", "Narration", "Game text"):
                parts.append(f"Type: {label}")

    if parts:
        return f"({', '.join(parts)}) {text}"
    return text


# ── Постоянные ошибки API: повторами не лечатся ──────────────────────────────
# Замер: при неверном ключе (401) или закрытой модели (404) 12 строк обрабатывались
# 60 секунд и 60 запросами — каждый батч повторялся, делился пополам и снова повторялся,
# и только потом выдавалось «переведено 0/12». На реальной игре (сотни батчей) — часы
# бессмысленных повторов. Теперь первая же постоянная ошибка останавливает прогон.
_BAD_KEY_MARKERS = ("api key not valid", "api_key_invalid", "invalid api key",
                    "incorrect api key", "invalid x-api-key")
_MODEL_GONE_MARKERS = ("model_not_found", "model not found", "decommissioned",
                       "has been deprecated", "no longer supported",
                       "is not found for api version")
_BILLING_MARKERS = ("insufficient_quota", "credit balance is too low",
                    "check your plan and billing")


def _http_status(exc: BaseException):
    for attr in ("status_code", "code"):          # openai/anthropic/groq: status_code; google: code
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _classify_permanent_error(exc: BaseException) -> str | None:
    """Причина (по-русски), если ошибка ПОСТОЯННАЯ; иначе None (повторить имеет смысл).

    Консервативно: 401/403/404 — всегда постоянные; 400 и 429 — только при явных
    маркерах в тексте (общий 400 «запрос слишком большой» и обычный rate limit
    постоянными НЕ считаются). Исключения без HTTP-статуса (таймаут, обрыв
    соединения) — временные.
    """
    status = _http_status(exc)
    if status is None:
        return None
    text = str(exc).lower()
    if status == 401 or (status in (400, 403) and any(m in text for m in _BAD_KEY_MARKERS)):
        return "неверный или отозванный API-ключ"
    if status == 403:
        return "доступ запрещён (проверьте права ключа, регион и тариф)"
    if status == 404 or (status == 400 and any(m in text for m in _MODEL_GONE_MARKERS)):
        return "модель или адрес API не найдены — возможно, модель закрыта провайдером"
    if status in (400, 429) and any(m in text for m in _BILLING_MARKERS):
        return "недостаточно средств или исчерпана квота аккаунта"
    return None


def _record_fatal(config: "TranslatorConfig", reason: str, exc: BaseException,
                  log_cb: Callable[[str], None] | None) -> None:
    """Запомнить постоянную ошибку (первая побеждает) и сообщить один раз."""
    box = config.fatal_error_box
    if "reason" in box:
        return
    box["reason"] = f"{reason} ({exc})"
    _log(f"  ⛔ Постоянная ошибка API — повторы бессмысленны, прогон остановлен: {reason}. "
         f"Ответ сервера: {exc}", log_cb)


def _do_translate_batch(
    provider,
    texts: list[str],
    config: TranslatorConfig,
    log_cb: Callable[[str], None] | None,
    cancel_event=None,
) -> tuple[bool, list[str]]:
    """
    Переводит батч с retry. При полном провале делит пополам и пробует снова.
    Возвращает (success, translations).
    cancel_event используется для прерываемого ожидания между попытками.
    """
    delay = config.retry_delay

    for attempt in range(1, config.max_retries + 1):
        if cancel_event and cancel_event.is_set():
            return False, [""] * len(texts)
        if config.fatal_error_box:
            # Другой батч уже получил постоянную ошибку API: этот (стоявший в очереди
            # или ждавший перед повтором) к провайдеру не ходит.
            return False, [""] * len(texts)
        try:
            translated = provider.translate_batch(texts, log_cb=log_cb)
            # Валидация: количество переведённых должно совпадать с отправленными
            if len(translated) != len(texts):
                raise ValueError(
                    f"Получено {len(translated)} переводов, ожидалось {len(texts)} — повтор"
                )
            # Проверяем что не вернулись пустые строки для непустых оригиналов
            empty_count = sum(1 for t in translated if not t.strip())
            if empty_count > len(texts) * 0.5:
                raise ValueError(
                    f"Слишком много пустых ответов ({empty_count}/{len(texts)}) — повтор"
                )

            _log(f"  ✓ Батч переведён (попытка {attempt})", log_cb)
            return True, translated
        except Exception as e:
            reason = _classify_permanent_error(e)
            if reason:
                _record_fatal(config, reason, e, log_cb)
                return False, [""] * len(texts)
            err_str = str(e).lower()
            # Rate limit (429) — ждём значительно дольше
            is_rate_limit = (
                "429" in err_str or
                "rate limit" in err_str or
                "quota" in err_str or
                "resource exhausted" in err_str or
                "too many requests" in err_str
            )
            if is_rate_limit:
                # Берём время из заголовка Retry-After (OpenAI/Anthropic/Groq)
                # или из текста ошибки (Gemini и другие) — сервер знает лучше нас.
                server_wait = _extract_retry_after(e)
                if server_wait is not None:
                    wait_time = min(server_wait + 1.0, 300.0)  # +1s буфер, max 5 мин
                    _log(f"  ⏳ Rate limit! Сервер: ждём {wait_time:.0f}с (Retry-After)...", log_cb)
                else:
                    wait_time = min(delay * 4, 120.0)
                    _log(f"  ⏳ Rate limit! Ждём {wait_time:.0f}с...", log_cb)
            else:
                wait_time = delay
                _log(f"  ✗ Попытка {attempt}/{config.max_retries}: {e}", log_cb)
            if attempt < config.max_retries:
                _log(f"  ↻ Повтор через {wait_time:.1f}с...", log_cb)
                # BUG FIX: "cancel_event.wait(wait_time) if cancel_event else False"
                # skipped the wait ENTIRELY when no cancel_event was supplied (this
                # is exactly the case for every `cli.py translate` run, which never
                # passes one) — retries fired back-to-back with zero backoff,
                # hammering a still-active rate limit instead of waiting it out.
                if cancel_event:
                    cancelled = cancel_event.wait(wait_time)
                else:
                    time.sleep(wait_time)
                    cancelled = False
                if cancelled:
                    _log("  ⏹ Ожидание прервано по запросу пользователя.", log_cb)
                    return False, [""] * len(texts)
                delay = min(delay * 2, 60.0)

    # Все попытки провалились — делим пополам, если батч > 1 строки
    if len(texts) > 1:
        half = len(texts) // 2
        _log(f"  ↗ Делю батч пополам ({half} + {len(texts) - half} строк)...", log_cb)
        ok1, part1 = _do_translate_batch(provider, texts[:half], config, log_cb, cancel_event)
        ok2, part2 = _do_translate_batch(provider, texts[half:], config, log_cb, cancel_event)
        if ok1 or ok2:
            return True, part1 + part2

    _log(f"  ✗ Батч из {len(texts)} строк не удалось перевести — оставляем оригинал.", log_cb)
    return False, [""] * len(texts)


def translate_texts(
    texts: list[str],
    config: TranslatorConfig,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
    state=None,
    cancel_event=None,
    contexts: list[str] | None = None,
) -> BatchResult:
    """
    Главная функция перевода.
    - Пропускает строки из кэша (state)
    - Батчи с защитой escape-кодов
    - Умный retry (half-batch fallback)
    - Валидация RPG-кодов после каждого перевода
    - Rate limiting между батчами
    - Кооперативная отмена через cancel_event
    """
    result = BatchResult(total=len(texts))
    provider = _get_provider(config)
    start = time.monotonic()

    # Разделяем: из кэша / нужно переводить
    cache_results: list[TranslationResult] = []
    to_translate_indices: list[int] = []

    for i, text in enumerate(texts):
        if state and state.has(text):
            cached = state.get(text) or ""
            # No auto-repair of cached translations — just warn if codes are missing.
            # The fix is in the system prompt (§N§ rule) not in post-hoc patching.
            if cached and not _validate_engine_tokens(text, cached, config.game_type):
                _log(
                    f"[Translator] ⚠ Кэшированный перевод потерял escape-коды: {text[:60]!r}"
                    " — будет переведён заново",
                    log_callback,
                )
                cached = ""  # force re-translation
            if cached:
                cache_results.append(TranslationResult(
                    original=text, translated=cached, success=True, from_cache=True
                ))
                result.from_cache += 1
            else:
                cache_results.append(None)  # type: ignore
                to_translate_indices.append(i)
        else:
            cache_results.append(None)  # type: ignore
            to_translate_indices.append(i)

    if result.from_cache:
        _log(f"[Translator] Из кэша: {result.from_cache}/{len(texts)} строк", log_callback)

    # ── Глобальная Translation Memory (второй слой кэша, между играми) ──
    # Строки без escape-кодов могут совпадать между разными играми одного жанра
    # (общие фразы UI, частые диалоговые конструкции). GlobalTM экономит вызовы API.
    global_tm = None
    tm_hits = 0
    if config.use_global_tm and to_translate_indices:
        try:
            from global_tm import GlobalTM
            # BUG FIX (P1-22, внешний аудит): tm_namespace уже смешивает
            # target_lang+game_type+style_profile в одну составную строку,
            # передаваемую как параметр target_lang в GlobalTM (де-факто
            # composite key внутри одной колонки — так изоляция по движку
            # и стилю уже была). Не было ТОЛЬКО source_lang — одинаковый
            # source-текст, переведённый из РАЗНЫХ исходных языков (или с
            # разным source_lang между проектами), мог получить чужой
            # закэшированный перевод. Добавляем source_lang в ту же
            # составную строку — без изменения схемы БД и без миграции
            # существующих данных (старые записи просто перестают
            # совпадать с новым namespace, что безопасно — они не
            # удаляются и не портятся, просто больше не используются для
            # новых, более точных по языку запросов).
            tm_namespace = (f"{config.source_lang}|{config.target_lang}|{config.game_type}|{config.style_profile or 'default'}"
                           if config.game_type not in ("", "unknown") else f"{config.source_lang}|{config.target_lang}")
            global_tm = GlobalTM(target_lang=tm_namespace).open()
            still_missing = [texts[i] for i in to_translate_indices]
            tm_map = global_tm.bulk_lookup(still_missing)
            if tm_map:
                remaining_indices = []
                for i in to_translate_indices:
                    t = texts[i]
                    tm_trans = tm_map.get(t.strip())
                    if tm_trans and _validate_engine_tokens(t, tm_trans, config.game_type):
                        cache_results[i] = TranslationResult(
                            original=t, translated=tm_trans, success=True, from_cache=True
                        )
                        result.from_cache += 1
                        tm_hits += 1
                    else:
                        remaining_indices.append(i)
                to_translate_indices = remaining_indices
                if tm_hits:
                    _log(f"[Translator] Из глобальной TM: {tm_hits} строк", log_callback)
        except Exception as e:
            _log(f"[Translator] ⚠ Global TM недоступна: {e}", log_callback)
            global_tm = None

    texts_to_translate = [texts[i] for i in to_translate_indices]

    if not texts_to_translate:
        _log("[Translator] Все строки из кэша — API не вызывается.", log_callback)
        for tr in cache_results:
            result.results.append(tr)
            result.succeeded += 1
        result.elapsed_sec = round(time.monotonic() - start, 2)
        if global_tm:
            global_tm.close()
        return result

    _log(
        f"[Translator] Перевод {len(texts_to_translate)} строк "
        f"(пропущено из кэша: {result.from_cache})",
        log_callback,
    )
    _log(f"[Translator] Провайдер: {config.provider} / {config.get_model()}", log_callback)
    _log(f"[Translator] Батч: {config.batch_size}, повторов: {config.max_retries}, задержка: {config.request_delay}с", log_callback)

    translated_map: dict[int, str] = {}

    # BUG FIX #1: при max_workers>1 все батчи сабмитятся до завершения первого,
    # поэтому translation_history была пуста при отправке — история не работала.
    # Скользящее окно контекста работает ТОЛЬКО при max_workers=1.
    use_sliding_history = config.use_context and config.max_workers == 1
    translation_history: list[tuple[str, str]] = []

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    lock = threading.Lock()
    done_count = 0
    total_to_do = len(texts_to_translate)

    # Строим батчи с контекстом заранее
    batches = []
    for batch_start in range(0, total_to_do, config.batch_size):
        batch = texts_to_translate[batch_start:batch_start + config.batch_size]
        indices = to_translate_indices[batch_start:batch_start + config.batch_size]
        batch_ctx: list[str] | None = None
        if config.use_context and contexts:
            batch_ctx = []
            for idx, text in zip(indices, batch):
                ctx = contexts[idx] if idx < len(contexts) else ""
                batch_ctx.append(_format_line_with_context(text, ctx) if ctx else text)
        batches.append((batch_start, batch, indices, batch_ctx))

    _log(
        f"[Translator] Подготовлено {len(batches)} батчей."
        f" Параллельных потоков: {config.max_workers}",
        log_callback,
    )

    if config.max_workers == 1:
        # ── Последовательный режим: история контекста работает корректно ──
        for b_start, batch, indices, batch_ctx in batches:
            if cancel_event and cancel_event.is_set():
                _log("[Translator] ⏹ Перевод прерван пользователем.", log_callback)
                result.cancelled = True
                break
            if config.fatal_error_box:
                break      # постоянная ошибка API: остальные батчи ждёт то же самое

            cfg_copy = TranslatorConfig(**config.__dict__)
            if use_sliding_history and translation_history:
                history_lines: list[str] = []
                for orig_h, trans_h in translation_history[-10:]:
                    history_lines.append(f"Source: {orig_h}")
                    history_lines.append(f"Translation: {trans_h}")
                cfg_copy.previous_context_block = "\n".join(history_lines)
            else:
                cfg_copy.previous_context_block = ""

            try:
                success, translated_batch = _do_translate_batch(
                    provider, batch_ctx if batch_ctx else batch,
                    cfg_copy, log_callback, cancel_event,
                )
            except Exception as e:
                success = False
                translated_batch = [""] * len(batch)
                _log(f"  ✗ Ошибка батча {b_start}: {e}", log_callback)

            translated_batch = _validate_and_repair_batch(batch, translated_batch, log_callback, config.game_type)

            # ── Retry строк с потерянными кодами (batch_size=1) ──
            # Корень проблемы: если AI потерял §N§ при большом батче — повторяем
            # эту строку отдельно (batch_size=1). Один вызов = максимум внимания AI.
            if success:
                # BUG FIX (P0-10/P0-11, внешний аудит, подтверждено чтением
                # кода): условие ниже раньше требовало truthy `trans` —
                # значит строка, для которой _parse_numbered() не нашла свой
                # номер в ответе модели (пропущенный [N] в неполном/
                # повреждённом нумерованном ответе — например есть [1] и [3],
                # но нет [2]), получала translated_batch[i] == "" и НИКОГДА
                # не попадала в этот же per-line retry, которым уже чинятся
                # потерянные коды. Общий batch-retry срабатывает только при
                # >50% пустых (см. _do_translate_batch) — то есть batch из 10
                # строк с 1-4 пропущенными проходил как "успешный" без
                # единой попытки восстановить именно эти позиции. (Не
                # постоянная порча: state.set() ниже и так уже не кэширует
                # пустой translated, так что при повторном Translate строка
                # была бы взята снова — но в ТЕКУЩЕМ проходе просто
                # оставалась непереведённой без единой попытки её вытащить.)
                # Теперь: то же самое исправление одним и тем же retry-
                # механизмом (batch_size=1, максимум внимания AI) — просто
                # шире условие: retry либо на потере кодов, либо на
                # полностью пустом результате.
                retry_indices_local = [
                    i for i, (orig, trans) in enumerate(zip(batch, translated_batch))
                    if orig and (not trans or not _validate_engine_tokens(orig, trans, config.game_type))
                ]
                if retry_indices_local:
                    _log(
                        f"  🔁 Перевод {len(retry_indices_local)} строк — "
                        f"потерянные коды или пропущенные в batch-ответе (batch=1)...",
                        log_callback,
                    )
                    for li in retry_indices_local:
                        orig_line = batch[li]
                        if (cancel_event and cancel_event.is_set()) or config.fatal_error_box:
                            break
                        try:
                            # Smart retry: добавляем в extra_instructions точный список
                            # кодов которые AI должен сохранить, чтобы он не потерял их снова.
                            missing_codes = _extract_codes(orig_line)
                            retry_cfg = TranslatorConfig(**cfg_copy.__dict__)
                            if missing_codes:
                                codes_str = " ".join(missing_codes[:8])
                                retry_cfg.extra_instructions = (
                                    f"CRITICAL: this line contains escape codes that MUST be preserved: {codes_str}\n"
                                    f"Previous translation lost them. Copy each code EXACTLY to the same position.\n"
                                    + (cfg_copy.extra_instructions or "")
                                ).strip()
                            ok1, r1 = _do_translate_batch(
                                provider, [orig_line],
                                retry_cfg, log_callback, cancel_event,
                            )
                            r1 = _validate_and_repair_batch([orig_line], r1, None, config.game_type)
                            if ok1 and r1[0] and _validate_engine_tokens(orig_line, r1[0], config.game_type):
                                translated_batch[li] = r1[0]
                                _log(f"    ✓ Строка {li}: восстановлена через retry", log_callback)
                            else:
                                _log(f"    ⚠ Строка {li}: не удалось восстановить после retry — строка пропущена", log_callback)
                                translated_batch[li] = ""  # skip broken line
                        except Exception:
                            translated_batch[li] = ""

            for orig_idx, (original, translated) in zip(indices, zip(batch, translated_batch)):
                final_t = translated if (success and translated) else ""
                translated_map[orig_idx] = final_t
                if state and success and translated and _validate_engine_tokens(original, translated, config.game_type):
                    state.set(original, translated)
                    if use_sliding_history:
                        translation_history.append((original, translated))

            done_count += len(batch)
            if progress_callback:
                batch_num = done_count // max(config.batch_size, 1)
                progress_callback(done_count, total_to_do, -1.0, f"батч {batch_num}")
            if state:
                state.save_periodically(50)
            if config.request_delay > 0:
                # BUG FIX: plain time.sleep() here ignored cancel_event entirely —
                # clicking Cancel during this pause (up to config.request_delay,
                # user-configurable up to 30s) had no effect until the sleep
                # finished. cancel_event.wait() returns immediately once the
                # event is set, same as everywhere else cancellation is honored.
                if cancel_event:
                    cancel_event.wait(config.request_delay)
                else:
                    time.sleep(config.request_delay)

    else:
        # ── Параллельный режим: без истории (порядок завершения непредсказуем) ──
        with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
            futures: dict = {}
            for b_start, batch, indices, batch_ctx in batches:
                if (cancel_event and cancel_event.is_set()) or config.fatal_error_box:
                    break
                cfg_copy = TranslatorConfig(**config.__dict__)
                cfg_copy.previous_context_block = ""
                future = executor.submit(
                    _do_translate_batch, provider,
                    batch_ctx if batch_ctx else batch,
                    cfg_copy, log_callback, cancel_event,
                )
                futures[future] = (b_start, batch, indices, cfg_copy)
                # BUG FIX #9: request_delay applied BEFORE next submission,
                # not after completion. This is the correct place to rate-limit
                # bursting when parallel workers submit to the API concurrently.
                # BUG FIX: was a plain time.sleep(), ignoring cancel_event — same
                # issue as the sequential-mode delay above, fixed the same way.
                if config.request_delay > 0 and b_start + config.batch_size < total_to_do:
                    if cancel_event:
                        cancel_event.wait(config.request_delay)
                    else:
                        time.sleep(config.request_delay)

            # BUG FIX #3: отменяем ещё не запущенные futures при cancel
            if cancel_event and cancel_event.is_set():
                result.cancelled = True
                for f in futures:
                    f.cancel()

            for future in as_completed(futures):
                b_start, batch, indices, batch_cfg_copy = futures[future]
                if config.fatal_error_box:
                    for f in futures:        # ещё не начатые батчи не нужны
                        f.cancel()
                if future.cancelled():
                    # Батч не успел стартовать и отменён вместе с остальными —
                    # результата нет, пропускаем.
                    result.cancelled = True
                    continue
                # BUG FIX: раньше при cancel_event здесь стоял `future.cancel(); continue`.
                # future.cancel() отменяет только ещё НЕ начатые задачи; для батча,
                # который уже выполнился (и оплачен), он ничего не делал, а `continue`
                # выбрасывал готовый результат: отмена сразу после первого успешного
                # батча давала «переведено 0/N». Завершённые батчи сохраняем всегда.
                if cancel_event and cancel_event.is_set():
                    result.cancelled = True

                try:
                    success, translated_batch = future.result()
                except Exception as e:
                    success = False
                    translated_batch = [""] * len(batch)
                    _log(f"  ✗ Внутренняя ошибка потока (батч {b_start}): {e}", log_callback)

                translated_batch = _validate_and_repair_batch(batch, translated_batch, log_callback, config.game_type)

                # ── Retry строк с потерянными кодами или пропущенными в
                # неполном batch-ответе (batch_size=1) — см. подробный
                # комментарий у идентичного места в последовательном режиме
                # выше (BUG FIX P0-10/P0-11).
                if success:
                    retry_local = [
                        i for i, (o, t) in enumerate(zip(batch, translated_batch))
                        if o and (not t or not _validate_engine_tokens(o, t, config.game_type))
                    ]
                    if retry_local:
                        _log(f"  🔁 Retry {len(retry_local)} строк (batch=1)...", log_callback)
                        for li in retry_local:
                            if (cancel_event and cancel_event.is_set()) or config.fatal_error_box: break
                            try:
                                orig_line = batch[li]
                                missing_codes = _extract_codes(orig_line)
                                retry_cfg = TranslatorConfig(**batch_cfg_copy.__dict__)
                                if missing_codes:
                                    codes_str = " ".join(missing_codes[:8])
                                    retry_cfg.extra_instructions = (
                                        f"CRITICAL: preserve these codes: {codes_str}\n"
                                        + (batch_cfg_copy.extra_instructions or "")
                                    ).strip()
                                ok1, r1 = _do_translate_batch(
                                    provider, [orig_line],
                                    retry_cfg, log_callback, cancel_event,
                                )
                                r1 = _validate_and_repair_batch([orig_line], r1, None, config.game_type)
                                if ok1 and r1[0] and _validate_engine_tokens(orig_line, r1[0], config.game_type):
                                    translated_batch[li] = r1[0]
                                else:
                                    translated_batch[li] = ""
                            except Exception:
                                translated_batch[li] = ""

                # BUG FIX #4: state.save_periodically делает I/O —
                # не держим лок во время записи (блокировало все рабочие потоки).
                batch_results: list[tuple[int, str, str]] = []
                for orig_idx, (original, translated) in zip(indices, zip(batch, translated_batch)):
                    final_t = translated if (success and translated) else ""
                    batch_results.append((orig_idx, original, final_t))

                with lock:
                    for orig_idx, original, final_t in batch_results:
                        translated_map[orig_idx] = final_t
                        if state and final_t and _validate_engine_tokens(original, final_t, config.game_type):
                            state.set(original, final_t)
                    done_count += len(batch)
                    if progress_callback:
                        batch_num = done_count // max(config.batch_size, 1)
                        progress_callback(done_count, total_to_do, -1.0, f"батч {batch_num}")

                # I/O вне лока
                if state:
                    state.save_periodically(50)
                # NOTE: request_delay is now applied between batch SUBMISSIONS above,
                # not here — applying it after completions didn't prevent API bursting.

    # Собираем финальный результат
    for i, text in enumerate(texts):
        if cache_results[i] is not None:
            result.results.append(cache_results[i])
            result.succeeded += 1
        else:
            translated = translated_map.get(i, "")  # BUG FIX: was `text` — failed entries returned original
            ok = i in translated_map and bool(translated_map[i])
            tr = TranslationResult(original=text, translated=translated, success=ok)
            result.results.append(tr)
            if ok:
                result.succeeded += 1
                # Сохраняем новый перевод в глобальную TM для будущих проектов
                if global_tm and _validate_engine_tokens(text, translated, config.game_type):
                    global_tm.set(text, translated)
            else:
                result.failed += 1

    if global_tm:
        global_tm.flush()
        global_tm.close()

    if state:
        state.save()

    result.elapsed_sec = round(time.monotonic() - start, 2)
    result.fatal_error = config.fatal_error_box.get("reason", "")
    status = "⏹ Прервано" if result.cancelled else ("⛔ Остановлено" if result.fatal_error else "Готово")
    _log(
        f"\n[Translator] {status}: {result.succeeded}/{result.total} строк, "
        f"из кэша: {result.from_cache}, время: {result.elapsed_sec}с",
        log_callback,
    )
    return result


def _do_polish_batch(
    provider,
    pairs: list[tuple[str, str]],
    config: TranslatorConfig,
    log_cb: Callable[[str], None] | None,
    cancel_event=None,
) -> tuple[bool, list[str]]:
    """
    Полирует батч пар (original, draft) с retry — та же логика rate-limit /
    экспоненциальной задержки, что и в _do_translate_batch. При полном
    провале делит батч пополам и пробует снова. Возвращает (success, texts),
    где texts при неудаче — ЧЕРНОВИКИ без изменений (безопасный откат,
    а не пустые строки: полировка никогда не должна стирать существующий
    перевод, только предлагать его улучшение).
    """
    delay = config.retry_delay
    drafts = [d for _, d in pairs]

    for attempt in range(1, config.max_retries + 1):
        if cancel_event and cancel_event.is_set():
            return False, drafts
        if config.fatal_error_box:
            return False, drafts
        try:
            polished = provider.polish_batch(pairs, log_cb=log_cb)
            if len(polished) != len(pairs):
                raise ValueError(
                    f"Получено {len(polished)} строк, ожидалось {len(pairs)} — повтор"
                )
            empty_count = sum(1 for t in polished if not t.strip())
            if empty_count > len(pairs) * 0.5:
                raise ValueError(
                    f"Слишком много пустых ответов ({empty_count}/{len(pairs)}) — повтор"
                )
            _log(f"  ✓ Батч полировки готов (попытка {attempt})", log_cb)
            return True, polished
        except Exception as e:
            reason = _classify_permanent_error(e)
            if reason:
                _record_fatal(config, reason, e, log_cb)
                return False, drafts
            err_str = str(e).lower()
            is_rate_limit = (
                "429" in err_str or "rate limit" in err_str or
                "quota" in err_str or "resource exhausted" in err_str or
                "too many requests" in err_str
            )
            if is_rate_limit:
                server_wait = _extract_retry_after(e)
                if server_wait is not None:
                    wait_time = min(server_wait + 1.0, 300.0)
                    _log(f"  ⏳ Rate limit! Сервер: ждём {wait_time:.0f}с (Retry-After)...", log_cb)
                else:
                    wait_time = min(delay * 4, 120.0)
                    _log(f"  ⏳ Rate limit! Ждём {wait_time:.0f}с...", log_cb)
            else:
                wait_time = delay
                _log(f"  ✗ Попытка {attempt}/{config.max_retries}: {e}", log_cb)
            if attempt < config.max_retries:
                _log(f"  ↻ Повтор через {wait_time:.1f}с...", log_cb)
                # BUG FIX: see the identical fix in _do_translate_batch — without
                # a cancel_event (e.g. `cli.py polish`), this used to skip the
                # wait entirely instead of actually backing off before retrying.
                if cancel_event:
                    cancelled = cancel_event.wait(wait_time)
                else:
                    time.sleep(wait_time)
                    cancelled = False
                if cancelled:
                    _log("  ⏹ Ожидание прервано по запросу пользователя.", log_cb)
                    return False, drafts
                delay = min(delay * 2, 60.0)

    if len(pairs) > 1:
        half = len(pairs) // 2
        _log(f"  ↗ Делю батч полировки пополам ({half} + {len(pairs) - half} строк)...", log_cb)
        ok1, part1 = _do_polish_batch(provider, pairs[:half], config, log_cb, cancel_event)
        ok2, part2 = _do_polish_batch(provider, pairs[half:], config, log_cb, cancel_event)
        if ok1 or ok2:
            return True, part1 + part2

    _log(f"  ✗ Батч полировки из {len(pairs)} строк не удался — черновики оставлены как есть.", log_cb)
    return False, drafts


def polish_texts(
    pairs: list[tuple[str, str]],
    config: TranslatorConfig,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
    cancel_event=None,
) -> PolishBatchResult:
    """
    Повторный AI-проход по уже переведённым строкам: НЕ переводит с нуля, а
    предлагает более естественную формулировку уже готового перевода,
    сохраняя смысл, тон и escape-коды.

    pairs: список (original, current_translation).

    Полировке подлежат только пары, уже прошедшие _validate_codes — пара
    с изначально битыми кодами пропускается (для восстановления кодов есть
    отдельный, уже существующий путь: _repair_codes / _validate_translations).
    Если предложенный вариант почему-то не проходит _validate_codes (модель
    случайно задела код) — правка отбрасывается, черновик остаётся как был.
    Полировка — это только "сделать лучше", никогда "сделать хуже или короче".
    """
    start = time.monotonic()
    result = PolishBatchResult(total=len(pairs))

    if not pairs:
        return result

    try:
        provider = _get_provider(config)
    except Exception as e:
        _log(f"[Polish] ✗ Не удалось создать провайдера: {e}", log_callback)
        result.results = [PolishResult(o, d, d, False, False) for o, d in pairs]
        result.failed = len(pairs)
        result.elapsed_sec = round(time.monotonic() - start, 2)
        return result

    # ── Глобальная Translation Memory ────────────────────────────────────
    # Улучшенный полировкой перевод так же обновляет кросс-проектный кэш,
    # как это уже делает translate_texts для свежих переводов — иначе
    # улучшение остаётся только в текущем проекте, а следующий проект с
    # тем же исходным текстом снова получит неотполированный вариант.
    global_tm = None
    if config.use_global_tm:
        try:
            from global_tm import GlobalTM
            # BUG FIX (P1-22, внешний аудит): tm_namespace уже смешивает
            # target_lang+game_type+style_profile в одну составную строку,
            # передаваемую как параметр target_lang в GlobalTM (де-факто
            # composite key внутри одной колонки — так изоляция по движку
            # и стилю уже была). Не было ТОЛЬКО source_lang — одинаковый
            # source-текст, переведённый из РАЗНЫХ исходных языков (или с
            # разным source_lang между проектами), мог получить чужой
            # закэшированный перевод. Добавляем source_lang в ту же
            # составную строку — без изменения схемы БД и без миграции
            # существующих данных (старые записи просто перестают
            # совпадать с новым namespace, что безопасно — они не
            # удаляются и не портятся, просто больше не используются для
            # новых, более точных по языку запросов).
            tm_namespace = (f"{config.source_lang}|{config.target_lang}|{config.game_type}|{config.style_profile or 'default'}"
                           if config.game_type not in ("", "unknown") else f"{config.source_lang}|{config.target_lang}")
            global_tm = GlobalTM(target_lang=tm_namespace).open()
        except Exception as e:
            _log(f"[Polish] ⚠ Global TM недоступна: {e}", log_callback)
            global_tm = None

    # BUG FIX: раньше результаты добавлялись в порядке обработки (сначала все
    # пропущенные, потом обработанные батчами) — это НЕ порядок входных pairs.
    # Любой вызывающий код, полагающийся на zip(pairs, result.results) по
    # позиции (как делает translate_texts для своих результатов), получил бы
    # рассинхронизацию original/before/after. Строим results по индексам,
    # как это уже делает translate_texts через enumerate(texts).
    ordered: list[PolishResult | None] = [None] * len(pairs)
    eligible_indices: list[int] = []
    for idx, (original, draft) in enumerate(pairs):
        if original and draft and _validate_engine_tokens(original, draft, config.game_type):
            eligible_indices.append(idx)
        else:
            ordered[idx] = PolishResult(original, draft, draft, False, False)
            result.skipped += 1

    done = 0
    batch_size = max(config.batch_size, 1)
    for i in range(0, len(eligible_indices), batch_size):
        if cancel_event and cancel_event.is_set():
            result.cancelled = True
            for idx in eligible_indices[i:]:
                original, draft = pairs[idx]
                ordered[idx] = PolishResult(original, draft, draft, False, False)
            break

        if config.fatal_error_box:
            # Постоянная ошибка API: оставшиеся строки не обрабатывались — возвращаем
            # черновики как есть (полировка никогда не стирает существующий перевод).
            for idx in eligible_indices[i:]:
                original, draft = pairs[idx]
                ordered[idx] = PolishResult(original, draft, draft, False, False)
                result.failed += 1
            break

        batch_indices = eligible_indices[i:i + batch_size]
        batch = [pairs[idx] for idx in batch_indices]
        ok, polished = _do_polish_batch(provider, batch, config, log_callback, cancel_event)

        for idx, (original, before), after_raw in zip(batch_indices, batch, polished):
            after = _clean_translation(after_raw) if (ok and after_raw) else before
            if ok and after and _validate_engine_tokens(original, after, config.game_type):
                changed = after.strip() != before.strip()
                ordered[idx] = PolishResult(original, before, after, changed, True)
                result.succeeded += 1
                if changed:
                    result.changed += 1
                    if global_tm:
                        global_tm.set(original, after)
            else:
                # Безопасный откат: правка отклонена (коды не совпали или
                # батч не удался) — черновик остаётся нетронутым.
                ordered[idx] = PolishResult(original, before, before, False, ok)
                if ok:
                    result.succeeded += 1
                else:
                    result.failed += 1

        done += len(batch)
        if progress_callback:
            progress_callback(done, len(eligible_indices), -1.0, "")
        if config.request_delay > 0 and i + batch_size < len(eligible_indices):
            (cancel_event.wait(config.request_delay) if cancel_event else time.sleep(config.request_delay))

    # Защитный фильтр: по построению ordered не должен содержать None (каждый
    # индекс покрывается либо ранним skip, либо обработкой батча, либо веткой
    # отмены), но не полагаемся на это молча — лучше потерять запись, чем
    # уронить всю полировку на AttributeError на None.
    result.results = [r for r in ordered if r is not None]

    if global_tm:
        global_tm.flush()
        global_tm.close()

    result.elapsed_sec = round(time.monotonic() - start, 2)
    result.fatal_error = config.fatal_error_box.get("reason", "")
    status = "⏹ Прервано" if result.cancelled else ("⛔ Остановлено" if result.fatal_error else "Готово")
    _log(
        f"\n[Polish] {status}: {result.succeeded}/{result.total} строк обработано, "
        f"улучшено: {result.changed}, без изменений: {result.succeeded - result.changed}, "
        f"пропущено (битые коды): {result.skipped}, время: {result.elapsed_sec}с",
        log_callback,
    )
    return result
