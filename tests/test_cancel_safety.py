"""
P0.2 — отмена и сбои в Translate не должны портить данные:
  * ранее полученные ВАЛИДНЫЕ переводы сохраняются (в т.ч. батч, закончившийся
    уже после нажатия «Стоп» — за него заплачено);
  * в файлы и состояние никогда не попадает None / мусор / строка без кодов;
  * ручные правки переживают отмену;
  * файлы и состояние остаются валидным JSON и согласованы друг с другом;
  * отмена срабатывает быстро, даже во время паузы между запросами.

Каждый тест гоняет настоящий Pipeline.translate на настоящих файлах; сеть
подменяется поддельным провайдером.
"""
import json
import sys
import threading
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
import cli
from conftest import make_mini_project
from engine.state import MANUAL_EDITED, SOURCE_MANUAL, STATE_FILE, TranslationState
from pipeline import Pipeline


def _translate(game, out, provider, monkeypatch, **kw):
    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: provider)
    kw.setdefault("request_delay", 0)
    kw.setdefault("max_retries", 1)
    p = Pipeline(str(game), str(out), log_callback=lambda m: None)
    ok, msg = p.translate(provider="openai", api_key="k", use_global_tm=False, **kw)
    return p, ok, msg


def _files(ext) -> dict[str, str]:
    data = json.loads((ext / "Actors_translated.json").read_text(encoding="utf-8"))   # валидный JSON
    return {e["original"]: e["translated"] for e in data}


def _state(out) -> dict[str, str]:
    raw = json.loads((out / STATE_FILE).read_text(encoding="utf-8")) if (out / STATE_FILE).exists() else {"cache": {}}
    return raw["cache"]


def _assert_consistent_and_clean(ext, out):
    """Файлы и состояние: валидный JSON, нет None, всё сохранённое в состоянии есть в файлах."""
    files, state = _files(ext), _state(out)
    for k, v in {**files, **state}.items():
        assert isinstance(v, str), f"{k!r}: {v!r}"          # ни None, ни чисел
    for original, translated in state.items():
        assert files.get(original) == translated, f"{original!r}: файл и состояние расходятся"
    return files, state


# ───────────────────────── отмена ─────────────────────────

def test_cancel_in_parallel_mode_keeps_the_batch_that_already_finished(tmp_path, monkeypatch):
    """Регрессия: `future.cancel(); continue` выбрасывал уже выполненный (оплаченный) батч —
    отмена сразу после первого успешного давала «переведено 0/N»."""
    cancel = threading.Event()

    class Provider:
        def __init__(self):
            self.calls = 0

        def translate_batch(self, texts, log_cb=None):
            self.calls += 1
            if self.calls == 1:
                cancel.set()                                  # «Стоп» нажат, пока батч ещё работает
            time.sleep(0.05)
            return [f"RU:{t}" for t in texts]

    game, out, ext = make_mini_project(tmp_path, [(f"文{i}", "") for i in range(8)])
    p, ok, msg = _translate(game, out, Provider(), monkeypatch,
                            batch_size=2, max_workers=3, cancel_event=cancel)

    assert ok is False and "Прервано" in msg
    files, state = _assert_consistent_and_clean(ext, out)
    done = [o for o, t in files.items() if t]
    assert len(done) >= 2 and "文0" in done and "文1" in done      # первый батч не потерян
    assert all(files[o] == f"RU:{o}" for o in done)
    assert p.last_translate_summary["cancelled"] is True
    assert p.last_translate_summary["translated"] == len(done)


def test_cancel_in_sequential_mode_keeps_progress_and_manual_edits(tmp_path, monkeypatch):
    cancel = threading.Event()

    class Provider:
        n = 0

        def translate_batch(self, texts, log_cb=None):
            Provider.n += 1
            if Provider.n == 1:
                cancel.set()
            return [f"RU:{t}" for t in texts]

    game, out, ext = make_mini_project(tmp_path, [("手動", "моя правка"), ("文1", ""), ("文2", ""), ("文3", "")])
    st = TranslationState(out / STATE_FILE)
    st.set("手動", "моя правка", source=SOURCE_MANUAL)
    st.save()

    p, ok, msg = _translate(game, out, Provider(), monkeypatch, batch_size=1, max_workers=1, cancel_event=cancel)

    assert ok is False and Provider.n == 1                         # после «Стоп» новые батчи не запускались
    files, state = _assert_consistent_and_clean(ext, out)
    assert files["手動"] == "моя правка"                            # ручная правка пережила отмену
    assert TranslationState(out / STATE_FILE).status_of("手動") == MANUAL_EDITED
    assert files["文1"] == "RU:文1" and files["文2"] == "" and files["文3"] == ""


