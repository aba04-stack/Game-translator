"""
test_qa_report.py — тесты для пост-переводной QA-проверки (v50, round 6).

Идея — из профессиональных localization QA-инструментов (Smartling,
SimpleLocalize, memoQ/Xbench): автоматические проверки переведённых строк
ДО инъекции вместо поиска проблемы постфактум в игре. Раздел важности:
critical (коды/плейсхолдеры потеряны — реально ломает игру) vs warning
(числа разошлись, перевод сильно длиннее оригинала — стоит посмотреть).

Покрывает:
- check_entry: все три проверки по отдельности + de-dup чисел внутри кодов
- run_qa_report: агрегация по нескольким файлам, сортировка critical первыми,
  карантинные/непереведённые записи пропускаются
- Pipeline.qa_report(): интеграция — сохраняет qa_report.json, логирует сводку
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qa_report import check_entry, run_qa_report, format_qa_report_text


# ─────────────────────────────────────────────────────────────────────────────
# check_entry
# ─────────────────────────────────────────────────────────────────────────────

def test_check_entry_flags_lost_codes_as_critical():
    entry = {"original": r"\C[1]太郎\C[0]", "translated": "Таро"}
    issues = check_entry(entry)
    types = {i["type"] for i in issues}
    assert "codes" in types
    codes_issue = next(i for i in issues if i["type"] == "codes")
    assert codes_issue["severity"] == "critical"


def test_check_entry_flags_number_mismatch_as_warning():
    entry = {"original": "HPが50回復した", "translated": "HP восстановлено на 100"}
    issues = check_entry(entry)
    assert len(issues) == 1
    assert issues[0]["type"] == "numbers"
    assert issues[0]["severity"] == "warning"


def test_check_entry_does_not_double_report_digits_inside_lost_codes():
    """
    Цифры внутри \\C[1] и т.п. не должны отдельно триггерить "numbers" —
    это тот же самый потерянный код, а не отдельная проблема. Но реальное
    число ВНЕ кодов (не входящее в скобки) обязано ловиться отдельно.
    """
    entry = {"original": r"HP\C[1]50\C[0]回復", "translated": "ХП восстановлено"}
    issues = check_entry(entry)
    codes = [i for i in issues if i["type"] == "codes"]
    numbers = [i for i in issues if i["type"] == "numbers"]
    assert len(codes) == 1
    # "50" стоит МЕЖДУ кодами, не внутри скобок — это реальное потерянное
    # число, должно поймать отдельно.
    assert len(numbers) == 1
    assert "50" in numbers[0]["detail"]


def test_check_entry_flags_excessive_length_ratio():
    entry = {"original": "こんにちは友人", "translated": "Здравствуй, мой дорогой давний друг, как твои дела сегодня"}
    issues = check_entry(entry, length_ratio=3.0)
    assert any(i["type"] == "length" for i in issues)
    assert all(i["severity"] == "warning" for i in issues if i["type"] == "length")


def test_check_entry_skips_length_check_for_very_short_original():
    """Короткие строки типа приветствий часто легитимно расширяются при переводе."""
    entry = {"original": "はい", "translated": "Да, конечно, я полностью согласен с этим"}
    issues = check_entry(entry)
    assert not any(i["type"] == "length" for i in issues)


def test_check_entry_clean_translation_has_no_issues():
    entry = {"original": "こんにちは", "translated": "Привет"}
    assert check_entry(entry) == []


def test_check_entry_skips_untranslated_entries():
    """Пустой translated (в т.ч. карантин из wolf_extractor.py) — не наша забота здесь."""
    entry = {"original": "こんにちは", "translated": ""}
    assert check_entry(entry) == []


# ─────────────────────────────────────────────────────────────────────────────
# run_qa_report
# ─────────────────────────────────────────────────────────────────────────────

def test_run_qa_report_aggregates_across_files_and_sorts_critical_first():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        entries_a = [
            {"file": "a.json", "path": "[0]", "original": "こんにちは", "translated": "Привет"},
            {"file": "a.json", "path": "[1]", "original": "HPが50回復", "translated": "HP на 100"},
        ]
        entries_b = [
            {"file": "b.json", "path": "[0]", "original": r"\N[1]さん", "translated": "друг"},
        ]
        (tmp / "a_translated.json").write_text(json.dumps(entries_a, ensure_ascii=False), encoding="utf-8")
        (tmp / "b_translated.json").write_text(json.dumps(entries_b, ensure_ascii=False), encoding="utf-8")

        report = run_qa_report(tmp)
        assert report["checked"] == 3
        assert report["clean"] == 1
        assert report["critical_count"] == 1
        assert report["warning_count"] == 1
        # critical должен идти первым в списке issues
        assert report["issues"][0]["severity"] == "critical"


def test_run_qa_report_skips_entries_without_translation():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        entries = [
            {"file": "a.json", "path": "[0]", "original": "こんにちは", "translated": ""},
            {"file": "a.json", "path": "[1]", "original": "アニメ", "translated": ""},
        ]
        (tmp / "a_translated.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        report = run_qa_report(tmp)
        assert report["checked"] == 0, "Непереведённые (в т.ч. карантин) не должны попадать в проверку"


def test_run_qa_report_empty_dir_returns_zeroed_report():
    with tempfile.TemporaryDirectory() as tmp:
        report = run_qa_report(Path(tmp))
        assert report == {"checked": 0, "clean": 0, "critical_count": 0, "warning_count": 0,
                          "issues": [], "unreadable_files": []}


def test_format_qa_report_text_is_readable_and_truncates():
    report = {
        "checked": 100, "clean": 0, "critical_count": 60, "warning_count": 40,
        "issues": [
            {"file": f"f{i}.json", "path": f"[{i}]", "original": "x", "translated": "y",
             "type": "codes", "severity": "critical", "detail": "test"}
            for i in range(60)
        ],
    }
    text = format_qa_report_text(report, max_lines=10)
    assert "100 переведённых" in text
    assert text.count("🔴") == 10
    assert "ещё 50" in text


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline.qa_report() — интеграция
# ─────────────────────────────────────────────────────────────────────────────

def test_pipeline_qa_report_saves_json_and_logs_summary(tmp_path):
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    (game_dir / "data").mkdir(parents=True)
    (game_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "T"}), encoding="utf-8")
    (game_dir / "js").mkdir()
    (game_dir / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"
    (out_dir / "extracted").mkdir(parents=True)

    entries = [
        {"file": "a.json", "path": "[0]", "original": r"\C[1]HP50\C[0]", "translated": "ХП"},
        {"file": "a.json", "path": "[1]", "original": "こんにちは", "translated": "Привет"},
    ]
    (out_dir / "extracted" / "a_translated.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )

    logs: list[str] = []
    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=logs.append)
    report = p.qa_report()

    assert report["critical_count"] == 1
    saved = json.loads((out_dir / "qa_report.json").read_text(encoding="utf-8"))
    assert saved["critical_count"] == 1
    assert any("QA-проверка" in l for l in logs)


def test_pipeline_qa_report_handles_missing_extracted_dir_gracefully(tmp_path):
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()  # extracted/ не создана — Extract ещё не запускался

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    report = p.qa_report()
    assert report["checked"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# GUI: кнопка "QA-отчёт" в rpgmtl_desktop.py
# ─────────────────────────────────────────────────────────────────────────────

def test_qa_report_button_exists_and_toggles_with_busy_state():
    """
    Кнопка должна существовать, иметь обработчик _run_qa_report и корректно
    включаться/выключаться вместе с Extract/Translate/Insert во время
    выполнения операций (_set_busy).
    """
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    assert hasattr(win, "btn_qa_report")
    assert hasattr(win, "_run_qa_report")
    assert win.btn_qa_report.isEnabled()

    win._set_busy(True, "test")
    assert not win.btn_qa_report.isEnabled(), "Кнопка QA-отчёта должна блокироваться во время других операций"

    win._set_busy(False)
    assert win.btn_qa_report.isEnabled()



# ───────────────────────── нечитаемые файлы / прогресс ─────────────────────────

def _write(dirpath, name, content):
    (Path(dirpath) / name).write_text(content, encoding="utf-8")


def test_qa_reports_unreadable_files_instead_of_silently_skipping_them():
    """Раньше битый *_translated.json молча пропускался: «проверено N, чисто» выглядело
    как гарантия по всем данным, хотя часть не проверялась вообще."""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "Good_translated.json", json.dumps(
            [{"file": "Good.json", "path": "p", "original": "勇者", "translated": "Герой"}], ensure_ascii=False))
        _write(tmp, "Broken_translated.json", "{ not json")
        _write(tmp, "NotList_translated.json", '{"a": 1}')

        report = run_qa_report(Path(tmp))

        assert report["checked"] == 1 and report["clean"] == 1
        names = sorted(u["file"] for u in report["unreadable_files"])
        assert names == ["Broken_translated.json", "NotList_translated.json"]
        assert all(u["error"] for u in report["unreadable_files"])
        text = format_qa_report_text(report)
        assert "НЕ проверено" in text and "Broken_translated.json" in text


def test_qa_ignores_non_dict_entries_instead_of_crashing():
    import json
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "A_translated.json", json.dumps(
            ["junk", 5, None, {"file": "A.json", "path": "p", "original": "勇者", "translated": "Герой"}],
            ensure_ascii=False))
        report = run_qa_report(Path(tmp))
        assert report["checked"] == 1 and report["unreadable_files"] == []


def test_qa_progress_is_reported_once_per_file_including_unreadable():
    import json
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(3):
            _write(tmp, f"F{i}_translated.json", json.dumps(
                [{"file": f"F{i}.json", "path": "p", "original": "勇者", "translated": "Герой"}], ensure_ascii=False))
        _write(tmp, "Z_translated.json", "{ broken")
        calls = []

        run_qa_report(Path(tmp), progress_callback=lambda c, t, e, f: calls.append((c, t, e, f)))

        assert [c[0] for c in calls] == [1, 2, 3, 4]
        assert all(c[1] == 4 and isinstance(c[2], float) and c[2] >= 0 and c[3].endswith("_translated.json")
                   for c in calls)
        assert calls[-1][2] == 0.0
