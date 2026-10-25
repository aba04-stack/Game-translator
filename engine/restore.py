"""
restore.py — восстановление оригинальных файлов игры из резервных копий.

Раньше эта логика жила прямо в GUI (MainWindow._restore_backup): выполнялась в
потоке интерфейса (окно «зависало» на больших играх), а ошибки копирования
глотались (`except (OSError, shutil.Error): pass`) — при этом файл всё равно
засчитывался как восстановленный, и пользователь видел «✓ Восстановлено N
файлов», даже если часть игры осталась с переводом.

Теперь:
  * каждая ошибка копирования фиксируется и попадает в результат;
  * счётчик считает только реально скопированные файлы;
  * маркеры инъекции снимаются ТОЛЬКО при полном успехе — иначе следующий
    Inject не узнал бы, что игра осталась в частично восстановленном состоянии;
  * прогресс — по единому контракту progress(current, total, eta, filename);
  * восстановление можно прервать (cancel_event) и безопасно повторить —
    копирование идемпотентно.
"""
from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Папки резервных копий, которые создают инжекторы разных движков.
BACKUP_DIR_NAMES = (
    "_backup_original",            # RPG Maker MV/MZ
    "_backup_renpy_original",      # Ren'Py
    "_backup_kirikiri_original",   # Kirikiri / KAG
    "_backup_wolf_original",       # Wolf RPG
    "_backup_vxace_original",      # RPG Maker VX/Ace
    "_backup_unity_original",      # Unity
    "_backup_godot_original",      # Godot
    "_backup_tyrano_original",     # TyranoBuilder
)

# Бэкапы этих движков зеркалят структуру игры (вложенные папки) —
# восстанавливаем относительно КОРНЯ игры. Остальные (MV/MZ, VX/Ace) хранят
# плоский набор файлов data/.
_MIRROR_KEYS = ("renpy", "kirikiri", "wolf", "godot", "tyrano", "unity")
DATA_DIR_CANDIDATES = ("data", "www/data", "Data")

MARKER_NAME = ".rpgmtl_injected"
_MARKER_PREFIX = ".rpgmtl"
# Маркер инъекции: у MV/MZ лежит в data-папке, у остальных — внутри папки бэкапа.
_MARKER_BACKUP_DIRS = (
    "_backup_vxace_original",
    "_backup_wolf_original",
    "_backup_kirikiri_original",
    "_backup_godot_original",
    "_backup_tyrano_original",
    "_backup_renpy_original",
    "_backup_unity_original",
)


@dataclass
class RestoreResult:
    restored: int = 0
    total: int = 0
    backups: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (что, причина)
    cancelled: bool = False
    markers_removed: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.backups) and not self.failed and not self.cancelled

    def summary(self) -> str:
        if not self.backups:
            return "Резервная копия не найдена: она создаётся автоматически при первом Inject."
        if self.cancelled:
            return (f"Восстановление прервано: восстановлено {self.restored} из {self.total} файлов. "
                    "Повторите «Восстановить оригинал», чтобы завершить (операция безопасна для повтора).")
        if self.failed:
            return (f"Восстановлено {self.restored} из {self.total} файлов, "
                    f"НЕ удалось: {len(self.failed)}. Игра может быть в частично "
                    "восстановленном состоянии.")
        return f"Восстановлено {self.restored} файлов."


def find_backups(game_path: Path) -> list[Path]:
    """Существующие папки резервных копий внутри игры."""
    game_path = Path(game_path)
    return [game_path / name for name in BACKUP_DIR_NAMES if (game_path / name).exists()]


# Первые сегменты путей, с которых начинается ВЛОЖЕННАЯ раскладка бэкапа MV/MZ.
_GAME_ROOT_PREFIXES = frozenset(c.split("/")[0] for c in DATA_DIR_CANDIDATES)   # {"data", "www", "Data"}


