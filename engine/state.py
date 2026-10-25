"""
state.py — сохранение и загрузка состояния перевода.

Позволяет прерывать и возобновлять перевод с того места, где остановились.
Сохраняет кэш переводов: если строка уже переведена — не трогаем.

Модель статусов строки (P0.1)
-----------------------------
Каждая строка в кэше имеет статус происхождения/защиты:

    EXTRACTED      — перевода ещё нет (неявный: строки нет в кэше)
    AI_TRANSLATED  — перевод получен от ИИ (неявный по умолчанию)
    MANUAL_EDITED  — пользователь правил текст руками (GUI-таблица, CSV-импорт)
    APPROVED       — пользователь явно одобрил перевод
    LOCKED         — пользователь заблокировал строку
    STALE          — исходный текст изменился после перевода
    NEEDS_REVIEW   — требуется проверка человеком

Правило: MANUAL_EDITED / APPROVED / LOCKED защищены — автоматические
операции (ИИ-перевод, ИИ-полировка, глоссарий-постпроцессинг, авто-ремонт
кодов) их не перезаписывают. LOCKED не перезаписывается даже явным запросом
пользователя «переперевести» — сначала нужно снять блокировку.

Формат файла обратно совместим: статусы лежат в необязательном ключе
"status" (только нестандартные), старые файлы без него читаются как есть.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from error_utils import console_print

STATE_FILE = "translation_state.json"
BACKUP_SUFFIX = ".bak"
CORRUPT_MARKER = ".corrupt-"

# fsync перед rename переживает внезапное отключение питания, но стоит
# миллисекунды на каждое сохранение. Тесты (и тот, кто сознательно хочет
# скорость вместо durability) отключают через RPGMTL_NO_FSYNC=1.
FSYNC_ON_SAVE = os.environ.get("RPGMTL_NO_FSYNC", "") != "1"

# ── статусы строки ──────────────────────────────────────────────
EXTRACTED = "extracted"
AI_TRANSLATED = "ai_translated"
MANUAL_EDITED = "manual_edited"
APPROVED = "approved"
LOCKED = "locked"
STALE = "stale"
NEEDS_REVIEW = "needs_review"

ALL_STATUSES = frozenset(
    {EXTRACTED, AI_TRANSLATED, MANUAL_EDITED, APPROVED, LOCKED, STALE, NEEDS_REVIEW}
)
# Статусы, которые нельзя задать вручную через mark(): EXTRACTED вычисляется
# (нет перевода), AI_TRANSLATED означает «снять явный статус».
PROTECTED_STATUSES = frozenset({MANUAL_EDITED, APPROVED, LOCKED})
_EXPLICIT_STATUSES = frozenset({MANUAL_EDITED, APPROVED, LOCKED, STALE, NEEDS_REVIEW})

# Источники записи (см. TranslationState.set)
SOURCE_AI = "ai"          # ИИ-перевод / ИИ-полировка
SOURCE_MANUAL = "manual"  # человек (GUI-правка, CSV-импорт)
SOURCE_SYSTEM = "system"  # автоматическое обслуживание (ремонт кодов, глоссарий)
SOURCE_SYNC = "sync"      # подтягивание текста из *_translated.json (файл — истина)
_SOURCES = frozenset({SOURCE_AI, SOURCE_MANUAL, SOURCE_SYSTEM, SOURCE_SYNC})


def _log(msg: str, cb: Callable[[str], None] | None = None):
    console_print(msg)
    if cb:
        cb(msg)


class StateFormatError(ValueError):
    """Файл состояния прочитан, но имеет неверную структуру."""


def _parse_state_file(path: Path) -> tuple[dict, dict, dict]:
    """Прочитать и ПРОВАЛИДИРОВАТЬ файл состояния → (cache, meta, status).

    Бросает исключение (OSError / JSONDecodeError / StateFormatError), если
    файл нечитаем или структура неверна — вызывающий решает, что с этим делать.
    Молча «починить» повреждённый файл здесь нельзя: именно так терялись
    переводы (см. TranslationState.load).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise StateFormatError("корневой элемент не объект JSON")
    cache = data.get("cache", {})
    meta = data.get("meta", {})
    status = data.get("status", {})
    if not isinstance(cache, dict):
        raise StateFormatError("поле 'cache' не объект")
    if not isinstance(meta, dict):
        raise StateFormatError("поле 'meta' не объект")
    if not isinstance(status, dict):
        raise StateFormatError("поле 'status' не объект")
    bad = [k for k, v in cache.items() if not isinstance(v, str)]
    if bad:
        raise StateFormatError(f"значения кэша не строки (например, для {bad[0][:40]!r})")
    # Статусы: оставляем только известные и только для строк, которые есть в кэше.
    clean_status = {
        k: v for k, v in status.items()
        if isinstance(v, str) and v in _EXPLICIT_STATUSES and k in cache
    }
    return cache, meta, clean_status


