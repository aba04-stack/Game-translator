"""
Восстановление оригинала (engine/restore.py + кнопка в GUI).

Главное, что здесь проверяется: ошибки копирования НЕ проглатываются.
Раньше `except (OSError, shutil.Error): pass` + `count += 1` приводили к
сообщению «✓ Восстановлено N файлов» даже тогда, когда часть файлов
осталась переведённой.
"""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import engine.restore as restore_mod
from engine.restore import MARKER_NAME, restore_game_backups


def _mv_game(tmp_path, files: dict[str, tuple[str, str]], data="data"):
    """MV/MZ: files = {имя: (оригинал, текущее_инжектированное)}."""
    game = tmp_path / "game"
    (game / data).mkdir(parents=True)
    (game / "_backup_original").mkdir()
    for name, (orig, injected) in files.items():
        (game / "_backup_original" / name).write_text(orig, encoding="utf-8")
        (game / data / name).write_text(injected, encoding="utf-8")
    return game


# ───────────────────────── базовое поведение ─────────────────────────

def test_restores_mv_files_into_data_dir_and_clears_marker(tmp_path):
    game = _mv_game(tmp_path, {"A.json": ("orig A", "ru A"), "B.json": ("orig B", "ru B")})
    (game / "data" / MARKER_NAME).write_text("x")

    logs: list[str] = []
    r = restore_game_backups(game, log_cb=logs.append)

    assert r.ok and r.restored == 2 and r.total == 2 and r.failed == []
    assert (game / "data" / "A.json").read_text(encoding="utf-8") == "orig A"
    assert (game / "data" / "B.json").read_text(encoding="utf-8") == "orig B"
    assert not (game / "data" / MARKER_NAME).exists() and r.markers_removed == 1
    assert any("✓" in m and "2" in m for m in logs)


@pytest.mark.parametrize("data_dir", ["data", "www/data", "Data"])
def test_data_dir_candidates(tmp_path, data_dir):
    game = _mv_game(tmp_path, {"A.json": ("orig", "ru")}, data=data_dir)
    assert restore_game_backups(game).ok
    assert (game / data_dir / "A.json").read_text(encoding="utf-8") == "orig"


def test_marker_files_inside_a_backup_are_never_copied_into_the_game(tmp_path):
    game = _mv_game(tmp_path, {"A.json": ("orig", "ru")})
    (game / "_backup_original" / MARKER_NAME).write_text("stale marker")
    (game / "_backup_original" / ".rpgmtl_other").write_text("x")
    r = restore_game_backups(game)
    assert r.ok and r.restored == 1
    assert not (game / "data" / MARKER_NAME).exists()          # иначе следующий Inject был бы заблокирован
    assert not (game / "data" / ".rpgmtl_other").exists()


def test_mirrored_engine_backups_keep_their_relative_layout(tmp_path):
    game = tmp_path / "game"
    for engine, rel in [("wolf", "Data/BasicData/Game.dat"), ("renpy", "game/script.rpy"),
                        ("godot", "scenes/a.tscn")]:
        b = game / f"_backup_{engine}_original" / Path(rel)
        b.parent.mkdir(parents=True)
        b.write_text("orig " + engine)
        live = game / rel
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("injected")
    r = restore_game_backups(game)
    assert r.ok and r.restored == 3
    for rel, eng in [("Data/BasicData/Game.dat", "wolf"), ("game/script.rpy", "renpy"), ("scenes/a.tscn", "godot")]:
        assert (game / rel).read_text() == "orig " + eng


