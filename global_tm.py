"""
global_tm.py — Cross-project Translation Memory backed by SQLite.

Хранится в ~/.rpgmtl/global_tm.db
Ускоряет перевод: строки из предыдущих игр не отправляются в API повторно.
Используется как дополнительный слой кэша поверх per-game state.json.

Только безкодовые строки попадают в глобальный TM — строки с escape-кодами
(\\C[2], \\N[1], §0§) слишком контекстно-зависимы и могут давать неверные
переводы в другой игре.
"""
import re
import sqlite3
import time
import os
from pathlib import Path
from typing import Callable, Optional

# Строки с RPG-кодами не включаем в глобальный TM — слишком контекстно-зависимы
_CODE_RE = re.compile(r"§\d+§|\\[CNVIBSPTFGcnvibsptfg][\[{]|\\\\|\\\.", re.IGNORECASE)

# Минимальная длина строки для записи в TM (слишком короткие = шум)
_MIN_LEN = 4

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm (
    source       TEXT    NOT NULL,
    target_lang  TEXT    NOT NULL,
    translation  TEXT    NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 1,
    updated_at   REAL    NOT NULL,
    PRIMARY KEY (source, target_lang)
);
CREATE INDEX IF NOT EXISTS tm_source ON tm (source, target_lang);
"""


def _qualifies(source: str) -> bool:
    """Проверить что строка подходит для сохранения в глобальный TM."""
    s = source.strip()
    if len(s) < _MIN_LEN:
        return False
    if _CODE_RE.search(s):
        return False
    return True


class GlobalTM:
    """
    Глобальная Translation Memory между проектами.

    Использование:
        with GlobalTM(target_lang="Russian") as tm:
            cached = tm.get("こんにちは")
            if cached is None:
                translation = ai_translate(...)
                tm.set("こんにちは", translation)

    ВАЖНО (P1-24, внешний аудит — проверено, на сейчас не баг, но держать
    в уме): open() создаёт connection с check_same_thread=False, что
    технически разрешает вызывать get()/set()/bulk_lookup() из ЛЮБОГО
    потока — но никакой блокировки на уровне класса нет. Сейчас это
    безопасно только потому, что весь реальный вызывающий код
    (ai_translator.translate_texts/polish_texts) обращается к GlobalTM
    исключительно ПОСЛЕ выхода из `with ThreadPoolExecutor(...)` — то есть
    строго последовательно из одного потока (плюс sqlite3.threadsafety==3
    в большинстве сборок Python — serialized mode — как дополнительная
    защита). Если когда-нибудь появится вызов set()/get() ИЗНУТРИ
    воркер-потока (внутри `with ThreadPoolExecutor`), это предположение
    сломается — в таком случае нужен threading.Lock вокруг операций.
    """

    # См. BUG FIX в config.py: RPGMTL_HOME (выставляется в tests/conftest.py
    # для всей тестовой сессии) не даёт тестам писать реальную SQLite TM-базу
    # в ~/.rpgmtl/global_tm.db настоящего пользователя. Вне тестов переменная
    # не установлена — поведение не меняется.
    DB_PATH = Path(os.environ.get("RPGMTL_HOME", str(Path.home()))) / ".rpgmtl" / "global_tm.db"

    def __init__(self, target_lang: str = "Russian", log_cb: Optional[Callable] = None):
        self.target_lang = target_lang
        self._log_cb = log_cb
        self._conn: Optional[sqlite3.Connection] = None
        self._hits = 0
        self._writes = 0
        self._db_error_warned = False

    def _log(self, msg: str) -> None:
        if self._log_cb:
            self._log_cb(msg)

    def _warn_db_error(self, operation: str, exc: Exception) -> None:
        """
        BUG FIX (P1-23, внешний аудит): раньше ЛЮБАЯ ошибка SQLite
        (corruption, lock, permission, disk full) молча превращалась в
        обычный cache miss (None/{}/0) — пользователь никогда не узнавал,
        что Global TM вообще перестала работать, просто видел чуть больше
        обращений к API, чем ожидал. Не делаем ошибку фатальной — GlobalTM
        это необязательная оптимизация, переводы обязаны продолжать
        работать даже если TM недоступна — но предупреждаем ОДИН РАЗ за
        сессию (не спамим лог при persistent-проблеме вроде "нет прав на
        файл", которая иначе повторялась бы на каждом вызове).
        """
        if self._db_error_warned:
            return
        self._db_error_warned = True
        self._log(
            f"[Global TM] ⚠ Ошибка базы данных при {operation}: {exc}. "
            f"Global TM временно недоступна в этой сессии, перевод "
            f"продолжится без общего кэша (только через обычный API)."
        )

    def open(self) -> "GlobalTM":
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.DB_PATH), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DB_SCHEMA)
        self._conn.commit()
        return self

    def close(self) -> None:
        # BUG FIX: close() used to close the connection WITHOUT committing
        # first. set() only commits every 200 writes (self._writes % 200),
        # so any session with < 200 pending writes that reached close()
        # WITHOUT an explicit prior flush() call would silently discard
        # those writes — Python's sqlite3 module rolls back uncommitted
        # changes on close(), it does not commit them implicitly. This bit
        # especially hard because the class's OWN docstring demonstrates
        # "with GlobalTM(...) as tm: ... tm.set(...)" as the idiomatic
        # usage — and __exit__ only ever called close(), never flush() —
        # so following the documented pattern was itself unsafe. Flushing
        # here makes every path to closing (context manager or direct
        # .close()) safe by default, regardless of whether the caller
        # remembers a manual flush() beforehand.
        self.flush()
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "GlobalTM":
        return self.open()

    def __exit__(self, *_) -> None:
        self.close()

    def get(self, source: str) -> Optional[str]:
        """Получить перевод из TM. Возвращает None если не найдено."""
        if not self._conn or not source.strip():
            return None
        try:
            row = self._conn.execute(
                "SELECT translation FROM tm WHERE source=? AND target_lang=?",
                (source.strip(), self.target_lang),
            ).fetchone()
            if row:
                # Инкрементируем счётчик обращений
                self._conn.execute(
                    "UPDATE tm SET hit_count=hit_count+1 WHERE source=? AND target_lang=?",
                    (source.strip(), self.target_lang),
                )
                self._hits += 1
                return row[0]
        except Exception as e:
            self._warn_db_error("чтении из кэша", e)
        return None

    def set(self, source: str, translation: str) -> None:
        """Сохранить перевод в TM (если строка подходит)."""
        if not self._conn or not source.strip() or not translation.strip():
            return
        if not _qualifies(source):
            return
        try:
            self._conn.execute(
                """INSERT INTO tm (source, target_lang, translation, hit_count, updated_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(source, target_lang) DO UPDATE SET
                       translation=excluded.translation,
                       updated_at=excluded.updated_at,
                       hit_count=hit_count+1""",
                (source.strip(), self.target_lang, translation.strip(), time.time()),
            )
            self._writes += 1
            if self._writes % 200 == 0:
                self._conn.commit()
        except Exception as e:
            self._warn_db_error("записи в кэш", e)

    def flush(self) -> None:
        """Принудительный коммит."""
        if self._conn:
            try:
                self._conn.commit()
            except Exception:
                pass

    def bulk_lookup(self, sources: list[str]) -> dict[str, str]:
        """Массовый поиск — эффективнее чем N вызовов get()."""
        if not self._conn or not sources:
            return {}
        result: dict[str, str] = {}
        # SQLite ограничение на IN (999 параметров)
        chunk_size = 500
        for i in range(0, len(sources), chunk_size):
            chunk = [s.strip() for s in sources[i:i + chunk_size] if s.strip()]
            if not chunk:
                continue
            placeholders = ",".join("?" * len(chunk))
            try:
                rows = self._conn.execute(
                    f"SELECT source, translation FROM tm "
                    f"WHERE source IN ({placeholders}) AND target_lang=?",
                    (*chunk, self.target_lang),
                ).fetchall()
                for src, trans in rows:
                    result[src] = trans
                    self._hits += 1
                # BUG FIX: get() инкрементирует hit_count на каждый хит, а
                # bulk_lookup() — основной, горячий путь чтения TM (см.
                # ai_translator.py) — не инкрементировал его вообще. Из-за
                # этого "top_entries по популярности" в get_stats() почти
                # никогда не отражал реальное использование: реальный
                # трафик идёт через bulk_lookup, а не get(). Один батчевый
                # UPDATE на чанк — не N отдельных (та же логика, что и
                # остальные perf-фиксы этой сессии).
                if rows:
                    found_sources = [r[0] for r in rows]
                    found_placeholders = ",".join("?" * len(found_sources))
                    self._conn.execute(
                        f"UPDATE tm SET hit_count=hit_count+1 "
                        f"WHERE source IN ({found_placeholders}) AND target_lang=?",
                        (*found_sources, self.target_lang),
                    )
            except Exception as e:
                self._warn_db_error("массовом поиске в кэше", e)
        return result

    def get_stats(self) -> dict:
        """Статистика TM."""
        if not self._conn:
            return {"total": 0, "languages": []}
        try:
            total = self._conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
            langs = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT target_lang FROM tm ORDER BY target_lang"
            ).fetchall()]
            top = self._conn.execute(
                "SELECT source, translation, hit_count FROM tm "
                "WHERE target_lang=? ORDER BY hit_count DESC LIMIT 10",
                (self.target_lang,),
            ).fetchall()
            return {
                "total": total,
                "languages": langs,
                "session_hits":   self._hits,
                "session_writes": self._writes,
                "top_entries":    [{"src": r[0], "trans": r[1], "hits": r[2]} for r in top],
            }
        except Exception as e:
            self._warn_db_error("получении статистики", e)
            return {"total": 0, "languages": []}

    def purge_old(self, days: int = 180) -> int:
        """Удалить записи старше N дней."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            cur = self._conn.execute(
                "DELETE FROM tm WHERE updated_at < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount
        except Exception:
            return 0

    def export_csv(self, path: Path) -> int:
        """Экспортировать TM в CSV для просмотра/редактирования."""
        if not self._conn:
            return 0
        import csv
        rows = self._conn.execute(
            "SELECT source, target_lang, translation, hit_count FROM tm ORDER BY hit_count DESC"
        ).fetchall()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Source", "Target Language", "Translation", "Hit Count"])
            writer.writerows(rows)
        return len(rows)

    def import_csv(self, path: Path) -> int:
        """Импортировать TM из CSV."""
        if not self._conn:
            return 0
        import csv
        count = 0
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                src   = row.get("Source", "").strip()
                lang  = row.get("Target Language", "").strip()
                trans = row.get("Translation", "").strip()
                if src and lang and trans and _qualifies(src):
                    self._conn.execute(
                        """INSERT INTO tm (source, target_lang, translation, hit_count, updated_at)
                           VALUES (?, ?, ?, 1, ?)
                           ON CONFLICT(source, target_lang) DO UPDATE SET
                               translation=excluded.translation,
                               updated_at=excluded.updated_at""",
                        (src, lang, trans, time.time()),
                    )
                    count += 1
        self._conn.commit()
        return count