class TranslationState:
    """
    Кэш переводов с поддержкой persist/resume и статусами строк.
    Ключ: нормализованный (strip) оригинальный текст.
    """

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self._cache: dict[str, str] = {}
        self._status: dict[str, str] = {}   # только явные статусы
        self._meta: dict = {}
        self._dirty = False
        self._dirty_count = 0  # BUG FIX: track additions since last save for reliable periodic saves
        # Диагностика загрузки (см. load()):
        self.load_error: str | None = None       # почему основной файл не прочитан
        self.corrupt_copy: Path | None = None    # куда отложен повреждённый файл
        self.recovered_from_backup: bool = False
        # True, если файл на диске прочитан/записан этим объектом и валиден —
        # тогда save() может скопировать его в .bak без повторного разбора.
        self._disk_valid: bool = False
        # Сколько записей ИИ/автоматики было отклонено защитой в этой сессии:
        self.protected_skipped: int = 0
        # BUG FIX: конструктор не загружал существующий кэш автоматически.
        # TranslationState(path) с существующим файлом возвращал пустой объект.
        # Любой код, создающий экземпляр напрямую (не через load_state()),
        # молча терял всю историю переводов.
        if self.state_path.exists():
            self.load()

    # ── пути ────────────────────────────────────────────────────
    @property
    def backup_path(self) -> Path:
        return self.state_path.with_name(self.state_path.name + BACKUP_SUFFIX)

    # ── загрузка ────────────────────────────────────────────────
    def _quarantine(self) -> Path | None:
        """Отложить повреждённый файл рядом, НЕ удаляя его."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = self.state_path.with_name(f"{self.state_path.name}{CORRUPT_MARKER}{stamp}")
        n = 1
        while target.exists():
            n += 1
            target = self.state_path.with_name(
                f"{self.state_path.name}{CORRUPT_MARKER}{stamp}_{n}"
            )
        try:
            os.replace(self.state_path, target)
            return target
        except OSError:
            try:
                shutil.copy2(self.state_path, target)
                return target
            except OSError:
                return None

    def load(self) -> bool:
        """Загрузить кэш. НИКОГДА не теряет данные молча.

        BUG FIX (потеря данных): раньше любое исключение при чтении давало
        `return False` и пустой кэш, а следующий save() затирал повреждённый
        файл — все переводы (включая ручные правки) пропадали без единого
        сообщения и без копии. Эмпирически воспроизведено: обрезанный
        translation_state.json (диск, kill, ручная правка JSON) → 0 записей →
        следующее сохранение перезаписывает файл.

        Теперь: повреждённый файл откладывается в *.corrupt-<время> (не
        удаляется), затем пробуется резервная копия *.bak; причина и пути
        доступны в load_error / corrupt_copy / recovered_from_backup, а
        load_state() сообщает об этом пользователю.
        """
        self.load_error = None
        self.corrupt_copy = None
        self.recovered_from_backup = False
        if not self.state_path.exists():
            return False
        try:
            self._cache, self._meta, self._status = _parse_state_file(self.state_path)
            self._disk_valid = True
            return True
        except Exception as e:  # noqa: BLE001 — любая причина нечитаемости
            self.load_error = f"{type(e).__name__}: {e}"

        # Основной файл повреждён: сначала сохраняем его, потом ищем замену.
        self.corrupt_copy = self._quarantine()
        self._disk_valid = False
        self._cache, self._meta, self._status = {}, {}, {}
        bak = self.backup_path
        if bak.exists():
            try:
                self._cache, self._meta, self._status = _parse_state_file(bak)
                self.recovered_from_backup = True
                # Основной файл отложен в карантин — пересоздаём его из резервной
                # копии НЕМЕДЛЕННО. Иначе следующий читатель (GUI опрашивает
                # состояние каждые 3 с через get_stats) увидит «файла нет» и начнёт
                # с пустого кэша, потеряв только что восстановленные переводы.
                self._dirty = True
                self.save()
                return True
            except Exception as e:  # noqa: BLE001
                self.load_error += f"; резервная копия тоже нечитаема ({type(e).__name__}: {e})"
        return False

    # ── сохранение ──────────────────────────────────────────────
    def save(self):
        if not self._dirty:
            return
        self._meta["last_saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._meta["cached_count"] = len(self._cache)
        data: dict = {"meta": self._meta, "cache": self._cache}
        live_status = {k: v for k, v in self._status.items() if k in self._cache}
        if live_status:
            data["status"] = live_status
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
                    f.flush()
                    if FSYNC_ON_SAVE:
                        try:
                            os.fsync(f.fileno())  # пережить внезапное отключение питания
                        except OSError:
                            pass
                # Резервная копия ПРЕДЫДУЩЕГО рабочего файла — страховка на случай,
                # если новый файл окажется испорчен снаружи. Копируем только
                # валидный файл, чтобы не размножать повреждение.
                if self.state_path.exists():
                    try:
                        if not self._disk_valid:
                            _parse_state_file(self.state_path)  # файл мог появиться/измениться извне
                        shutil.copyfile(self.state_path, self.backup_path)
                    except Exception:
                        pass
                Path(tmp).replace(self.state_path)
                self._disk_valid = True
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

    # ── чтение ──────────────────────────────────────────────────
    def get(self, original: str) -> str | None:
        return self._cache.get(original.strip())

    def has(self, original: str) -> bool:
        return original.strip() in self._cache

    def items(self):
        """Вернуть пары (original, translated) из кэша (публичный API)."""
        return self._cache.items()

    @property
    def count(self) -> int:
        return len(self._cache)

    # ── статусы ─────────────────────────────────────────────────
    def status_of(self, original: str) -> str:
        """EXTRACTED (нет перевода) | AI_TRANSLATED (по умолчанию) | явный статус."""
        key = original.strip()
        if key not in self._cache:
            return EXTRACTED
        return self._status.get(key, AI_TRANSLATED)

    def is_protected(self, original: str) -> bool:
        key = original.strip()
        return key in self._cache and self._status.get(key) in PROTECTED_STATUSES

    def protected_originals(self) -> set[str]:
        return {k for k, s in self._status.items() if s in PROTECTED_STATUSES and k in self._cache}

    def explicit_statuses(self) -> dict[str, str]:
        """Копия карты {оригинал: явный статус} (для GUI-подсветки)."""
        return {k: v for k, v in self._status.items() if k in self._cache}

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {AI_TRANSLATED: 0}
        for key in self._cache:
            st = self._status.get(key, AI_TRANSLATED)
            counts[st] = counts.get(st, 0) + 1
        return counts

    def mark(self, original: str, status: str) -> bool:
        """Явно выставить статус строки, которая есть в кэше.

        AI_TRANSLATED снимает явный статус (в том числе защиту). Возвращает
        False, если строки нет в кэше или статус неизвестен/не задаётся вручную.
        """
        key = original.strip()
        if key not in self._cache:
            return False
        if status == AI_TRANSLATED:
            if key in self._status:
                del self._status[key]
                self._dirty = True
                self._dirty_count += 1
            return True
        if status not in _EXPLICIT_STATUSES:
            return False
        if self._status.get(key) != status:
            self._status[key] = status
            self._dirty = True
            self._dirty_count += 1
        return True

    # ── запись ──────────────────────────────────────────────────
    def set(
        self,
        original: str,
        translated: str,
        *,
        source: str = SOURCE_AI,
        force: bool = False,
    ) -> bool:
        """Записать перевод. Возвращает True, если запись выполнена.

        source:
          "ai"      — ИИ-перевод/полировка. Не перезаписывает MANUAL_EDITED /
                      APPROVED / LOCKED. force=True разрешает перезаписать
                      MANUAL_EDITED и APPROVED (явный запрос пользователя
                      «переперевести эти строки»); LOCKED — никогда.
                      Успешная запись ИИ сбрасывает явный статус (STALE,
                      NEEDS_REVIEW → снова AI_TRANSLATED).
          "manual"  — человек. Всегда пишет; статус MANUAL_EDITED (LOCKED
                      остаётся LOCKED, текст при этом обновляется).
          "system"  — автоматическое обслуживание (ремонт кодов, глоссарий).
                      Защищённые строки не трогает; статус не меняет.
          "sync"    — подтягивание текста из *_translated.json (файл — истина
                      для правок, сделанных вне pipeline). Пишет всегда;
                      статус не меняет.
        """
        if source not in _SOURCES:
            raise ValueError(f"unknown source {source!r}; expected one of {sorted(_SOURCES)}")
        key = original.strip()
        if not key or not translated:
            return False

        current = self._status.get(key) if key in self._cache else None

        if source == SOURCE_AI:
            if current == LOCKED or (current in PROTECTED_STATUSES and not force):
                self.protected_skipped += 1
                return False
            self._status.pop(key, None)
        elif source == SOURCE_SYSTEM:
            if current in PROTECTED_STATUSES:
                self.protected_skipped += 1
                return False
        elif source == SOURCE_MANUAL:
            self._status[key] = LOCKED if current == LOCKED else MANUAL_EDITED
        # SOURCE_SYNC: статус не трогаем

        self._cache[key] = translated
        self._dirty = True
        self._dirty_count += 1
        return True

    # ── мета ────────────────────────────────────────────────────
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
            "protected": len(self.protected_originals()),
            "by_status": self.status_counts(),
        }

    def clear(self):
        self._cache = {}
        self._status = {}
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

    if state.load_error:
        # Повреждение — ВСЕГДА громко, это потенциальная потеря перевода.
        where = f" Копия повреждённого файла: {state.corrupt_copy.name}." if state.corrupt_copy else ""
        if state.recovered_from_backup:
            _log(
                f"[State] ⚠ Файл состояния повреждён ({state.load_error}).{where} "
                f"Восстановлено {state.count} переводов из резервной копии "
                f"{state.backup_path.name}.",
                log_cb,
            )
        else:
            _log(
                f"[State] ✗ Файл состояния повреждён и НЕ восстановлен ({state.load_error}).{where} "
                "Перевод начнётся с пустого кэша; исходный файл сохранён — "
                "его можно починить вручную и вернуть на место.",
                log_cb,
            )
    if state.count > 0:
        protected = len(state.protected_originals())
        extra = f", защищено от перезаписи: {protected}" if protected else ""
        _log(
            f"[State] Загружен кэш: {state.count} переводов{extra} "
            f"(последнее сохранение: {state.get_meta('last_saved', 'неизвестно')})",
            log_cb,
        )
    elif state_path.exists():
        _log("[State] Файл кэша существует, но пуст — новая сессия.", log_cb)
    else:
        _log("[State] Новая сессия перевода (кэш пуст).", log_cb)
    return state


def record_edits(
    output_dir: Path,
    *,
    manual: dict[str, str] | None = None,
    ai: dict[str, str] | None = None,
    sync: dict[str, str] | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Записать в состояние правки, сделанные ВНЕ pipeline (GUI-таблица, CSV-импорт).

    manual — {оригинал: текст}, набранные человеком → статус MANUAL_EDITED.
    ai     — {оригинал: текст}, полученные явным запросом пользователя
             «переперевести/отполировать выбранные строки» → пишутся с force
             (но LOCKED не пробивают).
    sync   — {оригинал: текст}: механическая правка уже существующего текста
             (массовая замена) — текст обновляется, статус НЕ меняется.
    Возвращает счётчики записанных/отклонённых строк.
    """
    state = load_state(Path(output_dir), log_cb=log_cb or (lambda _m: None))
    result = {"manual": 0, "ai": 0, "ai_blocked": 0, "sync": 0}
    for orig, text in (sync or {}).items():
        if state.set(orig, text, source=SOURCE_SYNC):
            result["sync"] += 1
    for orig, text in (manual or {}).items():
        if state.set(orig, text, source=SOURCE_MANUAL):
            result["manual"] += 1
    for orig, text in (ai or {}).items():
        if state.set(orig, text, source=SOURCE_AI, force=True):
            result["ai"] += 1
        else:
            result["ai_blocked"] += 1
    state.save()
    return result


def record_status(
    output_dir: Path,
    items: list[tuple[str, str]],
    status: str,
    log_cb: Callable[[str], None] | None = None,
) -> int:
    """Выставить статус строкам [(оригинал, текущий_перевод)].

    Если строки ещё нет в кэше (перевод загружен из файла/CSV и ни разу не
    проходил через pipeline), она сначала добавляется с текущим текстом
    (source="sync"), иначе mark() не смог бы её защитить. Возвращает число
    строк, которым статус выставлен.
    """
    state = load_state(Path(output_dir), log_cb=log_cb or (lambda _m: None))
    done = 0
    for orig, text in items:
        if not state.has(orig):
            state.set(orig, text, source=SOURCE_SYNC)
        if state.mark(orig, status):
            done += 1
    state.save()
    return done