def test_cancel_during_request_delay_returns_promptly(tmp_path, monkeypatch):
    cancel = threading.Event()

    class Provider:
        def translate_batch(self, texts, log_cb=None):
            return [f"RU:{t}" for t in texts]

    game, out, ext = make_mini_project(tmp_path, [(f"文{i}", "") for i in range(6)])
    threading.Timer(0.3, cancel.set).start()
    started = time.time()
    p, ok, msg = _translate(game, out, Provider(), monkeypatch, batch_size=2, max_workers=1,
                            request_delay=5.0, cancel_event=cancel)

    assert ok is False
    assert time.time() - started < 2.5                             # не ждёт 5-секундную паузу
    _assert_consistent_and_clean(ext, out)


# ───────────────────────── сбои провайдера ─────────────────────────

def test_provider_failure_leaves_untranslated_strings_empty_and_keeps_the_rest(tmp_path, monkeypatch):
    class Provider:
        def translate_batch(self, texts, log_cb=None):
            if "文2" in texts or "文3" in texts:
                raise RuntimeError("API 500")
            return [f"RU:{t}" for t in texts]

    game, out, ext = make_mini_project(tmp_path, [(f"文{i}", "") for i in range(4)])
    p, ok, msg = _translate(game, out, Provider(), monkeypatch, batch_size=2, max_workers=1)

    assert ok is True and "НЕ УДАЛОСЬ перевести 2" in msg           # главное сообщение честно говорит о провале
    files, state = _assert_consistent_and_clean(ext, out)
    assert files == {"文0": "RU:文0", "文1": "RU:文1", "文2": "", "文3": ""}
    assert set(state) == {"文0", "文1"}                              # неудачные в кэш не попали
    assert p.last_translate_summary == {"unique": 4, "translated": 2, "failed": 2, "remaining": 2,
                                        "cancelled": False, "fatal_error": ""}


def test_none_in_provider_reply_is_never_written(tmp_path, monkeypatch):
    class Provider:
        def translate_batch(self, texts, log_cb=None):
            return [None if t.endswith("1") else f"RU:{t}" for t in texts]

    game, out, ext = make_mini_project(tmp_path, [("文0", ""), ("文1", ""), ("文2", "")])
    p, ok, msg = _translate(game, out, Provider(), monkeypatch, batch_size=3, max_workers=1)

    files, state = _assert_consistent_and_clean(ext, out)
    assert files == {"文0": "RU:文0", "文1": "", "文2": "RU:文2"}
    assert p.last_translate_summary["failed"] == 1


def test_translation_that_lost_escape_codes_is_never_saved(tmp_path, monkeypatch):
    class Provider:
        def translate_batch(self, texts, log_cb=None):
            return ["Герой пришёл" for _ in texts]                  # все коды потеряны

    originals = ["\\C[2]勇者\\C[0]が来た", "\\C[2]魔王\\C[0]が来た", "\\C[2]村人\\C[0]が来た"]
    game, out, ext = make_mini_project(tmp_path, [(o, "") for o in originals])
    p, ok, msg = _translate(game, out, Provider(), monkeypatch, batch_size=1, max_workers=1)

    files, state = _assert_consistent_and_clean(ext, out)
    assert all(files[o] == "" for o in originals)                  # сломанный текст в игру не уйдёт
    assert state == {}
    assert p.last_translate_summary["failed"] == 3


def test_failure_in_parallel_mode_is_also_clean(tmp_path, monkeypatch):
    class Provider:
        def translate_batch(self, texts, log_cb=None):
            if "文3" in texts:
                raise RuntimeError("boom")
            return [f"RU:{t}" for t in texts]

    game, out, ext = make_mini_project(tmp_path, [(f"文{i}", "") for i in range(6)])
    p, ok, msg = _translate(game, out, Provider(), monkeypatch, batch_size=2, max_workers=3)

    files, state = _assert_consistent_and_clean(ext, out)
    assert files["文3"] == "" and files["文0"] == "RU:文0" and files["文5"] == "RU:文5"


# ───────────────────────── итог для скриптов (CLI) ─────────────────────────

def _cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["cli.py", *argv])
    return cli.main()


def test_cli_returns_3_when_some_strings_could_not_be_translated(tmp_path, monkeypatch, capsys):
    class Provider:
        def translate_batch(self, texts, log_cb=None):
            if "文1" in texts:
                raise RuntimeError("boom")
            return [f"RU:{t}" for t in texts]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Provider())
    game, out, ext = make_mini_project(tmp_path, [("文0", ""), ("文1", "")])

    rc = _cli(monkeypatch, "translate", "--game", str(game), "--output", str(out),
              "--provider", "openai", "--api-key", "sk-x", "--batch-size", "1", "--max-retries", "1")

    assert rc == cli.EXIT_PARTIAL == 3
    assert "Не переведено строк: 1" in capsys.readouterr().err


def test_cli_returns_0_when_everything_is_translated(tmp_path, monkeypatch):
    class Provider:
        def translate_batch(self, texts, log_cb=None):
            return [f"RU:{t}" for t in texts]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Provider())
    game, out, ext = make_mini_project(tmp_path, [("文0", ""), ("文1", "")])
    assert _cli(monkeypatch, "translate", "--game", str(game), "--output", str(out),
                "--provider", "openai", "--api-key", "sk-x") == 0
