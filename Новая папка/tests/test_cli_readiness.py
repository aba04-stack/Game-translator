"""
CLI (P0.4): ключ API из окружения, офлайн mock-провайдер, корректные коды
возврата, мягкая остановка по Ctrl+C и безопасный `import` CSV, который
помечает правки как ручные.

Все тесты идут через настоящий cli.main() с настоящими файлами на диске.
"""
import csv
import io
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
import cli
from conftest import make_mini_project
from engine.state import (
    AI_TRANSLATED, EXTRACTED, MANUAL_EDITED, SOURCE_MANUAL, STATE_FILE, TranslationState,
)
from pipeline import Pipeline

_KEY_VARS = ["RPGMTL_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
             "ANTHROPIC_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY"]


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    for v in _KEY_VARS:
        monkeypatch.delenv(v, raising=False)


def _run(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["cli.py", *argv])
    return cli.main()


def _translated(ext_dir) -> dict[str, str]:
    data = json.loads((ext_dir / "Actors_translated.json").read_text(encoding="utf-8"))
    return {e["original"]: e["translated"] for e in data}


# ───────────────────────── ключ API ─────────────────────────

def test_api_key_resolution_order(monkeypatch):
    r = cli._resolve_api_key
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider")
    monkeypatch.setenv("RPGMTL_API_KEY", "sk-generic")
    assert r("openai", "sk-explicit") == "sk-explicit"           # --api-key главнее всего
    assert r("openai", None) == "sk-generic"                      # затем общая переменная
    monkeypatch.delenv("RPGMTL_API_KEY")
    assert r("openai", "  ") == "sk-provider"                     # пустой --api-key не считается
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    assert r("gemini", None) == "g-key"                           # у gemini две допустимые переменные
    assert r("mock", None) == "mock-no-key"                       # mock ключа не требует
    monkeypatch.delenv("OPENAI_API_KEY")
    assert r("openai", None, base_url="http://localhost:1234/v1") == "not-needed"   # локальный сервер


def test_missing_key_is_a_usage_error_with_actionable_message(monkeypatch, capsys, tmp_path):
    rc = _run(monkeypatch, "translate", "--game", str(tmp_path), "--output", str(tmp_path / "o"),
              "--provider", "claude")
    err = capsys.readouterr().err
    assert rc == cli.EXIT_USAGE == 2
    assert "ANTHROPIC_API_KEY" in err and "--api-key" in err and "RPGMTL_API_KEY" in err
    assert "Traceback" not in err


def test_key_from_environment_reaches_the_provider(monkeypatch, tmp_path):
    seen = {}

    class Spy:
        def translate_batch(self, texts, log_cb=None):
            return [f"RU:{t}" for t in texts]

    def fake_get_provider(cfg):
        seen["key"], seen["provider"] = cfg.api_key, cfg.provider
        return Spy()

    monkeypatch.setattr(ai_translator, "_get_provider", fake_get_provider)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    game, out, ext = make_mini_project(tmp_path, [("勇者", "")])

    rc = _run(monkeypatch, "translate", "--game", str(game), "--output", str(out), "--provider", "openai")

    assert rc == 0
    assert seen == {"key": "sk-from-env", "provider": "openai"}
    assert _translated(ext)["勇者"] == "RU:勇者"


# ───────────────────────── mock-провайдер ─────────────────────────

def test_mock_provider_runs_the_whole_pipeline_offline(monkeypatch, tmp_path, capsys):
    """Без ключа и без сети: translate → polish → (inject) отрабатывают на реальных файлах."""
    def no_network(*a, **k):
        raise AssertionError("mock-провайдер не должен ходить в сеть")
    import socket
    monkeypatch.setattr(socket.socket, "connect", no_network)

    orig_codes = "\\C[2]勇者\\C[0]が来た"
    game, out, ext = make_mini_project(tmp_path, [(orig_codes, ""), ("魔王", "")])

    rc = _run(monkeypatch, "translate", "--game", str(game), "--output", str(out),
              "--provider", "mock", "--target-lang", "English")
    assert rc == 0
    t = _translated(ext)
    assert t["魔王"] == "[English] 魔王"
    assert t[orig_codes] == "[English] " + orig_codes             # escape-коды целы
    from ai_translator import _validate_codes
    assert _validate_codes(orig_codes, t[orig_codes])

    rc = _run(monkeypatch, "polish", "--game", str(game), "--output", str(out), "--provider", "mock")
    assert rc == 0
    assert _translated(ext) == t                                   # mock-полировка ничего не меняет
    assert "Traceback" not in capsys.readouterr().err


