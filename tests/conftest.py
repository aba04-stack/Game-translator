"""
conftest.py — pytest-wide setup, loaded BEFORE any test module is collected.

КРИТИЧНО: устанавливает QT_QPA_PLATFORM=offscreen до того, как PyQt6 будет
импортирован где-либо (напрямую в тестах или транзитивно через rpgmtl_desktop).

Без этого: любой тест, создающий QApplication() в окружении без дисплея
(headless CI, контейнеры, серверы) вызывает фатальный SIGABRT ("Aborted"),
который убивает ВЕСЬ процесс pytest, а не просто падает один тест — все
последующие тесты не запускаются и не репортятся как failed, они просто
никогда не выполняются. Это обнаружилось на практике: добавление реальных
функциональных тестов с QThread/QApplication сломало `pytest tests/` без
дополнительных флагов.

"offscreen" — официальный Qt QPA-плагин для headless-тестирования: полностью
инициализирует событийный цикл, QThread, сигналы/слоты так же, как с реальным
дисплеем, просто не рендерит пиксели на экран. Это не ослабляет тесты.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Тестам не нужна durability (fsync) — он только замедляет прогон.
os.environ.setdefault("RPGMTL_NO_FSYNC", "1")

# BUG FIX: config.py/global_tm.py/error_utils.py persist real, meaningful
# state (settings incl. api_key, cross-project translation memory, error
# logs) to Path.home() unconditionally. Empirically confirmed: running this
# suite left a real ~/.rpgmtl_config.json on the machine running the tests,
# containing a test fixture's fake api_key and dead /tmp paths from an
# already-finished test — because some tested code paths (e.g. starting a
# translate job) legitimately auto-save "last session" settings as a real
# feature, and most individual tests never think to isolate CONFIG_PATH
# (some explicitly do via monkeypatch — this doesn't change or conflict with
# those). On a machine where RPGMTL is actually configured for real use,
# this would silently overwrite the developer's real settings/TM database.
# RPGMTL_HOME redirects all three to a session-scoped temp dir instead, set
# here — before any test module or the app modules themselves are imported
# — exactly like QT_QPA_PLATFORM above. Unset outside tests, so real
# deployed behavior (Path.home()) is unchanged.
import sys
import tempfile
from pathlib import Path

# Project root must be importable from conftest itself (the autouse fixture
# below imports config / error_utils / global_tm).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ALWAYS a fresh temp dir — never an inherited RPGMTL_HOME. The earlier
# os.environ.setdefault(...) honored a pre-set value, so a developer who had
# RPGMTL_HOME pointing at real data (or a CI cache that persisted it between
# runs) got their real config/TM overwritten, and leftover state from a
# previous run made tests fail on the 2nd run (found empirically:
# test_export_diagnostics_redacts_api_key_leaked_through_log_text passed on
# the 1st run and failed on every later run sharing the same home).
# Escape hatch for deliberate debugging: RPGMTL_TEST_HOME=<dir>.
os.environ["RPGMTL_HOME"] = (
    os.environ.get("RPGMTL_TEST_HOME") or tempfile.mkdtemp(prefix="rpgmtl_test_home_")
)


def write_extract_artifacts(output_dir, game_dir, game_type: str, sources: dict) -> None:
    """Write extract_index.json + project_manifest.json exactly as a real
    Pipeline.extract() would leave them behind.

    Why this exists: multiple regression tests in this suite build their
    fixtures by hand (writing extracted/*.json + translation_state.json
    directly, skipping a real extract() call, for precise control over the
    exact strings under test). Without this helper it's easy to forget that
    Pipeline.translate()/inject()/CLI translate all call verify_manifest()
    first — with no project_manifest.json, they correctly refuse with
    "Рабочая папка не привязана к игре" *before* ever reaching the logic the
    test is actually trying to exercise. That's not a bug in verify_manifest
    (Project Lock is working as designed) — it's an incomplete fixture, and
    it silently makes assertions like `assert ok is False` pass for the
    WRONG reason. Three separate tests hit exactly this in one session
    (2026-08-09) before the pattern got a name. Call this right after
    writing your extracted/*.json files:

        write_extract_artifacts(output_dir, game_dir, "rpgmv",
                                 {"Actors.json": "www/data/Actors.json"})

    `sources` maps extracted filename -> path relative to game_dir (only
    used for the manifest's own bookkeeping, doesn't need to be exhaustive
    for the fixture to satisfy verify_manifest).
    """
    import json
    from pathlib import Path
    from engine.project_lock import write_manifest

    output_dir = Path(output_dir)
    game_dir = Path(game_dir)
    (output_dir / "extract_index.json").write_text(
        json.dumps({"game_type": game_type, "sources": sources}, ensure_ascii=False),
        encoding="utf-8",
    )
    write_manifest(output_dir, game_dir, game_type)


import pytest  # noqa: E402  (kept next to the fixture that needs it)


@pytest.fixture(autouse=True)
def _isolated_rpgmtl_home(tmp_path_factory, monkeypatch):
    """Give EVERY test its own empty RPGMTL home (settings, logs, Global TM).

    The session-wide RPGMTL_HOME above only protects the real user data; tests
    still shared one directory with each other. Several of them legitimately
    persist state as a real feature (auto-saved "last session" settings incl.
    output_dir, Global TM entries, the error log), so whether a later test saw
    a clean slate depended on pytest's execution order. Under
    `pytest --random-order` that surfaced as real failures:
        - test_export_diagnostics_redacts_api_key_leaked_through_log_text
          (diagnostics were written to an output dir saved by an earlier test)
        - test_tm_manage_dialog_shows_correct_total
          (Global TM already contained entries written by an earlier test)
    config.CONFIG_PATH, error_utils.LOG_DIR/LOG_FILE and GlobalTM.DB_PATH are
    all read at call time, so patching them per test is enough. Tests that
    monkeypatch the same names themselves keep working: their patch is applied
    after this one and undone before it.
    """
    home = tmp_path_factory.mktemp("rpgmtl_home")
    monkeypatch.setenv("RPGMTL_HOME", str(home))

    import config
    import error_utils
    import global_tm

    monkeypatch.setattr(config, "CONFIG_PATH", home / ".rpgmtl_config.json")
    monkeypatch.setattr(error_utils, "LOG_DIR", home / ".rpgmtl_logs")
    monkeypatch.setattr(error_utils, "LOG_FILE", home / ".rpgmtl_logs" / "rpgmtl.log")
    monkeypatch.setattr(global_tm.GlobalTM, "DB_PATH", home / ".rpgmtl" / "global_tm.db")
    yield home


_QT_PATCHED_CLASSES = ("QMessageBox", "QFileDialog", "QInputDialog")


@pytest.fixture(autouse=True)
def _restore_qt_class_attributes():
    """Undo permanent monkey-patches that tests apply to PyQt6 dialog classes.

    Many GUI tests do `QMessageBox.question = staticmethod(...)`,
    `QFileDialog.getSaveFileName = ...`, `QMessageBox.exec = lambda self: 0`
    directly on the class. Those are process-global and were never undone, so
    whether a later test saw the real Qt behaviour depended on execution order.

    One of them is actively destructive: _tm_dialog_click() saved
    `original_add = QMessageBox.addButton` (class access returns an UNBOUND
    builtin wrapper) and "restored" that value afterwards, which permanently
    breaks `msg.addButton(...)` on instances ("first argument of unbound
    method must have type 'QMessageBox'") and made
    test_tm_manage_dialog_shows_correct_total fail whenever it ran after it.
    Verified empirically: restoring the raw descriptor from the class __dict__
    works, delattr does not (the attribute disappears).

    So: snapshot each class's own __dict__ before the test, and after it put
    back every attribute whose raw object changed (and drop attributes the
    test added).
    """
    try:
        from PyQt6 import QtWidgets
    except Exception:  # PyQt6 not installed: nothing to protect
        yield
        return

    snapshots = []
    for name in _QT_PATCHED_CLASSES:
        cls = getattr(QtWidgets, name, None)
        if cls is not None:
            snapshots.append((cls, dict(cls.__dict__)))

    yield

    for cls, before in snapshots:
        for attr in [a for a in list(cls.__dict__) if a not in before]:
            if not (attr.startswith("__") and attr.endswith("__")):
                try:
                    delattr(cls, attr)
                except (AttributeError, TypeError):
                    pass
        for attr, raw in before.items():
            if attr.startswith("__") and attr.endswith("__"):
                continue
            if cls.__dict__.get(attr) is not raw:
                try:
                    setattr(cls, attr, raw)
                except (AttributeError, TypeError):
                    pass


def make_mini_project(tmp_path, entries, game_type="rpgmv"):
    """Мини-проект RPG Maker MV на диске. entries = [(original, translated_or_"")].

    Возвращает (game_dir, output_dir, extract_dir) — с manifest'ом/индексом, как
    после настоящего Extract, так что Pipeline.translate/polish его принимают.
    """
    import json

    game_dir = tmp_path / "game"
    output_dir = tmp_path / "out"
    (game_dir / "data").mkdir(parents=True)
    (game_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "T"}), encoding="utf-8")
    (game_dir / "data" / "Actors.json").write_text("{}", encoding="utf-8")
    ext_dir = output_dir / "extracted"
    ext_dir.mkdir(parents=True)
    src = [{"file": "Actors.json", "path": f"[{i}].name", "original": o, "translated": "",
            "context": "dialog_text"} for i, (o, _) in enumerate(entries)]
    done = [dict(e, translated=t) for e, (_, t) in zip(src, entries)]
    (ext_dir / "Actors.json").write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    if any(t for _, t in entries):
        (ext_dir / "Actors_translated.json").write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    write_extract_artifacts(output_dir, game_dir, game_type, {"Actors.json": "data/Actors.json"})
    return game_dir, output_dir, ext_dir
