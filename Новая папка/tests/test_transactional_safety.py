import json
from pathlib import Path

from engine.injector import _inject_dialog


def test_rpg_dialog_refuses_stale_event_block():
    data = {"events": [{"pages": [{"list": [
        {"code": 401, "parameters": ["CHANGED"]},
        {"code": 401, "parameters": ["WORLD"]},
    ]}]}]}
    path = "events[0].pages[0].list[0..1].dialog"
    assert _inject_dialog(data, path, "Привет\nМир", "HELLO\nWORLD") is False
    assert data["events"][0]["pages"][0]["list"][0]["parameters"][0] == "CHANGED"


def test_rpg_dialog_accepts_exact_source_block():
    data = {"events": [{"pages": [{"list": [
        {"code": 401, "parameters": ["HELLO"]},
        {"code": 401, "parameters": ["WORLD"]},
    ]}]}]}
    path = "events[0].pages[0].list[0..1].dialog"
    assert _inject_dialog(data, path, "Привет\nМир", "HELLO\nWORLD") is True
    assert [x["parameters"][0] for x in data["events"][0]["pages"][0]["list"]] == ["Привет", "Мир"]


def test_pipeline_rolls_back_unexpected_animation_mutation(tmp_path, monkeypatch):
    from pipeline import Pipeline

    game = tmp_path / "game"
    out = tmp_path / "out"
    data = game / "data"
    data.mkdir(parents=True)
    (data / "System.json").write_text(json.dumps({"gameTitle": "Test"}), encoding="utf-8")
    (data / "Actors.json").write_text(json.dumps([{"name": "Hero"}]), encoding="utf-8")
    anim = game / "hero.anim"
    anim.write_bytes(b"ORIGINAL-ANIMATION")
    out.mkdir()

    p = Pipeline(game, out)
    ok, _ = p.extract()
    assert ok
    ext = out / "extracted"
    for src in ext.glob("*.json"):
        if src.name.endswith("_translated.json"):
            continue
        rows = json.loads(src.read_text(encoding="utf-8"))
        for row in rows:
            if row.get("original"):
                row["translated"] = row["original"] + " translated"
        (ext / f"{src.stem}_translated.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    import engine.injector as rpg_injector
    original = rpg_injector.inject_translations

    def malicious(*args, **kwargs):
        anim.write_bytes(b"CORRUPTED")
        return {"files_modified": 0, "total_injected": 1, "total_errors": 0, "total_skipped": 0}

    monkeypatch.setattr(rpg_injector, "inject_translations", malicious)
    ok2, msg = p.inject(apply_warp=False)
    assert not ok2
    assert "Safety Guard" in msg
    assert anim.read_bytes() == b"ORIGINAL-ANIMATION"
    assert original is not None


def test_rollback_reports_partial_failure_honestly(tmp_path):
    """
    BUG (P1-2, внешний аудит, подтверждено чтением кода): _rollback_inject()
    ничего не возвращала (-> None); все 4 места вызова безусловно логировали
    "[Rollback] ✓ Игра восстановлена ..." сразу после вызова, даже если
    ВОССТАНОВЛЕНИЕ КОНКРЕТНОГО ФАЙЛА не удалось (это логировалось отдельным
    ✗-сообщением чуть выше, но общий итог всё равно объявлялся успешным).
    Пользователь мог поверить, что игра полностью цела, хотя часть файлов
    осталась в изменённом состоянии.
    """
    import json as json_mod
    from pipeline import Pipeline

    game = tmp_path / "game"
    game.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    rollback_dir = tmp_path / "rb"
    rollback_dir.mkdir()

    # Два файла в снапшоте: один реально восстановится, второй — "испорчен"
    # (не существует в rollback_dir, но мы подменим copy2, чтобы для него
    # восстановление гарантированно упало с ошибкой).
    (rollback_dir / "good.json").write_text('{"ok": true}', encoding="utf-8")
    (rollback_dir / "bad.json").write_text('{"ok": true}', encoding="utf-8")
    (rollback_dir / "manifest.json").write_text(json_mod.dumps([
        {"relative": "good.json"}, {"relative": "bad.json"},
    ]), encoding="utf-8")

    p = Pipeline(game, out)
    logs = []
    p.log_cb = logs.append

    import pipeline as pipeline_mod
    real_copy2 = pipeline_mod.shutil.copy2

    def flaky_copy2(src, dst, *a, **kw):
        if Path(src).name == "bad.json":
            raise OSError("simulated disk failure restoring bad.json")
        return real_copy2(src, dst, *a, **kw)

    pipeline_mod.shutil.copy2 = flaky_copy2
    try:
        ok, failed = p._rollback_inject(rollback_dir)
    finally:
        pipeline_mod.shutil.copy2 = real_copy2

    assert ok is False
    assert failed == ["bad.json"]
    assert (game / "good.json").exists(), "успешно восстановленный файл должен быть на месте"
    assert not (game / "bad.json").exists(), "файл, который не удалось скопировать, не появляется"

    p._log_rollback_result(ok, failed, "после теста")
    full_log = "\n".join(logs)
    assert "ЧАСТИЧНЫЙ откат" in full_log, (
        f"БАГ: частичный сбой отката не отражён честно в логе:\n{full_log}"
    )
    assert "bad.json" in full_log
    assert "✓ Игра восстановлена" not in full_log, (
        f"БАГ: лог утверждает полное восстановление при частичном провале:\n{full_log}"
    )

    # Контроль: полностью успешный rollback по-прежнему честно говорит "✓".
    logs.clear()
    ok2, failed2 = p._rollback_inject(rollback_dir)
    assert ok2 is True and failed2 == []
    p._log_rollback_result(ok2, failed2, "после теста")
    assert "✓ Игра восстановлена после теста" in "\n".join(logs)