def test_mock_provider_unit():
    cfg = ai_translator.TranslatorConfig(provider="mock", target_lang="Russian")
    assert cfg.get_model() == "mock"
    p = ai_translator._get_provider(cfg)
    assert isinstance(p, ai_translator.MockProvider)
    assert p.translate_batch(["a", "b"]) == ["[Russian] a", "[Russian] b"]
    assert p.polish_batch([("o", "draft")]) == ["draft"]


# ───────────────────────── Ctrl+C ─────────────────────────

def test_ctrl_c_stops_after_current_batch_keeps_progress_and_returns_130(monkeypatch, tmp_path):
    originals = [f"文{i}" for i in range(6)]
    game, out, ext = make_mini_project(tmp_path, [(o, "") for o in originals])
    calls = {"n": 0}

    class SigintOnSecondBatch:
        def translate_batch(self, texts, log_cb=None):
            calls["n"] += 1
            if calls["n"] == 2:
                # Наш обработчик должен быть установлен — иначе SIGINT убьёт весь pytest.
                assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
                os.kill(os.getpid(), signal.SIGINT)
            return [f"RU:{t}" for t in texts]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: SigintOnSecondBatch())
    before = signal.getsignal(signal.SIGINT)

    rc = _run(monkeypatch, "translate", "--game", str(game), "--output", str(out),
              "--provider", "openai", "--api-key", "sk-x", "--batch-size", "2", "--max-workers", "1")

    assert rc == cli.EXIT_INTERRUPTED == 130
    assert signal.getsignal(signal.SIGINT) is before               # обработчик восстановлен
    assert calls["n"] == 2                                          # третий батч не запускался
    state = TranslationState(out / STATE_FILE)
    saved = [o for o in originals if state.has(o)]
    assert saved == originals[:4]                                   # прогресс двух батчей сохранён
    t = _translated(ext)
    assert [t[o] for o in originals[:4]] == [f"RU:{o}" for o in originals[:4]]
    assert t[originals[4]] == "" and t[originals[5]] == ""          # остальное — не тронуто


def test_second_ctrl_c_raises_keyboard_interrupt():
    ev = threading.Event()
    with cli._graceful_sigint(ev):
        os.kill(os.getpid(), signal.SIGINT)
        assert ev.is_set()                                          # первый — мягко
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGINT)                     # второй — сразу
            time.sleep(0.5)                                         # дать обработчику сработать


def test_main_turns_keyboard_interrupt_into_130(monkeypatch, capsys, tmp_path):
    def boom(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "cmd_stats", boom)
    rc = _run(monkeypatch, "stats", "--game", str(tmp_path), "--output", str(tmp_path))
    assert rc == 130 and "Прервано" in capsys.readouterr().err


# ───────────────────────── прогресс ─────────────────────────

