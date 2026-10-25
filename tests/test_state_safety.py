"""
Тесты модели состояния перевода (engine/state.py):
  * статусы строк и защита ручных правок (P0.1);
  * потеря данных при повреждённом translation_state.json (data-loss);
  * обратная совместимость со старым форматом файла.

Все тесты проверяют РЕАЛЬНЫЕ файлы на диске, а не исходный код.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import state as st
from engine.state import (
    APPROVED, AI_TRANSLATED, EXTRACTED, LOCKED, MANUAL_EDITED, NEEDS_REVIEW,
    SOURCE_MANUAL, SOURCE_SYNC, SOURCE_SYSTEM, STATE_FILE,
    TranslationState, load_state, record_edits,
)


def _fresh(tmp_path) -> TranslationState:
    return TranslationState(tmp_path / STATE_FILE)


# ───────────────────────── статусы и защита ─────────────────────────

def test_default_status_and_legacy_semantics(tmp_path):
    s = _fresh(tmp_path)
    assert s.status_of("hello") == EXTRACTED          # нет перевода
    assert s.set("hello", "привет") is True            # прежний вызов set(orig, trans)
    assert s.status_of("hello") == AI_TRANSLATED
    assert s.get("hello") == "привет" and s.has("hello")
    assert s.is_protected("hello") is False
    assert s.set("", "x") is False and s.set("a", "") is False  # пустое по-прежнему игнорируется


def test_ai_cannot_overwrite_manual_edit(tmp_path):
    s = _fresh(tmp_path)
    s.set("line", "ai v1")
    assert s.set("line", "моя правка", source=SOURCE_MANUAL) is True
    assert s.status_of("line") == MANUAL_EDITED and s.is_protected("line")

    assert s.set("line", "ai v2") is False             # молча затирать нельзя
    assert s.get("line") == "моя правка"
    assert s.protected_skipped == 1


def test_force_overrides_manual_and_approved_but_never_locked(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "x"); s.mark("a", MANUAL_EDITED)
    s.set("b", "x"); s.mark("b", APPROVED)
    s.set("c", "x"); s.mark("c", LOCKED)

    assert s.set("a", "ai", force=True) is True and s.status_of("a") == AI_TRANSLATED
    assert s.set("b", "ai", force=True) is True and s.status_of("b") == AI_TRANSLATED
    assert s.set("c", "ai", force=True) is False       # LOCKED — только после снятия блокировки
    assert s.get("c") == "x" and s.status_of("c") == LOCKED

    assert s.mark("c", AI_TRANSLATED) is True          # снять блокировку
    assert s.set("c", "ai", force=True) is True


def test_manual_edit_keeps_lock_but_updates_text(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "x"); s.mark("a", LOCKED)
    assert s.set("a", "правка под замком", source=SOURCE_MANUAL) is True
    assert s.status_of("a") == LOCKED and s.get("a") == "правка под замком"


def test_system_maintenance_skips_protected_and_keeps_status(tmp_path):
    s = _fresh(tmp_path)
    s.set("p", "ручной", source=SOURCE_MANUAL)
    s.set("q", "ai")
    assert s.set("p", "авто-ремонт", source=SOURCE_SYSTEM) is False
    assert s.get("p") == "ручной"
    assert s.set("q", "ai (чуть исправлен)", source=SOURCE_SYSTEM) is True
    assert s.status_of("q") == AI_TRANSLATED


def test_sync_from_file_always_writes_and_keeps_status(tmp_path):
    """Файл — источник истины для правок вне pipeline (см. _sync_existing_translations)."""
    s = _fresh(tmp_path)
    s.set("p", "ручной", source=SOURCE_MANUAL)
    assert s.set("p", "текст из файла", source=SOURCE_SYNC) is True
    assert s.get("p") == "текст из файла"
    assert s.status_of("p") == MANUAL_EDITED


def test_fresh_ai_text_resets_needs_review(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "old"); s.mark("a", NEEDS_REVIEW)
    assert s.status_of("a") == NEEDS_REVIEW and not s.is_protected("a")
    assert s.set("a", "new ai") is True
    assert s.status_of("a") == AI_TRANSLATED


def test_mark_validation(tmp_path):
    s = _fresh(tmp_path)
    assert s.mark("missing", LOCKED) is False          # строки нет в кэше
    s.set("a", "x")
    assert s.mark("a", "no_such_status") is False
    assert s.mark("a", EXTRACTED) is False             # вычисляемый, вручную не задаётся
    with pytest.raises(ValueError):
        s.set("a", "y", source="bogus")


def test_status_counts_and_stats(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "1"); s.set("b", "2")
    s.set("c", "3", source=SOURCE_MANUAL)
    s.set("d", "4"); s.mark("d", LOCKED)
    counts = s.status_counts()
    assert counts[AI_TRANSLATED] == 2 and counts[MANUAL_EDITED] == 1 and counts[LOCKED] == 1
    assert s.protected_originals() == {"c", "d"}
    assert s.stats()["protected"] == 2


def test_clear_resets_statuses(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "1", source=SOURCE_MANUAL)
    s.clear()
    assert s.count == 0 and s.protected_originals() == set()
    assert "status" not in json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))


# ───────────────────────── формат файла ─────────────────────────

def test_statuses_persist_and_status_key_is_omitted_when_unneeded(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "1"); s.save()
    assert "status" not in json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))

    s.set("b", "2", source=SOURCE_MANUAL); s.mark("a", APPROVED); s.save()
    raw = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert raw["status"] == {"b": MANUAL_EDITED, "a": APPROVED}

    again = TranslationState(tmp_path / STATE_FILE)
    assert again.status_of("a") == APPROVED and again.status_of("b") == MANUAL_EDITED


def test_legacy_file_without_status_key_loads(tmp_path):
    legacy = {"meta": {"provider": "openai"}, "cache": {"こんにちは": "Здравствуйте"}}
    (tmp_path / STATE_FILE).write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    s = TranslationState(tmp_path / STATE_FILE)
    assert s.load_error is None
    assert s.get("こんにちは") == "Здравствуйте" and s.status_of("こんにちは") == AI_TRANSLATED


def test_unknown_or_orphan_statuses_are_dropped_on_load(tmp_path):
    data = {"cache": {"a": "1"}, "status": {"a": "locked", "ghost": "locked", "a2": "weird"}}
    (tmp_path / STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
    s = TranslationState(tmp_path / STATE_FILE)
    assert s.protected_originals() == {"a"}
    assert s.status_of("ghost") == EXTRACTED


# ───────────────────────── потеря данных при повреждении ─────────────────────────

def _populate(tmp_path, n=5) -> str:
    s = _fresh(tmp_path)
    for i in range(n):
        s.set(f"line {i}", f"строка {i}")
    s.set("hand", "моя ручная правка", source=SOURCE_MANUAL)
    s.save()
    return (tmp_path / STATE_FILE).read_text(encoding="utf-8")


def test_truncated_state_is_quarantined_not_silently_lost(tmp_path):
    """Регрессия: оборванный файл молча считался пустым, следующий save() его затирал."""
    good = _populate(tmp_path)
    broken = good[: len(good) // 2]
    (tmp_path / STATE_FILE).write_text(broken, encoding="utf-8")

    s = TranslationState(tmp_path / STATE_FILE)
    assert s.load_error
    assert s.corrupt_copy is not None and s.corrupt_copy.exists()
    assert s.corrupt_copy.read_text(encoding="utf-8") == broken      # байт-в-байт, не удалён

    # Дальнейшая работа не уничтожает отложенную копию:
    s.set("new", "новая"); s.save()
    assert s.corrupt_copy.read_text(encoding="utf-8") == broken
    fresh = TranslationState(tmp_path / STATE_FILE)
    assert fresh.get("new") == "новая" and fresh.load_error is None


def test_corrupt_main_is_recovered_from_backup(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "1"); s.save()
    s.set("b", "2", source=SOURCE_MANUAL); s.save()      # теперь .bak = версия с одним "a"
    assert (tmp_path / (STATE_FILE + st.BACKUP_SUFFIX)).exists()

    (tmp_path / STATE_FILE).write_text("{ not json", encoding="utf-8")
    r = TranslationState(tmp_path / STATE_FILE)
    assert r.recovered_from_backup is True
    assert r.get("a") == "1"                              # из резервной копии
    assert r.corrupt_copy is not None and r.corrupt_copy.exists()

    # Основной файл пересоздан СРАЗУ при восстановлении — следующий читатель
    # (GUI опрашивает состояние каждые 3 с) не должен увидеть «файла нет».
    assert (tmp_path / STATE_FILE).exists()
    again = TranslationState(tmp_path / STATE_FILE)
    assert again.load_error is None and again.get("a") == "1"
    assert again.recovered_from_backup is False           # это уже обычная загрузка


def test_backup_holds_previous_version(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "v1"); s.save()
    s.set("a", "v2"); s.save()
    bak = json.loads((tmp_path / (STATE_FILE + st.BACKUP_SUFFIX)).read_text(encoding="utf-8"))
    assert bak["cache"]["a"] == "v1"
    assert json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["cache"]["a"] == "v2"


@pytest.mark.parametrize("payload", [
    "[]",                                         # корень — не объект
    '{"cache": []}',                              # cache — не объект
    '{"cache": {"a": 5}}',                        # значение — не строка
    '{"cache": {}, "status": []}',                # status — не объект
    "",                                           # пустой файл
])
def test_structurally_invalid_state_is_treated_as_corrupt(tmp_path, payload):
    (tmp_path / STATE_FILE).write_text(payload, encoding="utf-8")
    s = TranslationState(tmp_path / STATE_FILE)
    assert s.load_error
    assert s.corrupt_copy is not None and s.corrupt_copy.read_text(encoding="utf-8") == payload
    assert s.count == 0


def test_load_state_reports_corruption_loudly(tmp_path):
    _populate(tmp_path)
    (tmp_path / STATE_FILE).write_text("{oops", encoding="utf-8")
    messages: list[str] = []
    s = load_state(tmp_path, messages.append)
    joined = "\n".join(messages)
    assert "повреждён" in joined and "НЕ восстановлен" in joined
    assert s.corrupt_copy is not None and s.corrupt_copy.name in joined


def test_load_state_reports_backup_recovery(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "1"); s.save()
    s.set("b", "2"); s.save()
    (tmp_path / STATE_FILE).write_text("{oops", encoding="utf-8")
    messages: list[str] = []
    r = load_state(tmp_path, messages.append)
    assert r.recovered_from_backup
    assert any("Восстановлено" in m and "резервной копии" in m for m in messages)


# ───────────────────────── record_edits ─────────────────────────

def test_record_edits_marks_manual_and_respects_lock(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "ai a"); s.set("b", "ai b"); s.set("c", "ai c"); s.mark("c", LOCKED)
    s.save()

    res = record_edits(
        tmp_path,
        manual={"a": "правка a"},
        ai={"b": "переперевод b", "c": "переперевод c"},
    )
    assert res == {"manual": 1, "ai": 1, "ai_blocked": 1, "sync": 0}

    r = TranslationState(tmp_path / STATE_FILE)
    assert r.get("a") == "правка a" and r.status_of("a") == MANUAL_EDITED
    assert r.get("b") == "переперевод b" and r.status_of("b") == AI_TRANSLATED
    assert r.get("c") == "ai c" and r.status_of("c") == LOCKED       # замок устоял


def test_record_edits_sync_changes_text_but_not_status(tmp_path):
    """Массовая замена: текст обновляется, статус строки остаётся прежним."""
    s = _fresh(tmp_path)
    s.set("ai", "Hero ai"); s.set("man", "Hero manual", source=SOURCE_MANUAL)
    s.save()
    res = record_edits(tmp_path, sync={"ai": "Brave ai", "man": "Brave manual"})
    assert res["sync"] == 2
    r = TranslationState(tmp_path / STATE_FILE)
    assert r.get("ai") == "Brave ai" and r.status_of("ai") == AI_TRANSLATED
    assert r.get("man") == "Brave manual" and r.status_of("man") == MANUAL_EDITED


def test_record_status_adds_missing_entry_then_protects_it(tmp_path):
    """Перевод загружен из файла/CSV и в кэше его нет — защита всё равно должна сработать."""
    from engine.state import record_status
    assert record_status(tmp_path, [("orig", "текст из файла")], LOCKED) == 1
    r = TranslationState(tmp_path / STATE_FILE)
    assert r.get("orig") == "текст из файла" and r.status_of("orig") == LOCKED
    assert record_status(tmp_path, [("orig", "x")], AI_TRANSLATED) == 1     # снять защиту
    assert TranslationState(tmp_path / STATE_FILE).status_of("orig") == AI_TRANSLATED
    assert record_status(tmp_path, [("orig", "x")], "bogus") == 0


def test_explicit_statuses_returns_a_copy(tmp_path):
    s = _fresh(tmp_path)
    s.set("a", "1", source=SOURCE_MANUAL)
    m = s.explicit_statuses()
    m["a"] = "tampered"
    assert s.status_of("a") == MANUAL_EDITED


def test_get_stats_poll_does_not_cause_data_loss_after_corruption(tmp_path):
    """Сценарий из GUI: первым повреждённый файл замечает get_stats() (опрос раз в 3 с),
    а перевод стартует позже. Восстановленное не должно пропасть между ними."""
    from pipeline import Pipeline
    s = _fresh(tmp_path)
    s.set("a", "1"); s.save()
    s.set("b", "2", source=SOURCE_MANUAL); s.save()       # .bak = версия только с "a"
    (tmp_path / STATE_FILE).write_text("{oops", encoding="utf-8")
    (tmp_path / "extracted").mkdir()

    game = tmp_path / "game"; game.mkdir()
    Pipeline(str(game), str(tmp_path), log_callback=lambda m: None).get_stats()   # «опрос» №1

    later = load_state(tmp_path, lambda m: None)           # «перевод стартовал» — читатель №2
    assert later.get("a") == "1"                           # восстановленное на месте


def test_stats_report_protected_strings(tmp_path):
    from pipeline import Pipeline
    s = _fresh(tmp_path)
    s.set("a", "1"); s.set("b", "2", source=SOURCE_MANUAL); s.mark("a", LOCKED); s.save()
    (tmp_path / "extracted").mkdir()
    game = tmp_path / "game"; game.mkdir()
    stats = Pipeline(str(game), str(tmp_path), log_callback=lambda m: None).get_stats()
    assert stats["protected_strings"] == 2