def test_no_backup_is_reported_not_treated_as_success(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    logs: list[str] = []
    r = restore_game_backups(game, log_cb=logs.append)
    assert not r.ok and r.backups == [] and "не найдена" in r.summary()
    assert any("✗" in m for m in logs)


def test_mv_backup_without_data_dir_is_a_reported_failure_but_other_engines_still_restore(tmp_path):
    game = tmp_path / "game"
    (game / "_backup_original").mkdir(parents=True)
    (game / "_backup_original" / "A.json").write_text("orig")             # data/ нет — некуда
    (game / "_backup_wolf_original" / "d").mkdir(parents=True)
    (game / "_backup_wolf_original" / "d" / "x.dat").write_text("orig wolf")
    (game / "d").mkdir()
    (game / "d" / "x.dat").write_text("injected")

    r = restore_game_backups(game)

    assert not r.ok and r.restored == 1
    assert any("_backup_original" in what and "некуда" in why for what, why in r.failed)
    assert (game / "d" / "x.dat").read_text() == "orig wolf"


# ───────────────────────── ошибки не проглатываются ─────────────────────────

def test_copy_failure_is_reported_counted_honestly_and_keeps_markers(tmp_path):
    """РЕАЛЬНЫЙ сбой без моков: на месте файла лежит папка — copy2 бросает IsADirectoryError."""
    game = _mv_game(tmp_path, {"A.json": ("orig A", "ru A"), "B.json": ("orig B", "ru B")})
    (game / "data" / "B.json").unlink()
    (game / "data" / "B.json").mkdir()                                       # B.json — теперь папка
    (game / "data" / MARKER_NAME).write_text("x")

    logs: list[str] = []
    r = restore_game_backups(game, log_cb=logs.append)

    assert not r.ok
    assert r.restored == 1                                                   # считаем только реально скопированное
    assert [what for what, _ in r.failed] == ["_backup_original/B.json"]
    assert (game / "data" / "A.json").read_text(encoding="utf-8") == "orig A"  # остальное восстановлено
    assert (game / "data" / MARKER_NAME).exists()                            # маркер НЕ снят: игра в частичном состоянии
    assert "НЕ удалось: 1" in r.summary() and "частично" in r.summary()
    assert any("✗" in m and "B.json" in m for m in logs)


def test_failure_does_not_stop_the_remaining_files(tmp_path, monkeypatch):
    game = _mv_game(tmp_path, {f"{c}.json": (f"orig {c}", f"ru {c}") for c in "ABCD"})
    real = restore_mod.shutil.copy2

    def flaky(src, dst, *a, **k):
        if Path(src).name == "B.json":
            raise PermissionError("файл занят")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(restore_mod.shutil, "copy2", flaky)
    r = restore_game_backups(game)

    assert r.restored == 3 and len(r.failed) == 1 and "PermissionError" in r.failed[0][1]
    for c in "ACD":
        assert (game / "data" / f"{c}.json").read_text(encoding="utf-8") == f"orig {c}"
    assert (game / "data" / "B.json").read_text(encoding="utf-8") == "ru B"


def test_marker_that_cannot_be_removed_is_reported(tmp_path):
    game = _mv_game(tmp_path, {"A.json": ("orig", "ru")})
    marker = game / "data" / MARKER_NAME
    marker.mkdir()                                                           # unlink() на папке → OSError
    r = restore_game_backups(game)
    assert r.restored == 1 and not r.ok
    assert any("маркер" in what for what, _ in r.failed)


# ───────────────────────── прогресс и отмена ─────────────────────────

def test_progress_follows_the_contract(tmp_path):
    game = _mv_game(tmp_path, {f"{i}.json": (f"o{i}", f"r{i}") for i in range(5)})
    calls = []

    def progress(current, total, eta, filename):                             # ровно 4 позиционных аргумента
        calls.append((current, total, eta, filename))

    restore_game_backups(game, progress_callback=progress)

    assert [c[0] for c in calls] == [1, 2, 3, 4, 5]                          # монотонно, без пропусков
    assert all(c[1] == 5 and isinstance(c[2], float) and isinstance(c[3], str) and c[3] for c in calls)
    assert calls[-1][0] == calls[-1][1] and calls[-1][2] == 0.0              # финал: current == total, ETA 0
    assert all(c[2] >= 0 for c in calls)


def test_cancel_stops_cleanly_keeps_markers_and_is_safe_to_resume(tmp_path):
    game = _mv_game(tmp_path, {f"{i}.json": (f"o{i}", f"r{i}") for i in range(4)})
    (game / "data" / MARKER_NAME).write_text("x")
    cancel = threading.Event()

    def stop_after_first(current, total, eta, filename):
        if current == 1:
            cancel.set()

    r = restore_game_backups(game, progress_callback=stop_after_first, cancel_event=cancel)

    assert r.cancelled and not r.ok and r.restored == 1 and r.total == 4
    assert "прервано" in r.summary() and "безопасна для повтора" in r.summary()
    assert (game / "data" / MARKER_NAME).exists()

    r2 = restore_game_backups(game)                                          # повтор достраивает остальное
    assert r2.ok and r2.restored == 4
    assert [(game / "data" / f"{i}.json").read_text(encoding="utf-8") for i in range(4)] == [f"o{i}" for i in range(4)]
    assert not (game / "data" / MARKER_NAME).exists()


# ───────────────────────── GUI: кнопка «Восстановить оригинал» ─────────────────────────

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])
import rpgmtl_desktop as rd  # noqa: E402


def _drive_gui_until_idle(win, timeout_s=15.0):
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _app.processEvents()
        worker = getattr(win, "_worker", None)
        if worker is not None and worker.isFinished() and not win._is_busy:
            break
        time.sleep(0.01)
    for _ in range(5):
        _app.processEvents()


@pytest.fixture
def gui(monkeypatch):
    shown = {"info": [], "warn": []}
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown["info"].append(a[1:3])))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown["warn"].append(a[1:3])))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    win = rd.MainWindow()
    win._stats_timer.stop()
    yield win, shown
    win._stats_timer.stop()
    win.close()


