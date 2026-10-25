"""
P0.5 — единый контракт прогресса: progress(current, total, eta, filename)
для КАЖДОГО этапа — Extract, Translate, Polish, QA, Inject, Restore.

Прогон идёт на настоящей мини-игре RPG Maker MV; колбэк с жёсткой сигнатурой из
четырёх позиционных аргументов — любой этап, вызвавший его иначе, упадёт с TypeError.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
from engine.restore import restore_game_backups
from pipeline import Pipeline


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, current, total, eta, filename):        # ровно 4 позиционных аргумента
        self.calls.append((current, total, eta, filename))


def assert_contract(rec: Recorder, stage: str):
    calls = rec.calls
    assert calls, f"{stage}: ни одного вызова прогресса"
    for cur, total, eta, fname in calls:
        assert isinstance(cur, int) and isinstance(total, int), (stage, cur, total)
        assert isinstance(eta, float), f"{stage}: eta должен быть float, а не {type(eta).__name__}"
        assert isinstance(fname, str), (stage, fname)
        assert total > 0 and 0 <= cur <= total, f"{stage}: current={cur} total={total}"
        assert eta == -1.0 or eta >= 0.0, f"{stage}: eta={eta} (допустимо -1.0 = «неизвестно» или >= 0)"
    seq = [c[0] for c in calls]
    assert seq == sorted(seq), f"{stage}: прогресс идёт назад: {seq}"
    assert calls[-1][0] == calls[-1][1], f"{stage}: последний вызов не «завершён»: {calls[-1]}"


@pytest.fixture
def project(tmp_path, monkeypatch):
    class Fake:
        def translate_batch(self, texts, log_cb=None):
            return [f"RU{t}" for t in texts]

        def polish_batch(self, pairs, log_cb=None):
            return [d + "!" for _, d in pairs]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Fake())
    game, out = tmp_path / "game", tmp_path / "out"
    (game / "data").mkdir(parents=True)
    (game / "js").mkdir()
    (game / "js" / "rpg_core.js").write_text("x")
    (game / "data" / "System.json").write_text(json.dumps({"gameTitle": "テスト"}), encoding="utf-8")
    (game / "data" / "Actors.json").write_text(json.dumps([None] + [
        {"id": i, "name": f"勇者{i}", "nickname": "", "profile": f"プロフィール{i}"} for i in range(1, 7)]),
        encoding="utf-8")
    out.mkdir()
    return game, out, Pipeline(str(game), str(out), log_callback=lambda m: None)


def test_every_stage_honours_the_progress_contract(project):
    game, out, p = project
    stages = {}

    rec = stages["extract"] = Recorder()
    assert p.extract(progress_callback=rec)[0]

    rec = stages["translate"] = Recorder()
    assert p.translate(provider="openai", api_key="k", batch_size=3, request_delay=0,
                       use_global_tm=False, progress_callback=rec)[0]

    rec = stages["polish"] = Recorder()
    assert p.polish(provider="openai", api_key="k", batch_size=3, request_delay=0, progress_callback=rec)[0]

    rec = stages["qa"] = Recorder()
    p.qa_report(progress_callback=rec)

    rec = stages["inject"] = Recorder()
    assert p.inject(progress_callback=rec)[0]

    rec = stages["restore"] = Recorder()
    assert restore_game_backups(game, progress_callback=rec).ok

    for name, r in stages.items():
        assert_contract(r, name)


def test_progress_callback_is_optional_for_every_stage(project):
    game, out, p = project
    assert p.extract()[0]
    assert p.translate(provider="openai", api_key="k", request_delay=0, use_global_tm=False)[0]
    assert p.polish(provider="openai", api_key="k", request_delay=0)[0]
    p.qa_report()
    assert p.inject()[0]
    assert restore_game_backups(game).ok


def test_translate_progress_stays_monotonic_across_parallel_batches(project):
    game, out, p = project
    assert p.extract()[0]
    rec = Recorder()
    assert p.translate(provider="openai", api_key="k", batch_size=2, max_workers=3, request_delay=0,
                       use_global_tm=False, progress_callback=rec)[0]
    assert_contract(rec, "translate (параллельно)")