class _Stream(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def test_progress_in_ci_logs_prints_one_line_per_ten_percent_not_carriage_returns():
    s = _Stream(tty=False)
    pr = cli._ProgressPrinter(s)
    for i in range(1, 101):
        pr.update(i, 100, 0.0, "f.json")
    lines = s.getvalue().splitlines()
    assert "\r" not in s.getvalue()
    assert 10 <= len(lines) <= 12 and lines[-1].lstrip().startswith("[100%]")


def test_progress_in_terminal_redraws_one_line_and_ends_with_newline():
    s = _Stream(tty=True)
    pr = cli._ProgressPrinter(s)
    pr.update(1, 2, 5.0); pr.update(2, 2, 0.0)
    out = s.getvalue()
    assert out.count("\r") == 2 and "ETA: 5s" in out and out.endswith("\n")


# ───────────────────────── polish --include-protected ─────────────────────────

def test_polish_include_protected_flag_wiring(monkeypatch, tmp_path):
    class Improver:
        def polish_batch(self, pairs, log_cb=None):
            return [d.replace("IMPROVE_ME", "improved") for _, d in pairs]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Improver())
    game, out, ext = make_mini_project(tmp_path, [("a", "ai IMPROVE_ME"), ("b", "ручной IMPROVE_ME")])
    st = TranslationState(out / STATE_FILE)
    st.set("a", "ai IMPROVE_ME"); st.set("b", "ручной IMPROVE_ME", source=SOURCE_MANUAL); st.save()
    base = ["polish", "--game", str(game), "--output", str(out), "--provider", "openai", "--api-key", "sk-x"]

    assert _run(monkeypatch, *base) == 0
    assert _translated(ext) == {"a": "ai improved", "b": "ручной IMPROVE_ME"}     # по умолчанию ручное пропущено

    assert _run(monkeypatch, *base, "--include-protected") == 0
    assert _translated(ext)["b"] == "ручной improved"                              # явный флаг — полируем


# ───────────────────────── export / import ─────────────────────────

def _write_translated(ext_dir: Path, stem: str, entries: list[dict]):
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / f"{stem}_translated.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _entry(path, original, translated, file="A.json"):
    return {"file": file, "path": path, "original": original, "translated": translated, "context": "x"}


def _csv(tmp_path, rows, header=("file", "path", "context", "original", "translated")) -> Path:
    p = tmp_path / "in.csv"
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL)
    w.writerow(header)
    w.writerows(rows)
    p.write_bytes(buf.getvalue().encode("utf-8-sig"))
    return p


def _read_json(ext_dir, name="A_translated.json"):
    return json.loads((ext_dir / name).read_text(encoding="utf-8"))


def test_import_marks_only_changed_rows_as_manual(monkeypatch, tmp_path, capsys):
    ext = tmp_path / "extracted"
    _write_translated(ext, "A", [_entry("p1", "勇者", "Герой"), _entry("p2", "魔法", "Магия")])
    csv_path = tmp_path / "t.csv"
    assert _run(monkeypatch, "export", "--output", str(tmp_path), "--csv", str(csv_path)) == 0
    csv_path.write_text(csv_path.read_text(encoding="utf-8-sig").replace("Герой", "Храбрец"),
                        encoding="utf-8-sig")

    rc = _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path))

    out = capsys.readouterr().out
    assert rc == 0 and "обновлено строк: 1" in out and "без изменений: 1" in out
    data = _read_json(ext)
    assert [e["translated"] for e in data] == ["Храбрец", "Магия"]
    st = TranslationState(tmp_path / STATE_FILE)
    assert st.get("勇者") == "Храбрец" and st.status_of("勇者") == MANUAL_EDITED
    assert st.status_of("魔法") == EXTRACTED                         # не менялась — не помечена


def test_reimporting_an_unedited_export_marks_nothing(monkeypatch, tmp_path):
    ext = tmp_path / "extracted"
    _write_translated(ext, "A", [_entry("p1", "勇者", "Герой")])
    csv_path = tmp_path / "t.csv"
    _run(monkeypatch, "export", "--output", str(tmp_path), "--csv", str(csv_path))

    assert _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path)) == 0
    assert not (tmp_path / STATE_FILE).exists()                      # защита не «размазалась» на всё


def test_import_skips_row_when_csv_original_does_not_match(monkeypatch, tmp_path, capsys):
    """Игру извлекли заново после экспорта → пути сместились: перевод нельзя вкладывать в чужую строку."""
    ext = tmp_path / "extracted"
    _write_translated(ext, "A", [_entry("p1", "勇者", "Герой")])
    csv_path = _csv(tmp_path, [["A", "p1", "x", "ДРУГОЙ_ОРИГИНАЛ", "Не туда"]])

    rc = _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path))

    assert rc == cli.EXIT_FAIL                                        # ничего не применилось
    assert "оригинал в CSV не совпал" in capsys.readouterr().out
    assert _read_json(ext)[0]["translated"] == "Герой"