def test_gui_restore_runs_in_background_and_reports_success(gui, monkeypatch, tmp_path):
    win, shown = gui
    game = _mv_game(tmp_path, {"A.json": ("orig A", "ru A")})
    monkeypatch.setattr(win, "_validate_paths", lambda: (True, str(game), str(tmp_path / "out")))

    win._restore_backup()
    assert win._worker is not None                                           # запущено потоком, а не синхронно в UI
    _drive_gui_until_idle(win)

    assert (game / "data" / "A.json").read_text(encoding="utf-8") == "orig A"
    assert any("Готово" in str(i) for i in shown["info"]) and shown["warn"] == []


def test_gui_restore_failure_is_shown_loudly_not_as_success(gui, monkeypatch, tmp_path):
    win, shown = gui
    game = _mv_game(tmp_path, {"A.json": ("orig A", "ru A")})
    (game / "data" / "A.json").unlink()
    (game / "data" / "A.json").mkdir()                                       # сбой копирования
    monkeypatch.setattr(win, "_validate_paths", lambda: (True, str(game), str(tmp_path / "out")))

    win._restore_backup()
    _drive_gui_until_idle(win)

    assert shown["info"] == []                                               # «✓ Готово» НЕ показывается
    assert any("Восстановление не завершено" in str(w) and "НЕ удалось: 1" in str(w) for w in shown["warn"])


def test_gui_restore_without_backup_warns_and_starts_nothing(gui, monkeypatch, tmp_path):
    win, shown = gui
    (tmp_path / "game").mkdir()
    monkeypatch.setattr(win, "_validate_paths", lambda: (True, str(tmp_path / "game"), str(tmp_path / "out")))
    before = getattr(win, "_worker", None)

    win._restore_backup()

    assert any("Нет резервной копии" in str(w) for w in shown["warn"])
    assert getattr(win, "_worker", None) is before


# ───────────────────────── полный цикл: inject → restore ─────────────────────────

def _snapshot(root: Path, skip_prefix="_backup") -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.relative_to(root).as_posix().startswith(skip_prefix)}


def _real_mv_game(tmp_path):
    import json
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "js").mkdir()
    (game / "js" / "rpg_core.js").write_text("x")
    (game / "data" / "System.json").write_text(json.dumps({"gameTitle": "テスト"}), encoding="utf-8")
    (game / "data" / "Actors.json").write_text(json.dumps([None] + [
        {"id": i, "name": f"勇者{i}", "nickname": "", "profile": f"プロフィール{i}"} for i in range(1, 4)]),
        encoding="utf-8")
    (tmp_path / "out").mkdir()
    return game, tmp_path / "out"


def test_inject_then_restore_returns_every_game_file_byte_for_byte_without_stray_files(tmp_path, monkeypatch):
    """Настоящий Extract → Translate → Inject → Restore. До исправления restore копировал обе
    раскладки бэкапа (вложенную и плоскую) относительно data/ и оставлял в игре мусорную
    папку data/data/."""
    import ai_translator
    from pipeline import Pipeline

    class Fake:
        def translate_batch(self, texts, log_cb=None):
            return [f"RU{t}" for t in texts]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Fake())
    game, out = _real_mv_game(tmp_path)
    original = _snapshot(game)

    p = Pipeline(str(game), str(out), log_callback=lambda m: None)
    assert p.extract()[0]
    assert p.translate(provider="openai", api_key="k", request_delay=0, use_global_tm=False)[0]
    assert p.inject()[0]
    injected = _snapshot(game)
    assert injected["data/Actors.json"] != original["data/Actors.json"]          # перевод реально вставлен
    assert (game / "_backup_original" / "Actors.json").exists()                  # плоская раскладка
    assert (game / "_backup_original" / "data" / "Actors.json").exists()         # вложенная раскладка

    result = restore_game_backups(game)

    assert result.ok, result.failed
    assert result.restored == 2                                                  # уникальные файлы, а не 4 копии
    assert _snapshot(game) == original                                           # ровно то, что было: ни мусора, ни маркера
    assert not (game / "data" / "data").exists()


def test_flat_and_nested_layouts_restore_each_destination_once(tmp_path):
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "A.json").write_text("injected")
    b = game / "_backup_original"
    (b / "data").mkdir(parents=True)
    (b / "data" / "A.json").write_text("original (nested)")
    (b / "A.json").write_text("original (flat)")

    r = restore_game_backups(game)

    assert r.ok and r.restored == 1 and r.total == 1
    assert (game / "data" / "A.json").read_text() == "original (nested)"        # основная (вложенная) раскладка главнее
    assert not (game / "data" / "data").exists()


def test_www_data_nested_layout_restores_relative_to_game_root(tmp_path):
    game = tmp_path / "game"
    (game / "www" / "data").mkdir(parents=True)
    (game / "www" / "data" / "A.json").write_text("injected")
    b = game / "_backup_original"
    (b / "www" / "data").mkdir(parents=True)
    (b / "www" / "data" / "A.json").write_text("original")
    (b / "A.json").write_text("original")

    r = restore_game_backups(game)

    assert r.ok and (game / "www" / "data" / "A.json").read_text() == "original"
    assert not (game / "www" / "www").exists() and not (game / "www" / "data" / "www").exists()
