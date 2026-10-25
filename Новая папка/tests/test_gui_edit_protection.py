"""
P0.1 в GUI: вкладка «Строки» (StringsViewer) должна
  * отличать то, что НАБРАЛ человек, от результата ИИ-действий;
  * записывать это в translation_state.json при «Сохранить правки»;
  * подсвечивать защищённые строки и не давать ИИ затереть их молча.

Тесты работают с реальными виджетами (QT_QPA_PLATFORM=offscreen) и реальными
файлами; модальные диалоги подменяются, потому что в headless-режиме
их некому закрыть.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from engine.state import (
    AI_TRANSLATED, APPROVED, LOCKED, MANUAL_EDITED, STATE_FILE,
    TranslationState,
)

_app = QApplication.instance() or QApplication([])
import rpgmtl_desktop as rd  # noqa: E402


ROWS = [("勇者", "Hero ai"), ("魔王", "Demon ai"), ("村人", "Villager ai")]


@pytest.fixture
def dialogs(monkeypatch):
    """Подменяет модальные диалоги; записывает, что было показано."""
    shown = {"info": [], "warn": [], "question": []}
    answers = {"question": QMessageBox.StandardButton.No}
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown["info"].append(a[1:3])))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown["warn"].append(a[1:3])))
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: (shown["question"].append(a[1:3]),
                                                      answers["question"])[1]))
    shown["answers"] = answers
    return shown


@pytest.fixture
def viewer(tmp_path, dialogs):
    out = tmp_path / "out"
    ext = out / "extracted"
    ext.mkdir(parents=True)
    src = [{"file": "Actors.json", "path": f"[{i}].name", "original": o, "translated": "",
            "context": "dialog_text"} for i, (o, _) in enumerate(ROWS)]
    done = [dict(e, translated=t) for e, (_, t) in zip(src, ROWS)]
    (ext / "Actors.json").write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    (ext / "Actors_translated.json").write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    st = TranslationState(out / STATE_FILE)
    for o, t in ROWS:
        st.set(o, t)
    st.save()

    v = rd.StringsViewer()
    v.set_output_dir(str(out))
    v.load()
    v._test_out = out
    v._test_ext = ext
    return v


def _row_of(viewer, original: str) -> int:
    for r in range(viewer.table.rowCount()):
        if viewer.table.item(r, 2).text() == original:
            return r
    raise AssertionError(original)


def _state(viewer) -> TranslationState:
    return TranslationState(viewer._test_out / STATE_FILE)


def _file_text(viewer, original: str) -> str:
    data = json.loads((viewer._test_ext / "Actors_translated.json").read_text(encoding="utf-8"))
    return next(e["translated"] for e in data if e["original"] == original)


def _color(item) -> str:
    return item.foreground().color().name().lower()


# ───────────────────────── ввод руками ─────────────────────────

def test_typed_edit_is_saved_as_manual_and_styled(viewer):
    r = _row_of(viewer, "勇者")
    item = viewer.table.item(r, 3)
    assert _color(item) == "#a5d6a7"                       # обычный ИИ-перевод — зелёный

    item.setText("Мой Герой")                              # пользователь набрал текст (сигналы включены)

    assert viewer._status_of("勇者") == MANUAL_EDITED       # статус виден сразу
    assert _color(item) == "#90caf9" and "Ручная правка" in item.toolTip()
    assert _state(viewer).status_of("勇者") == AI_TRANSLATED   # на диске ещё не сохранено

    viewer.save()

    st = _state(viewer)
    assert _file_text(viewer, "勇者") == "Мой Герой"
    assert st.get("勇者") == "Мой Герой" and st.status_of("勇者") == MANUAL_EDITED
    assert st.status_of("魔王") == AI_TRANSLATED            # соседние строки не затронуты
    assert viewer._pending_state_edits == {}


def test_manual_status_is_restored_after_reload(viewer):
    viewer.table.item(_row_of(viewer, "魔王"), 3).setText("Мой Демон")
    viewer.save()

    viewer.load()                                          # как будто перезапустили вкладку

    item = viewer.table.item(_row_of(viewer, "魔王"), 3)
    assert viewer._status_of("魔王") == MANUAL_EDITED
    assert _color(item) == "#90caf9"


def test_clearing_a_cell_is_not_recorded_as_manual_translation(viewer):
    item = viewer.table.item(_row_of(viewer, "村人"), 3)
    item.setText("")
    assert viewer._pending_state_edits.get("Actors.json", {}) == {}
    assert viewer._status_of("村人") == AI_TRANSLATED
    assert _color(item) == "#ef9a9a"                       # пустой — красный


# ───────────────────────── ИИ-действия ─────────────────────────

def _fake_retranslate_result(viewer, rows_to_new_text: dict[str, str]):
    """Имитирует завершение фонового re-translate: тот же контракт, что _RetranslateThread."""
    targets, results = [], []
    for original, new_text in rows_to_new_text.items():
        r = _row_of(viewer, original)
        targets.append((r, "Actors.json", f"[{r}].name", original, viewer.table.item(r, 3)))
        results.append(SimpleNamespace(success=True, translated=new_text))
    viewer._retranslate_targets = targets
    viewer._on_retranslate_finished(SimpleNamespace(results=results), "")


def test_explicit_ai_retranslate_resets_manual_status(viewer):
    viewer.table.item(_row_of(viewer, "勇者"), 3).setText("Правка человека")
    viewer.save()
    assert _state(viewer).status_of("勇者") == MANUAL_EDITED

    _fake_retranslate_result(viewer, {"勇者": "Новый ИИ-перевод"})   # пользователь явно попросил
    viewer.save()

    st = _state(viewer)
    assert st.get("勇者") == "Новый ИИ-перевод" and st.status_of("勇者") == AI_TRANSLATED
    assert _file_text(viewer, "勇者") == "Новый ИИ-перевод"


def test_row_actions_never_send_locked_rows_to_ai(viewer, monkeypatch, dialogs):
    """Реальный путь защиты: заблокированная строка не доходит до ИИ ни в
    «Перевести», ни в «Улучшить» — поток даже не создаётся."""
    viewer.set_settings_provider(lambda: {"provider": "openai", "api_key": "sk-test"})
    r = _row_of(viewer, "魔王")
    viewer._mark_rows([r], "locked")

    def must_not_start(*a, **k):
        raise AssertionError("ИИ-поток не должен создаваться для заблокированной строки")
    monkeypatch.setattr(rd, "_RetranslateThread", must_not_start)
    monkeypatch.setattr(rd, "_PolishThread", must_not_start)

    viewer._retranslate_rows([r])
    viewer._polish_rows([r])

    assert sum("Пропущено заблокированных" in str(i) for i in dialogs["info"]) == 2
    assert getattr(viewer, "_retranslate_thread", None) is None
    assert getattr(viewer, "_polish_thread", None) is None
    st = _state(viewer)
    assert st.get("魔王") == "Demon ai" and st.status_of("魔王") == LOCKED
    assert viewer.table.item(r, 3).text() == "Demon ai"


def test_filter_skips_locked_and_asks_about_manual_rows(viewer, dialogs):
    viewer.table.item(_row_of(viewer, "勇者"), 3).setText("Правка")        # manual
    viewer._mark_rows([_row_of(viewer, "魔王")], "locked")                  # locked
    # 村人 — обычная ИИ-строка
    targets = [(_row_of(viewer, o), "Actors.json", "", o, None) for o in ("勇者", "魔王", "村人")]

    # Ответ «Нет» → защищённые пропускаются
    dialogs["answers"]["question"] = QMessageBox.StandardButton.No
    kept = viewer._filter_protected_targets(list(targets), "перевести")
    assert [t[3] for t in kept] == ["村人"]
    assert any("Пропущено заблокированных" in str(i) for i in dialogs["info"])

    # Ответ «Да» → ручная правка включается, замок — по-прежнему нет
    dialogs["answers"]["question"] = QMessageBox.StandardButton.Yes
    kept = viewer._filter_protected_targets(list(targets), "перевести")
    assert {t[3] for t in kept} == {"村人", "勇者"}          # замок («魔王») по-прежнему исключён

    # «Отмена» → None (ничего не делаем)
    dialogs["answers"]["question"] = QMessageBox.StandardButton.Cancel
    assert viewer._filter_protected_targets(list(targets), "перевести") is None


def test_filter_does_not_ask_when_nothing_is_protected(viewer, dialogs):
    targets = [(_row_of(viewer, o), "Actors.json", "", o, None) for o in ("勇者", "村人")]
    kept = viewer._filter_protected_targets(list(targets), "улучшить")
    assert len(kept) == 2 and dialogs["question"] == [] and dialogs["info"] == []


# ───────────────────────── массовая замена / CSV / статусы ─────────────────────────

def test_bulk_replace_changes_text_but_keeps_each_rows_status(viewer, monkeypatch, dialogs):
    viewer.table.item(_row_of(viewer, "勇者"), 3).setText("Hero manual")
    viewer.save()                                           # 勇者 → manual, "Hero manual"

    answers = iter([("Hero", True), ("Brave", True)])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    viewer._bulk_replace()
    viewer.save()

    st = _state(viewer)
    assert st.get("勇者") == "Brave manual" and st.status_of("勇者") == MANUAL_EDITED
    assert st.get("魔王") == "Demon ai" and st.status_of("魔王") == AI_TRANSLATED   # без «Hero» — не менялась


def test_csv_import_marks_rows_as_manual(viewer, monkeypatch, tmp_path, dialogs):
    csv_path = tmp_path / "tr.csv"
    csv_path.write_text(
        '"Файл";"Путь";"Оригинал";"Перевод"\n'
        '"Actors.json";"[2].name";"村人";"Деревенщина (переводчик)"\n',
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(csv_path), "CSV")))

    viewer._import_csv()
    viewer.save()

    st = _state(viewer)
    assert st.get("村人") == "Деревенщина (переводчик)"
    assert st.status_of("村人") == MANUAL_EDITED            # человеческий перевод защищён


def test_mark_rows_approve_lock_unprotect(viewer):
    r = _row_of(viewer, "村人")
    viewer._mark_rows([r], "approved")
    assert _state(viewer).status_of("村人") == APPROVED and viewer._status_of("村人") == APPROVED
    assert "Одобрено" in viewer.table.item(r, 3).toolTip()

    viewer._mark_rows([r], "locked")
    assert _state(viewer).status_of("村人") == LOCKED

    viewer._mark_rows([r], "ai_translated")
    assert _state(viewer).status_of("村人") == AI_TRANSLATED
    assert viewer.table.item(r, 3).toolTip() == ""


def test_status_set_while_text_is_unsaved_is_not_overwritten_by_manual_mark(viewer):
    r = _row_of(viewer, "勇者")
    viewer.table.item(r, 3).setText("Набрал и сразу одобрил")
    viewer._mark_rows([r], "approved")
    viewer.save()

    st = _state(viewer)
    assert st.get("勇者") == "Набрал и сразу одобрил"
    assert st.status_of("勇者") == APPROVED                  # выбор пользователя не затёрт «ручной правкой»


def test_mark_rows_on_untranslated_row_is_refused(viewer, dialogs):
    r = _row_of(viewer, "村人")
    viewer.table.blockSignals(True)
    viewer.table.item(r, 3).setText("")
    viewer.table.blockSignals(False)
    viewer._mark_rows([r], "locked")
    assert any("уже переведённым" in str(i) for i in dialogs["info"])
    assert _state(viewer).status_of("村人") == AI_TRANSLATED


# ───────────────────────── устойчивость ─────────────────────────

def test_failed_status_write_keeps_pending_edit_and_retry_succeeds(viewer, monkeypatch, dialogs):
    import engine.state as st_mod
    real_record_edits = st_mod.record_edits
    viewer.table.item(_row_of(viewer, "勇者"), 3).setText("Правка")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(st_mod, "record_edits", boom)
    viewer.save()

    assert _file_text(viewer, "勇者") == "Правка"                   # текст на диск дошёл
    assert any("не сохранены" in str(w) for w in dialogs["warn"])    # пользователь предупреждён
    assert viewer._pending_state_edits["Actors.json"]["勇者"] == ("manual", "Правка")   # не потеряно
    assert _state(viewer).status_of("勇者") == AI_TRANSLATED          # защиты на диске ещё нет

    monkeypatch.setattr(st_mod, "record_edits", real_record_edits)   # диск «починили»
    viewer.save()                                                    # повтор, как и просит предупреждение

    st = _state(viewer)
    assert st.status_of("勇者") == MANUAL_EDITED and st.get("勇者") == "Правка"
    assert viewer._pending_state_edits == {}
    assert "сохранены" in viewer.lbl_count.text()


def test_reload_discards_pending_notes_together_with_unsaved_cells(viewer):
    viewer.table.item(_row_of(viewer, "勇者"), 3).setText("Не сохранено")
    assert viewer._pending_state_edits
    viewer.load()
    assert viewer._pending_state_edits == {}
    assert viewer.table.item(_row_of(viewer, "勇者"), 3).text() == "Hero ai"