def test_import_pairs_rows_without_path_by_original_in_file_order(monkeypatch, tmp_path):
    ext = tmp_path / "extracted"
    _write_translated(ext, "A", [_entry("", "はい", "Да"), _entry("", "はい", "Да"), _entry("", "いいえ", "Нет")])
    csv_path = _csv(tmp_path, [["A", "", "x", "はい", "Ага"], ["A", "", "x", "はい", "Угу"]])

    assert _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path)) == 0

    assert [e["translated"] for e in _read_json(ext)] == ["Ага", "Угу", "Нет"]


def test_import_never_wipes_a_translation_with_an_empty_cell(monkeypatch, tmp_path, capsys):
    ext = tmp_path / "extracted"
    _write_translated(ext, "A", [_entry("p1", "勇者", "Герой"), _entry("p2", "魔法", "Магия")])
    csv_path = _csv(tmp_path, [["A", "p1", "x", "勇者", ""], ["A", "p2", "x", "魔法", "Чары"]])

    assert _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path)) == 0

    assert [e["translated"] for e in _read_json(ext)] == ["Герой", "Чары"]
    assert "пустых переводов в CSV пропущено: 1" in capsys.readouterr().out


def test_import_warns_about_broken_escape_codes(monkeypatch, tmp_path, capsys):
    ext = tmp_path / "extracted"
    orig = "\\C[2]勇者\\C[0]が来た"
    _write_translated(ext, "A", [_entry("p1", orig, "\\C[2]Герой\\C[0] пришёл")])
    csv_path = _csv(tmp_path, [["A", "p1", "x", orig, "Герой пришёл"]])               # коды потеряны

    rc = _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path))

    assert rc == 0
    assert "нарушены escape-коды в 1 строках" in capsys.readouterr().out


@pytest.mark.parametrize("header", [("file", "path", "context", "original"),         # нет translated
                                    ("path", "original", "translated")])               # нет file
def test_import_rejects_csv_without_required_columns(monkeypatch, tmp_path, capsys, header):
    _write_translated(tmp_path / "extracted", "A", [_entry("p1", "勇者", "Герой")])
    csv_path = _csv(tmp_path, [["x"] * len(header)], header=header)

    rc = _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path))

    err = capsys.readouterr().err
    assert rc == cli.EXIT_FAIL and "нет обязательных колонок" in err and "Traceback" not in err


def test_import_missing_inputs_fail_cleanly(monkeypatch, tmp_path, capsys):
    assert _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(tmp_path / "nope.csv")) == 1
    assert "не найден" in capsys.readouterr().err
    (tmp_path / "x.csv").write_text("a;b\n", encoding="utf-8")
    assert _run(monkeypatch, "import", "--output", str(tmp_path / "no_such_out"), "--csv", str(tmp_path / "x.csv")) == 1
    assert "extracted не найдена" in capsys.readouterr().err


def test_import_corrupt_json_is_reported_and_other_files_still_processed(monkeypatch, tmp_path, capsys):
    ext = tmp_path / "extracted"
    _write_translated(ext, "A", [_entry("p1", "勇者", "Герой")])
    (ext / "B_translated.json").write_text("{ broken", encoding="utf-8")
    csv_path = _csv(tmp_path, [["A", "p1", "x", "勇者", "Храбрец"], ["B", "p1", "x", "魔法", "Чары"]])

    rc = _run(monkeypatch, "import", "--output", str(tmp_path), "--csv", str(csv_path))

    cap = capsys.readouterr()
    assert rc == cli.EXIT_FAIL
    assert "B_translated.json" in cap.err and "Traceback" not in cap.err
    assert _read_json(ext)[0]["translated"] == "Храбрец"              # A обработан, несмотря на ошибку в B


