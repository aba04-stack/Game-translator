"""
P0.1 — защита ручных правок на уровне Pipeline.

Правило (PROJECT_STATUS.md, раздел 3):
    MANUAL_EDITED / APPROVED / LOCKED  →  AI не перезаписывает автоматически.

Каждый тест строит реальный мини-проект на диске, прогоняет реальный код
Pipeline (с подменённым сетевым провайдером) и проверяет СОДЕРЖИМОЕ файлов и
состояния — а не исходный код.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
from conftest import write_extract_artifacts
from engine.state import (
    AI_TRANSLATED, APPROVED, LOCKED, MANUAL_EDITED, SOURCE_MANUAL, STATE_FILE,
    TranslationState,
)
from pipeline import Pipeline


class FakeProvider:
    """Сетевой провайдер без сети: перевод = префикс, полировка = замена маркера."""

    def __init__(self):
        self.translated: list[str] = []
        self.polished: list[str] = []

    def translate_batch(self, texts, log_cb=None):
        self.translated.extend(texts)
        return [f"RU:{t}" for t in texts]

    def polish_batch(self, pairs, log_cb=None):
        self.polished.extend(o for o, _ in pairs)
        return [d.replace("IMPROVE_ME", "improved") for _, d in pairs]


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: fake)
    return fake


def _project(tmp_path, entries: list[tuple[str, str]]):
    """entries: [(original, translated_or_empty)] → (game_dir, output_dir, ext_dir)."""
    game_dir = tmp_path / "game"
    output_dir = tmp_path / "out"
    (game_dir / "data").mkdir(parents=True)
    (game_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "T"}), encoding="utf-8")
    (game_dir / "data" / "Actors.json").write_text("{}", encoding="utf-8")
    ext_dir = output_dir / "extracted"
    ext_dir.mkdir(parents=True)
    src = [{"file": "Actors.json", "path": f"[{i}].name", "original": o, "translated": "",
            "context": "dialog_text"} for i, (o, _) in enumerate(entries)]
    done = [{"file": "Actors.json", "path": f"[{i}].name", "original": o, "translated": t,
             "context": "dialog_text"} for i, (o, t) in enumerate(entries)]
    (ext_dir / "Actors.json").write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    if any(t for _, t in entries):
        (ext_dir / "Actors_translated.json").write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    write_extract_artifacts(output_dir, game_dir, "rpgmv", {"Actors.json": "data/Actors.json"})
    return game_dir, output_dir, ext_dir


def _translated(ext_dir) -> dict[str, str]:
    data = json.loads((ext_dir / "Actors_translated.json").read_text(encoding="utf-8"))
    return {e["original"]: e["translated"] for e in data}


def _state(output_dir) -> TranslationState:
    return TranslationState(output_dir / STATE_FILE)


def _seed_state(output_dir, ai=(), manual=(), locked=(), approved=()):
    s = TranslationState(output_dir / STATE_FILE)
    for o, t in ai:
        s.set(o, t)
    for o, t in manual:
        s.set(o, t, source=SOURCE_MANUAL)
    for o, t in locked:
        s.set(o, t); s.mark(o, LOCKED)
    for o, t in approved:
        s.set(o, t); s.mark(o, APPROVED)
    s.save()


_POLISH = dict(provider="openai", api_key="sk-fake", target_lang="Russian", request_delay=0)


# ───────────────────────── polish ─────────────────────────

def test_polish_skips_manual_edit_and_polishes_ai_text(tmp_path, provider):
    game, out, ext = _project(tmp_path, [("勇者A", "ai IMPROVE_ME A"), ("勇者B", "моя IMPROVE_ME правка")])
    _seed_state(out, ai=[("勇者A", "ai IMPROVE_ME A")], manual=[("勇者B", "моя IMPROVE_ME правка")])

    logs: list[str] = []
    ok, _ = Pipeline(str(game), str(out), log_callback=logs.append).polish(**_POLISH)

    assert ok is True
    t = _translated(ext)
    assert t["勇者A"] == "ai improved A"                       # ИИ-текст отполирован
    assert t["勇者B"] == "моя IMPROVE_ME правка"               # правка человека нетронута
    assert provider.polished == ["勇者A"]                      # защищённая строка даже не ушла провайдеру
    assert any("Пропущено защищённых строк" in m and "1" in m for m in logs)
    s = _state(out)
    assert s.status_of("勇者B") == MANUAL_EDITED and s.get("勇者B") == "моя IMPROVE_ME правка"
    assert s.status_of("勇者A") == AI_TRANSLATED and s.get("勇者A") == "ai improved A"


def test_polish_include_protected_overrides_manual_and_approved_but_never_locked(tmp_path, provider):
    rows = [("m", "manual IMPROVE_ME"), ("a", "approved IMPROVE_ME"), ("l", "locked IMPROVE_ME")]
    game, out, ext = _project(tmp_path, rows)
    _seed_state(out, manual=[rows[0]], approved=[rows[1]], locked=[rows[2]])

    ok, _ = Pipeline(str(game), str(out), log_callback=lambda m: None).polish(
        **_POLISH, texts=["m", "a", "l"], include_protected=True)

    assert ok is True
    t = _translated(ext)
    assert t["m"] == "manual improved" and t["a"] == "approved improved"   # явный запрос — можно
    assert t["l"] == "locked IMPROVE_ME"                                    # замок не пробивается
    s = _state(out)
    assert s.status_of("m") == AI_TRANSLATED and s.status_of("a") == AI_TRANSLATED
    assert s.status_of("l") == LOCKED
    assert "l" not in provider.polished


def test_polish_with_only_protected_strings_explains_why(tmp_path, provider):
    game, out, ext = _project(tmp_path, [("x", "ручной IMPROVE_ME")])
    _seed_state(out, manual=[("x", "ручной IMPROVE_ME")])

    ok, msg = Pipeline(str(game), str(out), log_callback=lambda m: None).polish(**_POLISH)

    assert ok is False
    assert "защищены" in msg
    assert provider.polished == []
    assert _translated(ext)["x"] == "ручной IMPROVE_ME"


# ───────────────────────── translate ─────────────────────────

def test_translate_never_touches_protected_and_translates_the_rest(tmp_path, provider):
    game, out, ext = _project(tmp_path, [("勇者", "моя правка"), ("魔王", ""), ("村人", "")])
    _seed_state(out, manual=[("勇者", "моя правка")])

    ok, _ = Pipeline(str(game), str(out), log_callback=lambda m: None).translate(
        provider="openai", api_key="sk-fake", target_lang="Russian",
        request_delay=0, use_global_tm=False, use_context=False)

    assert ok is True
    assert "勇者" not in provider.translated                   # провайдеру защищённая строка не отправлялась
    assert sorted(provider.translated) == sorted(["魔王", "村人"])   # ровно две непереведённые строки
    t = _translated(ext)
    assert t["勇者"] == "моя правка"
    assert t["魔王"] == "RU:魔王" and t["村人"] == "RU:村人"
    s = _state(out)
    assert s.status_of("勇者") == MANUAL_EDITED
    assert s.status_of("魔王") == AI_TRANSLATED


# ───────────────────────── глоссарий / ремонт кодов / sync ─────────────────────────

def test_glossary_postprocess_skips_protected_entries(tmp_path):
    from engine.glossary import Glossary
    game, out, ext = _project(tmp_path, [("a", "Пришёл 勇者"), ("b", "Пришёл 勇者")])
    g = Glossary()
    g.add("勇者", "Герой")

    Pipeline(str(game), str(out), log_callback=lambda m: None)._apply_glossary_postprocess(
        ext, g, protected={"b"})

    t = _translated(ext)
    assert t["a"] == "Пришёл Герой"        # обычная строка скорректирована
    assert t["b"] == "Пришёл 勇者"         # ручная — нет


def test_validate_does_not_silently_rewrite_protected_text_with_broken_codes(tmp_path):
    orig_ai, orig_manual = "\\C[2]勇者\\C[0]が来た", "\\C[2]魔王\\C[0]が来た"
    game, out, ext = _project(tmp_path, [(orig_ai, "Герой пришёл"), (orig_manual, "Король пришёл")])
    _seed_state(out, ai=[(orig_ai, "Герой пришёл")], manual=[(orig_manual, "Король пришёл")])
    state = _state(out)

    logs: list[str] = []
    tmap = {orig_ai: "Герой пришёл", orig_manual: "Король пришёл"}
    Pipeline(str(game), str(out), log_callback=logs.append)._validate_translations(
        tmap, [orig_ai, orig_manual], "Japanese", state=state, extract_dir=ext)

    t = _translated(ext)
    assert t[orig_ai] == "\\C[2]Герой\\C[0] пришёл"       # ИИ-текст починен (как и раньше)
    assert t[orig_manual] == "Король пришёл"               # ручной — не тронут
    assert tmap[orig_manual] == "Король пришёл"
    assert state.get(orig_manual) == "Король пришёл" and state.status_of(orig_manual) == MANUAL_EDITED
    assert state.get(orig_ai) == "\\C[2]Герой\\C[0] пришёл"
    assert any("НЕ изменены автоматически" in m for m in logs)   # пользователь видит, что осталось


def test_sync_pulls_file_text_but_keeps_protection(tmp_path):
    """Файл — источник истины для правок вне pipeline, но статус защиты сохраняется."""
    game, out, ext = _project(tmp_path, [("勇者", "новый текст из файла")])
    _seed_state(out, manual=[("勇者", "старый текст")])
    state = _state(out)

    Pipeline(str(game), str(out), log_callback=lambda m: None)._sync_existing_translations(ext, state)

    assert state.get("勇者") == "новый текст из файла"
    assert state.status_of("勇者") == MANUAL_EDITED