def _plan(game_path: Path, found: list[Path]):
    """Список (источник, назначение, подпись) и ошибки планирования.

    В _backup_original у MV/MZ ДВЕ раскладки одних и тех же файлов — обе создаются
    намеренно:
      * вложенная  (data/Actors.json, www/data/Actors.json) — основной бэкап
        Pipeline.inject; путь отсчитывается от КОРНЯ игры;
      * плоская    (Actors.json) — совместимость со старым кодом восстановления
        и внешними инструментами; путь отсчитывается от папки data.
    Если копировать обе относительно data/, в игре появляется мусорная папка
    data/data/ (проверено эмпирически полным циклом inject → restore). Поэтому
    вложенные файлы возвращаются относительно корня игры, плоские — в data/, а
    файлы с одним назначением копируются один раз (вложенная раскладка главнее).
    """
    pairs: list[tuple[Path, Path, str]] = []
    errors: list[tuple[str, str]] = []
    seen: set[Path] = set()

    def add(src: Path, dest: Path, label: str) -> None:
        if dest in seen:                      # то же назначение уже запланировано
            return
        seen.add(dest)
        pairs.append((src, dest, label))

    for backup_dir in found:
        files = [f for f in sorted(backup_dir.rglob("*"))
                 # Маркеры .rpgmtl_* нельзя копировать обратно в игру: маркер инъекции
                 # в игре заблокировал бы следующий Inject (idempotency guard).
                 if f.is_file() and not f.name.startswith(_MARKER_PREFIX)]

        if any(key in backup_dir.name for key in _MIRROR_KEYS):
            for f in files:
                rel = f.relative_to(backup_dir)
                add(f, game_path / rel, f"{backup_dir.name}/{rel.as_posix()}")
            continue

        data_dir = next(
            (game_path / cand for cand in DATA_DIR_CANDIDATES if (game_path / cand).exists()),
            None,
        )
        flat_without_target = False
        nested, flat = [], []
        for f in files:
            rel = f.relative_to(backup_dir)
            (nested if len(rel.parts) > 1 and rel.parts[0] in _GAME_ROOT_PREFIXES else flat).append((f, rel))
        for f, rel in nested:
            add(f, game_path / rel, f"{backup_dir.name}/{rel.as_posix()}")
        for f, rel in flat:
            if data_dir is None:
                flat_without_target = True
                continue
            add(f, data_dir / rel, f"{backup_dir.name}/{rel.as_posix()}")
        if flat_without_target:
            errors.append((backup_dir.name,
                           "папка data / www/data / Data не найдена — некуда восстанавливать"))
    return pairs, errors


def _remove_markers(game_path: Path, failed: list[tuple[str, str]]) -> int:
    """Снять маркеры инъекции; ошибки удаления — в failed (иначе следующий Inject
    молча упрётся в «уже инжектировано»)."""
    candidates = [game_path / cand / MARKER_NAME for cand in DATA_DIR_CANDIDATES]
    candidates += [game_path / name / MARKER_NAME for name in _MARKER_BACKUP_DIRS]
    removed = 0
    for marker in candidates:
        try:
            if marker.exists():
                marker.unlink()
                removed += 1
        except OSError as e:
            failed.append((f"маркер {marker.parent.name}/{marker.name}", f"{type(e).__name__}: {e}"))
    return removed


def restore_game_backups(
    game_path: Path,
    log_cb: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> RestoreResult:
    """Вернуть оригинальные файлы игры из всех найденных резервных копий."""
    game_path = Path(game_path)
    log = log_cb or (lambda _m: None)
    result = RestoreResult()

    found = find_backups(game_path)
    result.backups = [d.name for d in found]
    if not found:
        log("[Restore] ✗ " + result.summary())
        return result

    pairs, plan_errors = _plan(game_path, found)
    result.failed.extend(plan_errors)
    result.total = len(pairs)
    started = time.monotonic()

    for index, (src, dest, label) in enumerate(pairs, 1):
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # shutil.copy2(src, dst) при существующей ПАПКЕ dst молча копирует файл
            # ВНУТРЬ неё: оригинал оказался бы не на своём месте, а восстановление
            # отчиталось бы об успехе. Проверено эмпирически (см. tests/test_restore.py).
            if dest.is_dir():
                raise IsADirectoryError(f"на месте файла лежит папка: {dest.name}")
            shutil.copy2(src, dest)
            result.restored += 1
        except (OSError, shutil.Error) as e:
            result.failed.append((label, f"{type(e).__name__}: {e}"))
        if progress_callback:
            eta = (time.monotonic() - started) / index * (result.total - index)
            progress_callback(index, result.total, eta, label)

    if not result.failed and not result.cancelled:
        result.markers_removed = _remove_markers(game_path, result.failed)

    if result.ok:
        log(f"[Restore] ✓ {result.summary()}")
    else:
        log(f"[Restore] ✗ {result.summary()}")
        for what, why in result.failed[:10]:
            log(f"  ✗ {what}: {why}")
        if len(result.failed) > 10:
            log(f"  … и ещё {len(result.failed) - 10}")
    return result
