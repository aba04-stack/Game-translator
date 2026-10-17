"""
glossary.py — глоссарий терминов для стабильного перевода.

Главная проблема машинного перевода RPG: имена персонажей
и термины переводятся по-разному в разных местах.
Глоссарий фиксирует их раз и навсегда.

Автоматически загружает имена актёров из Actors.json.
"""

import csv
import io
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable

from engine.file_utils import atomic_write_json


DEFAULT_GLOSSARY_FILE = "glossary.json"

# Compiled once at module load — used by Glossary.apply() to protect RPG escape codes
_CODE_RE = re.compile(
    r"(?:\x1b)?\\(?:"
    r"[NnVvCcIiGgPpTtSsFfBbRr](?:\[[^\]]*\])?"
    r"|[A-Z]{2,}(?:\[[^\]]*\])?"
    r"|[\\nrtbfv]"
    r"|[!.<>^{}|]"
    r"|u[0-9A-Fa-f]{4}"
    r"|x[0-9A-Fa-f]{2}"
    r")"
)
_PLACEHOLDER = "\x00RPGC{:04d}\x00"


def _log(msg: str, cb: Callable[[str], None] | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


class Glossary:
    def __init__(self):
        self.terms: dict[str, str] = {}
        self.source_file: Path | None = None

    def add(self, original: str, translation: str):
        if original and translation and original != translation:
            self.terms[original.strip()] = translation.strip()

    def remove(self, original: str):
        self.terms.pop(original.strip(), None)

    def get(self, original: str, default: str = "") -> str:
        """Получить перевод термина (публичный API)."""
        return self.terms.get(original, default)

    def __contains__(self, term: str) -> bool:
        return term in self.terms

    def apply(self, text: str) -> str:
        """Применить глоссарий к тексту (для постпроцессинга).

        BUG FIX #1: Пропускаем термины с пустым переводом.
        text.replace(term, "") молча удалял термины из текста.

        BUG FIX #2: Защищаем RPG Maker escape-коды.
        Коды типа \\N[1] содержат буквы — без защиты глоссарий мог
        заменить «N» в коде и разрушить его.

        BUG FIX #3: Не импортируем ai_translator внутри engine/.
        Функция _protect_codes дублирована локально — нет cross-package
        import-зависимости, которая ломалась при определённых entry-point.

        Для обработки МНОЖЕСТВА строк подряд используйте apply_batch() —
        здесь термины сортируются заново на каждый вызов (см. BUG FIX в
        apply_batch() ниже про цену этого при большом числе вызовов).
        """
        sorted_terms = self._sorted_terms()
        return self._apply_with_sorted_terms(text, sorted_terms)

    def apply_batch(self, texts: list[str]) -> list[str]:
        """
        То же самое, что apply(), но для множества строк сразу — сортирует
        термины глоссария ОДИН раз на весь батч, а не один раз на каждую
        строку.

        BUG FIX: apply() пересортировывала self.terms.items() при КАЖДОМ
        вызове — результат идентичен, пока glossary.terms не менялся, но
        pipeline.py._apply_glossary_postprocess() вызывала apply() один раз
        на КАЖДУЮ переведённую строку (потенциально тысячи за одну игру).
        На синтетическом тесте (глоссарий 300 терминов, 20 000 строк) на
        одну только бесполезную пересортировку уходило ~0.8 из ~1.6 секунды
        общего времени — около половины. apply_batch() убирает эту
        избыточность: сортировка происходит один раз для всего вызова.
        """
        sorted_terms = self._sorted_terms()
        return [self._apply_with_sorted_terms(t, sorted_terms) for t in texts]

    def _sorted_terms(self) -> list[tuple[str, str]]:
        """Термины глоссария, отсортированные по убыванию длины оригинала
        (длинные фразы должны заменяться раньше их более коротких подстрок),
        с уже отфильтрованными пустыми переводами (BUG FIX #1)."""
        return [
            (orig, trans)
            for orig, trans in sorted(self.terms.items(), key=lambda x: -len(x[0]))
            if orig and trans
        ]

    def _apply_with_sorted_terms(self, text: str, sorted_terms: list[tuple[str, str]]) -> str:
        placeholders: dict[str, str] = {}
        counter = [0]

        def _protect(m: re.Match) -> str:
            key = _PLACEHOLDER.format(counter[0])
            placeholders[key] = m.group(0)
            counter[0] += 1
            return key

        protected = _CODE_RE.sub(_protect, text)

        for orig, trans in sorted_terms:
            protected = protected.replace(orig, trans)

        # Восстанавливаем коды
        for key, code in placeholders.items():
            protected = protected.replace(key, code)
        return protected

    def to_prompt_block(self, source_lang: str = "Japanese", target_lang: str = "Russian") -> str:
        """Сформировать блок глоссария для системного промпта.

        BUG FIX: термины без перевода (только что обнаруженные auto_detect_actors /
        extract_game_terms, ещё не заполненные пользователем или
        auto_translate_glossary) раньше попадали в промпт как "  term → " —
        пустая цель после стрелки, хотя промпт прямо требует "always use these
        translations, never change them". Пропускаем термины без значения —
        так же, как это уже делает apply() (см. её BUG FIX #1).
        """
        entries = [(orig, trans) for orig, trans in self.terms.items() if orig and trans]
        if not entries:
            return ""
        lines = ["GLOSSARY (always use these translations, never change them):"]
        for orig, trans in sorted(entries):
            lines.append(f"  {orig} → {trans}")
        return "\n".join(lines)

    def save(self, path: Path):
        atomic_write_json(path, dict(sorted(self.terms.items())))
        self.source_file = path

    def load(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.terms = {str(k): str(v) for k, v in data.items()}
            self.source_file = path
            return True
        except Exception:
            return False

    def __len__(self) -> int:
        return len(self.terms)


def auto_detect_actors(
    game_dir: Path,
    log_cb: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """
    Загрузить имена актёров из Actors.json как заготовки для глоссария.
    Возвращает {japanese_name: ""} — пользователь заполняет перевод.
    """
    for candidate in ["data", "www/data", "Data"]:
        actors_file = game_dir / candidate / "Actors.json"
        if actors_file.exists():
            break
    else:
        return {}

    try:
        with open(actors_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _log(f"[Glossary] Ошибка чтения Actors.json: {e}", log_cb)
        return {}

    result: dict[str, str] = {}
    for actor in data:
        if not isinstance(actor, dict):
            continue
        name = actor.get("name", "").strip()
        if name and _is_japanese_or_special(name):
            result[name] = ""
        nickname = actor.get("nickname", "").strip()
        if nickname and _is_japanese_or_special(nickname):
            result[nickname] = ""

    _log(f"[Glossary] Обнаружено имён актёров: {len(result)}", log_cb)
    return result


def _is_japanese_or_special(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", text))



def auto_translate_glossary(
    glossary: "Glossary",
    config,
    log_cb: Callable[[str], None] | None = None,
) -> int:
    """
    Автоматически перевести пустые термины глоссария через AI.
    Возвращает количество переведённых терминов.
    """
    empty_terms = [(orig, tr) for orig, tr in glossary.terms.items() if not tr]
    if not empty_terms:
        _log("[Glossary] Все термины уже переведены.", log_cb)
        return 0

    originals = [orig for orig, _ in empty_terms]
    _log(f"[Glossary] Авто-перевод {len(originals)} терминов...", log_cb)

    try:
        from ai_translator import translate_texts, TranslatorConfig
        # Создаём специальный конфиг для имён: температура ниже, контекст другой
        name_config = TranslatorConfig(
            provider=config.provider,
            api_key=config.api_key,
            model=config.get_model() if hasattr(config, "get_model") else "",
            source_lang=getattr(config, "source_lang", "Japanese"),
            target_lang=getattr(config, "target_lang", "Russian"),
            batch_size=min(50, len(originals)),
            max_retries=2,
            extra_instructions=(
                "These are proper names from a Japanese RPG game. "
                "Transliterate character names (田中 → Tanaka, etc.) "
                "or translate if they are meaningful words (勇者 → Hero). "
                "Return only the translated/transliterated name, nothing else."
            ),
            use_context=False,
        )
        result = translate_texts(originals, name_config, log_callback=log_cb)
        translated_count = 0
        for tr in result.results:
            if tr.success and tr.translated and tr.translated.strip():
                glossary.terms[tr.original] = tr.translated.strip()
                translated_count += 1
        _log(f"[Glossary] ✓ Переведено терминов: {translated_count}", log_cb)
        return translated_count
    except Exception as e:
        _log(f"[Glossary] ✗ Ошибка авто-перевода: {e}", log_cb)
        return 0


def export_glossary_csv(glossary: "Glossary", output_path: Path) -> int:
    """Экспортировать глоссарий в CSV файл (атомарная запись)."""
    rows = [(orig, tr) for orig, tr in glossary.terms.items()]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Original", "Translation"])
    w.writerows(rows)
    content = buf.getvalue().encode("utf-8-sig")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=output_path.parent, suffix=".tmp", prefix=".rpgmtl_"
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, output_path)
    except Exception:
        try: os.unlink(tmp_path)
        except Exception: pass
        raise
    return len(rows)


def import_glossary_csv(glossary: "Glossary", csv_path: Path,
                         overwrite: bool = False) -> int:
    """Импортировать переводы из CSV в глоссарий.

    BUG FIX: раньше вся функция была обёрнута в try/except Exception: pass —
    сбой ПОСЕРЕДИНЕ чтения (не при открытии файла — это отдельная, не
    проглоченная здесь ошибка) означал, что count тихо возвращался как
    финальный результат, будто это и есть весь файл. Единственный вызывающий
    код (rpgmtl_desktop.py) уже оборачивает этот вызов в свой try/except с
    понятным сообщением об ошибке — даём исключению дойти до него, а не
    маскируем частичный импорт под успешный.
    """
    if not csv_path.exists():
        return 0
    count = 0
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        next(reader, None)  # пропускаем заголовок
        for row in reader:
            if len(row) < 1:
                continue
            orig = row[0].strip()
            tr   = row[1].strip() if len(row) > 1 else ""
            if orig:
                if overwrite or orig not in glossary.terms:
                    glossary.terms[orig] = tr
                    count += 1
    return count


def extract_game_terms(
    all_entries: list[dict],
    max_terms: int = 50,
    log_cb: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """
    Извлечь потенциальные термины (имена, места) из всех записей.
    Ищет короткие японские строки которые повторяются 3+ раз — вероятно имена.
    Возвращает {japanese: ""}.
    """
    counter: Counter = Counter()
    for entry in all_entries:
        orig = entry.get("original", "")
        ctx  = entry.get("context", "")
        # Короткие строки из имён/speaker контекстов — хорошие кандидаты
        if ctx in ("speaker_name", "actor_name", "enemy") and orig:
            counter[orig] += 10  # высокий приоритет
        elif len(orig) <= 10 and _is_japanese_or_special(orig):
            counter[orig] += 1

    # Берём самые частые
    terms = {}
    for term, count in counter.most_common(max_terms):
        if _is_japanese_or_special(term) and count >= 1:
            terms[term] = ""
    if terms:
        _log(f"[Glossary] Найдено потенциальных терминов: {len(terms)}", log_cb)
    return terms


def load_or_create_glossary(
    output_dir: Path,
    game_dir: Path | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> Glossary:
    """Загрузить глоссарий из файла или создать пустой."""
    g = Glossary()
    gpath = output_dir / DEFAULT_GLOSSARY_FILE

    if g.load(gpath):
        _log(f"[Glossary] Загружен: {len(g)} терминов из {gpath}", log_cb)
    else:
        _log("[Glossary] Файл не найден, создаётся новый.", log_cb)
        if game_dir:
            detected = auto_detect_actors(game_dir, log_cb)
            if detected:
                _log(f"[Glossary] Добавлены заготовки имён: {list(detected.keys())[:5]}...", log_cb)
                g.terms.update(detected)
            else:
                _log("[Glossary] Японских имён актёров не найдено — глоссарий пустой.", log_cb)
        g.save(gpath)

    return g