def test_export_reports_corrupt_json_without_traceback(monkeypatch, tmp_path, capsys):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "A_translated.json").write_text("{ broken", encoding="utf-8")
    rc = _run(monkeypatch, "export", "--output", str(tmp_path), "--csv", str(tmp_path / "o.csv"))
    err = capsys.readouterr().err
    assert rc == 1 and "A_translated.json" in err and "Traceback" not in err


def test_imported_translations_survive_a_following_polish(monkeypatch, tmp_path):
    """Сквозной сценарий: переводчик правит CSV → import → AI-полировка его не затирает."""
    class Improver:
        def polish_batch(self, pairs, log_cb=None):
            return [d.replace("IMPROVE_ME", "improved") for _, d in pairs]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Improver())
    game, out, ext = make_mini_project(tmp_path, [("a", "ai IMPROVE_ME"), ("b", "ai IMPROVE_ME")])
    csv_path = tmp_path / "t.csv"
    assert _run(monkeypatch, "export", "--output", str(out), "--csv", str(csv_path)) == 0
    # Переводчик правит только строку b:
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    fixed = [l.replace("ai IMPROVE_ME", "работа переводчика IMPROVE_ME") if l.startswith('"Actors";"[1].name"') else l
             for l in lines]
    csv_path.write_text("\n".join(fixed) + "\n", encoding="utf-8-sig")
    assert _run(monkeypatch, "import", "--output", str(out), "--csv", str(csv_path)) == 0

    ok, _ = Pipeline(str(game), str(out), log_callback=lambda m: None).polish(
        provider="openai", api_key="sk-x", request_delay=0)

    assert ok is True
    t = _translated(ext)
    assert t["a"] == "ai improved"                                    # ИИ-строка отполирована
    assert t["b"] == "работа переводчика IMPROVE_ME"                  # работа человека — нет
    assert TranslationState(out / STATE_FILE).status_of("b") == MANUAL_EDITED
    assert TranslationState(out / STATE_FILE).status_of("a") == AI_TRANSLATED


# ───────────────────────── Inject: QA-барьер fail-closed ─────────────────────────

def test_inject_gate_stops_when_qa_cannot_read_a_translation_file(tmp_path):
    """QA-барьер перед Inject не должен пропускать непроверенные данные: нечитаемый
    *_translated.json = «не проверено», а не «критических нет»."""
    game, out, ext = make_mini_project(tmp_path, [("勇者", "Герой")])
    (ext / "Broken_translated.json").write_text("{ not json", encoding="utf-8")
    before = (game / "data" / "Actors.json").read_bytes()

    logs: list[str] = []
    ok, msg = Pipeline(str(game), str(out), log_callback=logs.append).inject()

    assert ok is False
    assert "QA не смог прочитать" in msg and "Broken_translated.json" in msg
    assert (game / "data" / "Actors.json").read_bytes() == before        # игра не тронута


# ───────────────────────── restore ─────────────────────────

def test_cli_restore_returns_the_original_and_exits_0(monkeypatch, tmp_path, capsys):
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "A.json").write_text("injected")
    (game / "_backup_original").mkdir()
    (game / "_backup_original" / "A.json").write_text("original")

    rc = _run(monkeypatch, "restore", "--game", str(game))

    assert rc == 0
    assert (game / "data" / "A.json").read_text() == "original"
    assert "Восстановлено 1 файлов" in capsys.readouterr().out


def test_cli_restore_without_a_backup_is_an_error(monkeypatch, tmp_path, capsys):
    game = tmp_path / "game"
    game.mkdir()
    assert _run(monkeypatch, "restore", "--game", str(game)) == cli.EXIT_FAIL
    assert "не найдена" in capsys.readouterr().out


def test_cli_restore_reports_partial_failure_with_nonzero_exit(monkeypatch, tmp_path, capsys):
    game = tmp_path / "game"
    (game / "data" / "A.json").mkdir(parents=True)                         # на месте файла — папка
    (game / "_backup_original").mkdir()
    (game / "_backup_original" / "A.json").write_text("original")

    rc = _run(monkeypatch, "restore", "--game", str(game))

    out = capsys.readouterr().out
    assert rc == cli.EXIT_FAIL and "НЕ удалось: 1" in out
