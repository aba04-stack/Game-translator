"""
state.py — сохранение и загрузка состояния перевода.

Позволяет прерывать и возобновлять перевод с того места, где остановились.
Сохраняет кэш переводов: если строка уже переведена — не трогаем.
"""

import json
import os
import tempfile
import sys
import time
from pathlib import Path
from typing import Callable

STATE_FILE = "translation_state.json"


def _log(msg: str, cb: Callable[[str], None] | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


class TranslationState:
    """
    Кэш переводов с поддержкой persist/resume.
    Ключ: нормализованный оригинальный текст.
    """

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self._cache: dict[str, str] = {}
        self._meta: dict = {}
        self._dirty = False
        self._dirty_count = 0  # BUG FIX: track additions since last save for reliable periodic saves
        # BUG FIX: конструктор не загружал существующий кэш автоматически.
        # TranslationState(path) с существующим файлом возвращал пустой объект.
        # Любой код, создающий экземпляр напрямую (не через load_state()),
        # молча терял всю историю переводов.
        if self.state_path.exists():
            self.load()

    def load(self) -> bool:
        if not self.state_path.exists():
            return False
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cache = data.get("cache", {})
            self._meta = data.get("meta", {})
            return True
        except Exception:
            return False

    def save(self):
        if not self._dirty:
            return
        self._meta["last_saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._meta["cached_count"] = len(self._cache)
        data = {"meta": self._meta, "cache": self._cache}
        # BUG FIX: атомарная запись через temp-файл + rename.
        # Прямая запись в state_path при прерывании (диск полон, kill) оставляла
        # обрезанный/пустой JSON — кэш терялся и весь перевод шёл заново.

        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self.state_path.parent,
                prefix=".state_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                Path(tmp).replace(self.state_path)
                self._dirty = False
                self._dirty_count = 0
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        except Exception as e:
            print(f"[State] ✗ Ошибка сохранения кэша: {e}", file=sys.stderr, flush=True)

    def save_periodically(self, every_n: int = 50):
        """Сохранить если накопилось достаточно изменений."""
        # BUG FIX: was `len(self._cache) % every_n == 0` which misses saves
        # when cache grows non-linearly (e.g. 49→52). Use a separate dirty counter.
        if self._dirty_count >= every_n:
            self.save()

    def get(self, original: str) -> str | None:
        return self._cache.get(original.strip())

    def set(self, original: str, translated: str):
        key = original.strip()
        if key and translated:
            self._cache[key] = translated
            self._dirty = True
            self._dirty_count += 1

    def has(self, original: str) -> bool:
        return original.strip() in self._cache

    def items(self):
        """Вернуть пары (original, translated) из кэша (публичный API)."""
        return self._cache.items()

    @property
    def count(self) -> int:
        return len(self._cache)

    def set_meta(self, key: str, value):
        self._meta[key] = value
        self._dirty = True

    def get_meta(self, key: str, default=None):
        return self._meta.get(key, default)

    def stats(self) -> dict:
        return {
            "cached": self.count,
            "last_saved": self._meta.get("last_saved", "never"),
            "provider": self._meta.get("provider", "?"),
            "target_lang": self._meta.get("target_lang", "?"),
        }

    def clear(self):
        self._cache = {}
        self._meta = {}
        self._dirty = True
        self.save()


def load_state(output_dir: Path, log_cb: Callable[[str], None] | None = None) -> TranslationState:
    """Загрузить или создать TranslationState.

    TranslationState.__init__ уже вызывает self.load() при наличии файла.
    Повторный вызов state.load() здесь — двойное I/O без пользы.
    """
    state_path = output_dir / STATE_FILE
    state = TranslationState(state_path)        # __init__ загружает файл если есть
    if state.count > 0:
        _log(
            f"[State] Загружен кэш: {state.count} переводов "
            f"(последнее сохранение: {state.get_meta('last_saved', 'неизвестно')})",
            log_cb,
        )
    elif state_path.exists():
        _log("[State] Файл кэша существует, но пуст — новая сессия.", log_cb)
    else:
        _log("[State] Новая сессия перевода (кэш пуст).", log_cb)
    return state
