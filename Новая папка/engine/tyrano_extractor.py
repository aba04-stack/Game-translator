"""
tyrano_extractor.py — извлечение текста из TyranoBuilder/TyranoScript игр.

TyranoScript использует файлы .ks в папке data/scenario.
Диалоги могут быть как чистым текстом, так и в макросах [chara_ptext name="Speaker"].
Также реплики часто начинаются с #Speaker.
"""

from engine.file_utils import atomic_write_json
import hashlib
import re
from pathlib import Path
from typing import Callable

from .text_filter import should_translate

# ── Паттерны TyranoScript ──────────────────────────────────────────────────

# Теги [tag arg=val]
_TAG_RE = re.compile(r'\[/?[a-zA-Z0-9_]+(?:\s+[^\]]*)?\]')

# Идентификаторы изображений/переменных: слова_через_подчёркивание, без пробелов.
# Такие строки — имена спрайтов, переменных JS — НЕ переводятся:
# после замены TyranoBuilder вычисляет русский текст как JS и падает с
# "ReferenceError: Стоящая_иллюстрация_старшеклассницы... is not defined".
_TYRANO_IDENT_RE = re.compile(r'^[\w]+(?:_[\w]+)+$')

# #Speaker (в TyranoScript решётка часто обозначает спикера)
_SPEAKER_RE = re.compile(r'^#([^\n]+)$')

def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)

class TyranoExtractor:
    def __init__(self, game_dir: Path, log_cb: Callable | None = None):
        self.game_dir = Path(game_dir)
        self.log_cb = log_cb
        self.entries: list[dict] = []

    def _log(self, msg: str):
        _log(msg, self.log_cb)

    def extract(self, progress_cb: Callable | None = None) -> list[dict]:
        # BUG FIX: сценарии бывают в data/scenario, tyrano/scenario или scenario/
        # Ищем все возможные расположения.
        _SCENARIO_CANDIDATES = [
            self.game_dir / "data" / "scenario",
            self.game_dir / "tyrano" / "scenario",
            self.game_dir / "scenario",
        ]
        scenario_dir = next((p for p in _SCENARIO_CANDIDATES if p.exists()), None)
        if scenario_dir is None:
            raise FileNotFoundError(
                f"TyranoScript: папка scenario не найдена в {self.game_dir}.\n"
                f"  Проверено: {[str(p) for p in _SCENARIO_CANDIDATES]}"
            )

        files = sorted(scenario_dir.glob("*.ks"))
        self._log(f"[Tyrano] Найдено {len(files)} .ks файлов")

        for idx, ks_path in enumerate(files):
            if progress_cb:
                progress_cb(idx + 1, len(files), -1.0, ks_path.name)

            try:
                # BUG FIX (тот же класс, что найден и исправлен в Kirikiri):
                # раньше файл читался ЖЁСТКО как UTF-8 (errors="replace") без
                # попытки определить исходную кодировку. TyranoScript — веб-
                # движок (JS/HTML5), UTF-8 реалистичный дефолт для него, но
                # часть TyranoBuilder-проектов исторически конвертировались
                # из Kirikiri (тот же .ks/KAG-синтаксис) и могли унаследовать
                # shift_jis/cp932. Если так, старое чтение молча превращало
                # ВЕСЬ японский текст в "�" — то есть Extract работал бы
                # заметно неправильно, а не тихо портил отдельные символы.
                # Определяем кодировку так же, как уже сделано для Kirikiri.
                for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
                    try:
                        text = ks_path.read_text(encoding=enc, errors="strict")
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                else:
                    text = ks_path.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
            except Exception as e:
                self._log(f"  ✗ Ошибка чтения {ks_path.name}: {e}")
                continue

            current_speaker = ""
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or stripped.startswith(";"): # Комментарии
                    continue

                # Проверка на спикера
                spk_match = _SPEAKER_RE.match(stripped)
                if spk_match:
                    current_speaker = spk_match.group(1).strip()
                    continue

                # Если это не тег целиком (например, не [l][r])
                clean_text = _TAG_RE.sub("", stripped).strip()
                if not clean_text or not should_translate(clean_text):
                    continue

                # FIX: Skip strings that look like image/character/script identifiers.
                # In TyranoBuilder, image and variable names appear as bare text lines
                # (e.g. "立ち絵_女子高生_巨乳_正面"). If translated, TyranoBuilder tries
                # to evaluate the Russian result as a JavaScript identifier → ReferenceError.
                # Pattern: only word-chars + underscores, at least one underscore, no spaces.
                if _TYRANO_IDENT_RE.fullmatch(clean_text):
                    continue

                self.entries.append({
                        "file": ks_path.name,
                        "path": f"line:{i}",
                        "original": clean_text,    # только текст → в AI
                        "translated": "",
                        "context": f"say:{current_speaker}" if current_speaker else "narrator",
                        "speaker": current_speaker,
                        "game_type": "tyrano",
                        "full_line": stripped,     # полная строка с тегами → в injector
                        # BUG FIX: was "_full_line" (underscore prefix), which was stripped
                        # by the `if not k.startswith('_')` filter in extract_tyrano()
                        # before saving to JSON. The injector got "" and lost all KAG tags.
                    })

        # Дедупликация в пределах файла
        unique = []
        seen = set()
        for e in self.entries:
            key = (e["file"], e["path"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        self.entries = unique

        self._log(f"[Tyrano] ✓ Извлечено {len(self.entries)} строк")
        return self.entries


def extract_tyrano(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable | None = None,
    progress_callback: Callable | None = None,
) -> dict:
    _log(f"[Tyrano] Начало извлечения: {game_dir}", log_callback)

    ext = TyranoExtractor(game_dir, log_callback)
    entries = ext.extract(progress_cb=progress_callback)

    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list[dict]] = {}
    for e in entries:
        clean = {k: v for k, v in e.items() if not k.startswith('_')}
        by_file.setdefault(clean["file"], []).append(clean)

    for fname, file_entries in by_file.items():
        base = fname.replace("/", "_").replace("\\", "_")
        stem = base.rsplit(".", 1)[0] if "." in base else base
        if len(stem) > 80:
            h = hashlib.md5(stem.encode('utf-8')).hexdigest()[:8]
            stem = f"{stem[:40]}__{h}__{stem[-30:]}"
        safe = stem + ".json"
        
        for e in file_entries:
            e["file"] = safe
            e["_source_ks"] = fname
            
        out = extract_dir / safe
        atomic_write_json(out, file_entries)

    index = {
        "game_dir": str(game_dir),
        "game_type": "tyrano",
        "total_strings": len(entries),
        "files": {f: len(e) for f, e in by_file.items()},
    }
    atomic_write_json(output_dir / "extract_index.json", index)
    _log(f"[Tyrano] ✓ Сохранено: {len(entries)} строк", log_callback)
    return index

