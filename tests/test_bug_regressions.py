"""
test_bug_regressions.py — регрессионные тесты для всех 13 исправлений v49.

Каждый тест проверяет конкретный баг, который был исправлен, и гарантирует,
что он не вернётся при будущих изменениях.
"""
import os

# BUG FIX: должно быть установлено ДО любого импорта PyQt6 (напрямую здесь или
# транзитивно через rpgmtl_desktop) — иначе QApplication() в headless-окружении
# роняет весь процесс pytest через SIGABRT. tests/conftest.py уже делает это на
# уровне сессии, но дублируем здесь на случай запуска файла в обход conftest
# (например `python -m pytest tests/test_bug_regressions.py::test_x` из другого cwd).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import re
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1: Мёртвый return в _run_inject (синтаксический, нет рантайм-теста)
# Проверяем косвенно — модуль импортируется без ошибок
# ─────────────────────────────────────────────────────────────────────────────

def test_desktop_module_imports_cleanly():
    """Модуль rpgmtl_desktop должен импортироваться без SyntaxError."""
    import importlib, importlib.util
    spec = importlib.util.spec_from_file_location(
        "rpgmtl_desktop",
        Path(__file__).parent.parent / "rpgmtl_desktop.py",
    )
    # Не запускаем — PyQt6 может отсутствовать; только компиляция AST
    import ast
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Нет двух подряд return на одном уровне вложенности в одной функции
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            for i in range(len(body) - 1):
                assert not (
                    isinstance(body[i], ast.Return) and isinstance(body[i + 1], ast.Return)
                ), f"Двойной return в функции {node.name}:{body[i].lineno}"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2: Аннотация progress_callback — все файлы должны использовать 4 аргумента
# ─────────────────────────────────────────────────────────────────────────────

def test_progress_callback_annotations_all_correct():
    """Везде должно быть Callable[[int, int, float, str], None], не [int, int, str]."""
    root = Path(__file__).parent.parent
    wrong = re.compile(r"Callable\[\[int, int, str\], None\]")
    py_files = [
        f for f in root.rglob("*.py")
        if "__pycache__" not in str(f) and "tests/" not in str(f)
    ]
    violations = []
    for f in py_files:
        src = f.read_text(encoding="utf-8")
        if wrong.search(src):
            violations.append(f.relative_to(root))
    assert violations == [], f"Неправильные аннотации progress_callback в: {violations}"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 3: cfg_copy в параллельном retry — теперь берётся из futures dict
# ─────────────────────────────────────────────────────────────────────────────

def test_parallel_retry_uses_per_batch_cfg_copy():
    """futures[future] должен содержать 4 элемента (включая cfg_copy)."""
    src = (Path(__file__).parent.parent / "ai_translator.py").read_text(encoding="utf-8")
    # Проверяем что futures хранит cfg_copy
    assert "futures[future] = (b_start, batch, indices, cfg_copy)" in src, \
        "futures должны хранить cfg_copy как 4-й элемент"
    assert "b_start, batch, indices, batch_cfg_copy = futures[future]" in src, \
        "as_completed должен извлекать batch_cfg_copy из futures"
    # Parallel retry создаёт retry_cfg на основе batch_cfg_copy (smart retry)
    assert "retry_cfg = TranslatorConfig(**batch_cfg_copy.__dict__)" in src, \
        "parallel retry должен создавать retry_cfg на основе batch_cfg_copy"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 4: Восстановление Unity — _restore_backup теперь маршрутизирует правильно
# ─────────────────────────────────────────────────────────────────────────────

def test_restore_backup_routes_unity_to_recursive_path(tmp_path):
    """Unity-бэкап зеркалит структуру игры: восстанавливается относительно КОРНЯ игры
    (а не в data/, как MV/MZ). Раньше это «проверялось» регуляркой по тексту GUI-кода;
    теперь — реальным вызовом engine.restore.restore_game_backups."""
    from engine.restore import restore_game_backups

    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)                       # data/ есть — но Unity туда попасть не должен
    sub = game / "_backup_unity_original" / "GameName_Data" / "StreamingAssets"
    sub.mkdir(parents=True)
    (sub / "translations.json").write_bytes(b"original unity asset")
    live = game / "GameName_Data" / "StreamingAssets"
    live.mkdir(parents=True)
    (live / "translations.json").write_bytes(b"injected content")

    result = restore_game_backups(game)

    assert result.ok and result.restored == 1
    assert (live / "translations.json").read_bytes() == b"original unity asset"
    assert not (game / "data" / "GameName_Data").exists()      # в data/ ничего не попало


# ─────────────────────────────────────────────────────────────────────────────
# Bug 5: VXAce marker cleanup после ручного restore
# ─────────────────────────────────────────────────────────────────────────────

def test_vxace_marker_cleanup_present_in_restore(tmp_path):
    """После успешного restore снимаются маркеры инъекции ВСЕХ движков (иначе следующий
    Inject упрётся в idempotency guard). Раньше — проверка строк в тексте GUI-кода."""
    from engine.restore import restore_game_backups

    game = tmp_path / "game"
    data = game / "data"
    data.mkdir(parents=True)
    (data / ".rpgmtl_injected").write_text("x")                 # MV/MZ: маркер в data/
    (data / "Actors.json").write_text("injected")
    (game / "_backup_original").mkdir()
    (game / "_backup_original" / "Actors.json").write_text("orig")

    engines = ["vxace", "wolf", "kirikiri", "godot", "tyrano", "renpy", "unity"]
    for e in engines:
        b = game / f"_backup_{e}_original"
        b.mkdir()
        (b / ".rpgmtl_injected").write_text("x")                # маркер лежит внутри бэкапа
        (b / f"{e}.dat").write_text("orig")

    result = restore_game_backups(game)

    assert result.ok
    assert not (data / ".rpgmtl_injected").exists()
    for e in engines:
        assert not (game / f"_backup_{e}_original" / ".rpgmtl_injected").exists(), e
    assert result.markers_removed == 1 + len(engines)


# ─────────────────────────────────────────────────────────────────────────────
# Bug 6: ClaudeProvider — безопасный доступ к content blocks
# ─────────────────────────────────────────────────────────────────────────────

def test_claude_provider_content_extraction_is_safe():
    """ClaudeProvider не должен использовать небезопасный msg.content[0].text."""
    src = (Path(__file__).parent.parent / "ai_translator.py").read_text(encoding="utf-8")
    # Старая небезопасная строка
    assert "msg.content[0].text if msg.content" not in src, \
        "ClaudeProvider всё ещё использует небезопасный msg.content[0].text"
    # Новая безопасная итерация
    assert "b.text for b in msg.content if hasattr(b" in src, \
        "ClaudeProvider должен итерировать content блоки безопасно"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 7: tyrano_injector — убран дублированный if tag_matches
# ─────────────────────────────────────────────────────────────────────────────

def test_tyrano_injector_no_duplicate_if_tag_matches():
    """В tyrano_injector не должно быть дублированного 'if tag_matches:'."""
    src = (Path(__file__).parent.parent / "engine" / "tyrano_injector.py").read_text(encoding="utf-8")
    # Допускаем один if tag_matches (внешний), но не два подряд
    occurrences = [i for i, line in enumerate(src.splitlines())
                   if "if tag_matches:" in line]
    # Не должно быть двух подряд с одинаковым indent
    lines = src.splitlines()
    for idx in occurrences:
        if idx + 1 < len(lines) and "if tag_matches:" in lines[idx + 1]:
            pytest.fail(f"Дублированный if tag_matches: в строке {idx + 1}")


# ─────────────────────────────────────────────────────────────────────────────
# Bugs 8–11: Аннотации в injector/extractor файлах
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel_path", [
    "engine/extractor.py",
    "engine/injector.py",
    "engine/unity_extractor.py",
    "engine/unity_injector.py",
    "renpy_extractor.py",
    "renpy_injector.py",
    "ai_translator.py",
])
def test_no_wrong_progress_annotation_in_file(rel_path):
    """Ни один из целевых файлов не должен содержать старую 3-аргументную аннотацию."""
    root = Path(__file__).parent.parent
    src = (root / rel_path).read_text(encoding="utf-8")
    assert "Callable[[int, int, str], None]" not in src, \
        f"{rel_path} содержит устаревшую аннотацию Callable[[int, int, str], None]"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 12: _bulk_replace не должен сбрасывать изменения через self.load()
# ─────────────────────────────────────────────────────────────────────────────

def test_bulk_replace_does_not_call_load():
    """_bulk_replace не должен вызывать self.load() (это сбрасывает изменения)."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(
        r"def _bulk_replace\(self\):(.*?)(?=\n    def |\Z)",
        src,
        re.DOTALL,
    )
    assert m, "_bulk_replace не найден"
    body = m.group(1)
    # Проверяем только реальный код (без строк-комментариев)
    code_lines = [
        line for line in body.splitlines()
        if not line.strip().startswith("#")
    ]
    code_only = "\n".join(code_lines)
    assert "self.load()" not in code_only, \
        "_bulk_replace всё ещё вызывает self.load() в коде (не в комментарии)"
    assert "blockSignals(True)" in body, \
        "_bulk_replace должен использовать blockSignals для in-place обновления таблицы"


# ─────────────────────────────────────────────────────────────────────────────
# Новый баг (этот round): save() без изменений — не сохраняет всё
# ─────────────────────────────────────────────────────────────────────────────

def test_strings_viewer_save_reports_partial_failure_instead_of_silent_pass(tmp_path):
    """
    UI-аудит: save() писал файлы в цикле try/except Exception: pass — если
    ОДИН из нескольких изменённых файлов не удавалось записать (диск
    заполнен, нет прав, путь недоступен), ошибка проглатывалась молча, а
    пользователь видел "Сохранено файлов: N" без единого намёка, что часть
    его РУЧНЫХ правок не долетела до диска. Хуже того, self._dirty_files
    очищался БЕЗУСЛОВНО — кнопка "Сохранить правки" переставала показывать
    несохранённые изменения, хотя они фактически были потеряны.

    Теперь: показывается предупреждение с именами failed-файлов, а сами
    они остаются помеченными dirty (следующее "Сохранить правки" подхватит
    их снова), вместо того чтобы тихо считаться сохранёнными.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem
    from PyQt6.QtCore import Qt
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sv = rd.StringsViewer()
    sv._output_dir = str(tmp_path)
    sv._file_map = {
        "Good.json": [{"original": "こんにちは", "translated": "Привет",
                       "context": "", "path": "[0]"}],
        "Bad.json":  [{"original": "さようなら", "translated": "Пока",
                       "context": "", "path": "[0]"}],
    }
    sv.table.setRowCount(2)
    rows = [(0, "Good.json", "こんにちは", "Привет"),
            (1, "Bad.json",  "さようなら", "Пока")]
    for row, fname, orig, trans in rows:
        file_item = QTableWidgetItem(fname)
        file_item.setData(Qt.ItemDataRole.UserRole, "[0]")
        sv.table.setItem(row, 0, file_item)
        sv.table.setItem(row, 2, QTableWidgetItem(orig))
        sv.table.setItem(row, 3, QTableWidgetItem(trans))
    sv._all_rows = [(0, "Good.json"), (1, "Bad.json")]
    sv._dirty_files = {"Good.json", "Bad.json"}

    warnings_shown = []
    QMessageBox.warning = staticmethod(
        lambda *a, **kw: warnings_shown.append(a[2] if len(a) > 2 else "")
    )

    original_write = rd.atomic_write_json
    def flaky_write(path, data):
        if "Bad" in str(path):
            raise OSError("Диск заполнен (симуляция для теста)")
        return original_write(path, data)
    rd.atomic_write_json = flaky_write
    try:
        sv.save()
    finally:
        rd.atomic_write_json = original_write

    assert warnings_shown, "БАГ: ошибка записи файла не показана пользователю"
    assert "Bad.json" in warnings_shown[0], "Предупреждение должно называть проблемный файл"
    assert "Good.json" not in sv._dirty_files, (
        "БАГ: успешно сохранённый файл не должен оставаться dirty"
    )
    assert "Bad.json" in sv._dirty_files, (
        "БАГ: файл, который не удалось сохранить, должен остаться dirty — "
        "иначе повторное 'Сохранить правки' молча его пропустит"
    )
    assert (tmp_path / "extracted" / "Good_translated.json").exists(), (
        "Good.json должен был успешно сохраниться несмотря на провал Bad.json"
    )
    """StringsViewer.save() при пустом _dirty_files не должен перезаписывать все файлы."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    # Находим StringsViewer.save() — ищем вторую def save() после class StringsViewer
    class_line = next(
        i for i, l in enumerate(lines) if "class StringsViewer" in l
    )
    save_line = next(
        i for i, l in enumerate(lines)
        if i > class_line and re.match(r"    def save\(self\):", l)
    )
    # Берём тело метода (до следующего def на том же уровне)
    body_lines = []
    for l in lines[save_line + 1:]:
        if re.match(r"    def \w", l):
            break
        body_lines.append(l)
    body = "\n".join(body_lines)

    assert "else set(self._file_map.keys())" not in body, \
        "StringsViewer.save() всё ещё использует save-all fallback при пустом _dirty_files"
    assert "not self._dirty_files" in body or "Нет изменений" in body, \
        "StringsViewer.save() должен иметь ранний выход когда _dirty_files пуст"


# ─────────────────────────────────────────────────────────────────────────────
# Новый баг (этот round): _parse_numbered — поддержка формата "N. text"
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_numbered_dot_format():
    """_parse_numbered должен распознавать '1. text' формат."""
    from ai_translator import _parse_numbered
    raw = "1. Привет\n2. Мир"
    result = _parse_numbered(raw, 2)
    assert result[0] == "Привет", f"Expected 'Привет', got {result[0]!r}"
    assert result[1] == "Мир",   f"Expected 'Мир', got {result[1]!r}"


def test_parse_numbered_paren_format():
    """_parse_numbered должен распознавать '1) text' формат."""
    from ai_translator import _parse_numbered
    raw = "1) Hello\n2) World"
    result = _parse_numbered(raw, 2)
    assert result[0] == "Hello", f"Expected 'Hello', got {result[0]!r}"
    assert result[1] == "World", f"Expected 'World', got {result[1]!r}"


def test_parse_numbered_dot_format_multiline():
    """_parse_numbered: '1. ' формат с многострочным переводом."""
    from ai_translator import _parse_numbered
    raw = "1. Первая строка\nпродолжение\n2. Вторая"
    result = _parse_numbered(raw, 2)
    assert "Первая строка" in result[0], f"Первая строка не найдена: {result[0]!r}"
    assert result[1] == "Вторая", f"Expected 'Вторая', got {result[1]!r}"


def test_parse_numbered_dot_format_does_not_strip_content_numbers():
    """Число в НАЧАЛЕ перевода не должно срезаться если после него нет '. ' или ') '."""
    from ai_translator import _parse_numbered
    # Если AI вернул [1] формат — числа в переводе не трогаются
    raw = "[1] 123 это нормальный текст\n[2] 456 тоже"
    result = _parse_numbered(raw, 2)
    assert result[0] == "123 это нормальный текст", f"Got {result[0]!r}"
    assert result[1] == "456 тоже", f"Got {result[1]!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Roundtrip: убеждаемся что Unity restore fix работает на реальных данных
# ─────────────────────────────────────────────────────────────────────────────

def test_unity_backup_restore_uses_game_path_not_data_dir(tmp_path):
    """
    Восстановление _backup_unity_original копирует файлы относительно game_path
    (как Godot/Wolf), а не data/ (как MV/MZ). Раньше тест «эмулировал» логику внутри
    себя и реальный код не вызывал — теперь вызывает engine.restore.
    """
    from engine.restore import restore_game_backups

    game_dir = tmp_path / "game"
    sub = game_dir / "_backup_unity_original" / "GameName_Data" / "StreamingAssets"
    sub.mkdir(parents=True)
    original_content = b"original unity asset"
    (sub / "translations.json").write_bytes(original_content)
    target_dir = game_dir / "GameName_Data" / "StreamingAssets"
    target_dir.mkdir(parents=True)
    (target_dir / "translations.json").write_bytes(b"injected content")

    result = restore_game_backups(game_dir)

    assert result.restored == 1, f"Должен был восстановлен 1 файл, получено {result.restored}"
    restored = (target_dir / "translations.json").read_bytes()
    assert restored == original_content, f"Unity backup не восстановлен корректно: {restored!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Новые баги (этот round)
# ─────────────────────────────────────────────────────────────────────────────

def test_godot_tscn_inject_does_not_interpret_backslashes_as_backreferences():
    """
    БАГ: изначальная реализация делала _TSCN_TEXT_RE.sub(r'\\1\\2\\3' + trans + ...)
    строкой-заменой — re.sub интерпретирует \\1, \\g<name> и т.п. внутри
    строки-замены как обратные ссылки на группы, что портило перевод, если
    он сам содержал обратный слэш с цифрой/именем после него. Раньше это
    проверялось только по наличию "lambda m" в исходнике (implementation
    detail, не поведение) — здесь фактически прогоняем inject_godot и
    проверяем итоговый файл.
    """
    from engine.godot_injector import inject_godot

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "project.godot").write_text("config_version=5", encoding="utf-8")
        scene = tmp / "scene.tscn"
        scene.write_text('[node name="Label"]\ntext = "こんにちは"\n', encoding="utf-8")
        out_dir = tmp / "out"
        (out_dir / "extracted").mkdir(parents=True)

        translated = "Привет \\1 \\g<name> мир"  # содержит "\1" и "\g<name>"
        entries = [{
            "file": "scene.tscn.json", "_source_path": "scene.tscn", "path": "line:1",
            "original": "こんにちは", "translated": translated,
            "context": "godot_text", "game_type": "godot",
        }]
        (out_dir / "extracted" / "scene_translated.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        result = inject_godot(tmp, out_dir, log_callback=lambda m: None)
        assert result["total_injected"] == 1
        final = scene.read_text(encoding="utf-8")
        assert 'text = "Привет \\\\1 \\\\g<name> мир"' in final, (
            f"БАГ: обратный слэш в переводе интерпретирован как backreference: {final!r}"
        )


def test_godot_dialogic_dialogue_file_can_actually_be_injected(tmp_path):
    """
    BUG (P0-3, внешний аудит, подтверждено эмпирически полным циклом
    Extract->Inject): .dialogue (плагин Dialogic) извлекается через ТОТ ЖЕ
    path-префикс "line:N", что и .tscn/.tres, но синтаксис строки
    совершенно другой ("Character: текст" / "- вариант" / голый нарратив,
    а не "key = \"value\""). Единственная ветка обработки "line:" требовала
    совпадения с _TSCN_TEXT_RE, которое для .dialogue строк никогда не
    совпадает — 100% записей из .dialogue гарантированно проваливались
    (file_err), файл оставался полностью непереведённым, без единой
    подсказки о причине. Экстрактор при этом извлекал контент отлично
    (включая speaker context "say:Taro") — только injector не умел
    записать обратно.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.godot_extractor import extract_godot
    from engine.godot_injector import inject_godot

    game_dir = tmp_path / "game"
    (game_dir / "dialogue").mkdir(parents=True)
    (game_dir / "project.godot").write_text("config_version=5", encoding="utf-8")
    dlg = game_dir / "dialogue" / "chapter1.dialogue"
    dlg.write_text("Taro: こんにちは、旅人よ\n- 話しかける\n遠くで鳥の声がする\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    extract_godot(game_dir, out_dir, log_callback=lambda m: None)
    ext_file = out_dir / "extracted" / "dialogue__chapter1.dialogue.json"
    assert ext_file.exists(), "экстракция .dialogue должна была создать JSON"
    entries = json.loads(ext_file.read_text(encoding="utf-8"))
    assert len(entries) == 3
    assert entries[0]["context"] == "say:Taro", "speaker-контекст должен извлекаться"

    tr_entries = [dict(e, translated=f"ПЕРЕВОД: {e['original']}") for e in entries]
    (out_dir / "extracted" / "dialogue__chapter1.dialogue_translated.json").write_text(
        json.dumps(tr_entries, ensure_ascii=False), encoding="utf-8"
    )

    result = inject_godot(game_dir, out_dir, log_callback=lambda m: None, backup=False)
    assert result["total_injected"] == 3, f"БАГ: не все записи вставлены: {result}"
    assert result["total_errors"] == 0, f"БАГ: инъекция .dialogue проваливается: {result}"

    final = dlg.read_text(encoding="utf-8")
    assert "Taro: ПЕРЕВОД: こんにちは、旅人よ" in final, "speaker-префикс должен сохраниться"
    assert "- ПЕРЕВОД: 話しかける" in final, "префикс варианта выбора должен сохраниться"
    assert "ПЕРЕВОД: 遠くで鳥の声がする" in final, "нарратив должен перевестись"


def test_godot_regex_tr_multiple_occurrences_get_independent_translations():
    """
    BUG (внешний аудит): extractor записывает точный character-offset
    вхождения tr("...") (regex_tr:{m.start()}), но injector его игнорировал
    и делал text.replace(target, replacement) по ВСЕМУ файлу — если один и
    тот же tr("X") встречается в файле несколько раз (например, одна и та
    же UI-строка используется в разных местах с разными по смыслу
    переводами), первая замена меняла ВСЕ вхождения разом, и последующие
    записи не находили свою цель.

    Плюс отдельная, более тонкая проблема при исправлении первой: если
    просто использовать offset для точечной замены БЕЗ учёта того, что
    перевод обычно длиннее оригинала, offset каждой СЛЕДУЮЩЕЙ записи в том
    же файле съезжает после уже применённой замены (тот же класс, что уже
    был правильно решён в wolf_injector.py — замены нужно применять от
    конца файла к началу).
    """
    from engine.godot_injector import inject_godot

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "project.godot").write_text("config_version=5", encoding="utf-8")
        src = tmp / "script.gd"
        text = 'a = tr("Fire")\nb = tr("Fire")\nc = tr("Fire")\n'
        src.write_text(text, encoding="utf-8")
        offsets = []
        pos = 0
        while True:
            i = text.find('tr("Fire")', pos)
            if i == -1:
                break
            offsets.append(i)
            pos = i + 1

        out_dir = tmp / "out"
        (out_dir / "extracted").mkdir(parents=True)
        translations = ["Очень длинный перевод номер один", "Второй", "Три"]
        entries = [
            {"file": "script.gd", "_source_path": "script.gd", "game_type": "godot",
             "path": f"regex_tr:{off}", "original": "Fire", "translated": tr}
            for off, tr in zip(offsets, translations)
        ]
        (out_dir / "extracted" / "script_translated.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        logs = []
        result = inject_godot(tmp, out_dir, log_callback=logs.append, backup=False)
        assert result["total_injected"] == 3
        assert result["total_errors"] == 0
        final = src.read_text(encoding="utf-8")
        for tr in translations:
            assert tr in final, f"БАГ: перевод {tr!r} не применился независимо: {final!r}"
        assert not any("offset не совпал" in m for m in logs), (
            f"БАГ: offset съехал из-за предыдущей замены другой длины: {logs}"
        )


def test_godot_tscn_inject_skips_when_line_changed_since_extract():
    """
    БАГ: замена в godot_injector была чисто позиционной (по номеру строки),
    без проверки, что текст на этой строке всё ещё совпадает с original —
    если .tscn/.tres поменялся между Extract и Inject, перевод СТАРОГО
    текста молча записывался поверх НОВОГО содержимого.
    """
    from engine.godot_injector import inject_godot

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "project.godot").write_text("config_version=5", encoding="utf-8")
        scene = tmp / "scene.tscn"
        # Строка 1 на момент Extract была "こんにちは", но разработчик её
        # поменял на "さようなら" ДО того, как перевод был вставлен.
        scene.write_text('[node name="Label"]\ntext = "さようなら"\n', encoding="utf-8")
        out_dir = tmp / "out"
        (out_dir / "extracted").mkdir(parents=True)

        entries = [{
            "file": "scene.tscn.json", "_source_path": "scene.tscn", "path": "line:1",
            "original": "こんにちは", "translated": "Привет",
            "context": "godot_text", "game_type": "godot",
        }]
        (out_dir / "extracted" / "scene_translated.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        result = inject_godot(tmp, out_dir, log_callback=lambda m: None)
        assert result["total_injected"] == 0, "БАГ: перевод устаревшего оригинала применился"
        assert result["total_errors"] == 1
        final = scene.read_text(encoding="utf-8")
        assert "さようなら" in final, f"БАГ: текущий текст был затёрт переводом устаревшего original: {final!r}"


def test_godot_tscn_re_at_module_level():
    """_TSCN_TEXT_RE должен быть на уровне модуля, а не внутри inject_godot."""
    src = (Path(__file__).parent.parent / "engine" / "godot_injector.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    # Должен быть определён ДО первой функции
    tscn_line = next((i for i, l in enumerate(lines) if "_TSCN_TEXT_RE" in l and "re.compile" in l), None)
    inject_line = next((i for i, l in enumerate(lines) if "def inject_godot" in l), None)
    assert tscn_line is not None, "_TSCN_TEXT_RE не найден"
    assert inject_line is not None, "inject_godot не найдена"
    assert tscn_line < inject_line, \
        f"_TSCN_TEXT_RE (строка {tscn_line+1}) должен быть до inject_godot (строка {inject_line+1})"




def test_safe_name_truncation_under_80_chars():
    """_safe_name не должен создавать имена длиннее 80 символов."""
    # Проверяем формулы в исходниках напрямую (import невозможен из-за relative imports)
    for rel, label in [
        ("engine/godot_extractor.py", "godot"),
        ("engine/unity_extractor.py", "unity"),
    ]:
        src = (Path(__file__).parent.parent / rel).read_text(encoding="utf-8")
        # Формула: N + 2 + 8 + 2 + 30 <= 80  =>  N <= 38.  Принято N=36.
        assert "[:36]" in src, \
            f"{label} _safe_name должен использовать [:36] (36+2+8+2+30=78 chars)"
        assert "[:40]" not in src or "name[:40]" not in src, \
            f"{label} _safe_name всё ещё использует [:40] (40+2+8+2+30=82>80)"


def test_godot_idempotency_restore_uses_rglob():
    """inject_godot idempotency restore должен использовать rglob."""
    src = (Path(__file__).parent.parent / "engine" / "godot_injector.py").read_text(encoding="utf-8")
    assert "_godot_backup.rglob(" in src, \
        "godot_injector restore должен использовать _godot_backup.rglob"
    # Проверяем только строки кода (не комментарии) в блоке restore
    marker_idx = src.find("_godot_marker.exists()")
    backup_idx  = src.find("if backup:")
    assert marker_idx != -1 and backup_idx != -1
    restore_block = src[marker_idx:backup_idx]
    code_lines = [l for l in restore_block.splitlines() if not l.strip().startswith("#")]
    assert "for src_path in by_file" not in "\n".join(code_lines), \
        "restore итерирует только by_file (не весь backup)"


def test_vxace_restore_uses_iterdir_not_by_file():
    """vxace_injector idempotency restore должен итерировать весь бэкап."""
    src = (Path(__file__).parent.parent / "engine" / "vxace_injector.py").read_text(encoding="utf-8")
    assert ".iterdir()" in src, "VXAce restore должен использовать .iterdir()"
    marker_idx  = src.find("_vxace_marker.exists()")
    backup_idx  = src.find("    if backup:")
    assert marker_idx != -1 and backup_idx != -1
    restore_block = src[marker_idx:backup_idx]
    code_lines = [l for l in restore_block.splitlines() if not l.strip().startswith("#")]
    assert "for fname in by_file" not in "\n".join(code_lines), \
        "VXAce restore всё ещё итерирует only by_file (не весь backup)"


def test_wolf_encode_returns_tuple_and_uses_cp932_for_cyrillic():
    """
    _encode_wolf_str должен возвращать (bytes, enc).
    CP932 (Windows-31J) включает кириллицу через NEC-расширения (ряды 0x83-0x86),
    поэтому кириллица корректно кодируется в CP932 без fallback на UTF-8.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "wolf_injector",
        Path(__file__).parent.parent / "engine" / "wolf_injector.py",
    )
    wolf_inj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wolf_inj)

    # Функция должна возвращать кортеж (bytes, str)
    result = wolf_inj._encode_wolf_str("Hello", "cp932")
    assert isinstance(result, tuple) and len(result) == 2, \
        f"_encode_wolf_str должна вернуть (bytes, str), получено {type(result)}"
    raw, enc = result
    assert isinstance(raw, bytes) and isinstance(enc, str)

    # Кириллица кодируется через CP932 NEC-расширения — round-trip корректен
    raw_cyr, enc_cyr = wolf_inj._encode_wolf_str("Привет", "cp932")
    assert isinstance(raw_cyr, bytes) and len(raw_cyr) > 0
    decoded_back = raw_cyr.decode(enc_cyr, errors="replace")
    assert decoded_back == "Привет", \
        f"Round-trip сломан для кириллицы через {enc_cyr}: {decoded_back!r}"


# ═══════════════════════════════════════════════════════════════════════════
# Новые возможности (этот round): Global TM, Re-translate, Cost confirmation
# ═══════════════════════════════════════════════════════════════════════════

def test_global_tm_module_exists_and_importable():
    """global_tm.py должен существовать и импортироваться."""
    import importlib.util
    path = Path(__file__).parent.parent / "global_tm.py"
    assert path.exists(), "global_tm.py не найден"
    spec = importlib.util.spec_from_file_location("global_tm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "GlobalTM")
    assert hasattr(mod, "_qualifies")


def test_global_tm_excludes_coded_strings():
    """Строки с RPG escape-кодами не должны попадать в глобальный TM."""
    import importlib.util
    path = Path(__file__).parent.parent / "global_tm.py"
    spec = importlib.util.spec_from_file_location("global_tm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert not mod._qualifies("§0§勇者§1§"), "Строка с §N§ не должна квалифицироваться"
    assert not mod._qualifies("\\C[2]text\\C[0]"), "Строка с \\C[] не должна квалифицироваться"
    assert mod._qualifies("Обычный диалог без кодов"), "Чистая строка должна квалифицироваться"
    assert not mod._qualifies("ab"), "Слишком короткая строка не должна квалифицироваться"


def test_global_tm_get_set_roundtrip(tmp_path):
    """Базовый round-trip: set → get должен вернуть то же значение."""
    import importlib.util
    path = Path(__file__).parent.parent / "global_tm.py"
    spec = importlib.util.spec_from_file_location("global_tm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.GlobalTM.DB_PATH = tmp_path / "test_tm.db"
    with mod.GlobalTM(target_lang="Russian") as tm:
        assert tm.get("a fresh phrase never seen") is None
        tm.set("a fresh phrase never seen", "свежая фраза никогда не виденная")
        tm.flush()
        assert tm.get("a fresh phrase never seen") == "свежая фраза никогда не виденная"


def test_global_tm_bulk_lookup(tmp_path):
    """bulk_lookup должен находить несколько строк за один вызов."""
    import importlib.util
    path = Path(__file__).parent.parent / "global_tm.py"
    spec = importlib.util.spec_from_file_location("global_tm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.GlobalTM.DB_PATH = tmp_path / "test_tm2.db"
    with mod.GlobalTM(target_lang="Russian") as tm:
        tm.set("first test phrase", "первая тестовая фраза")
        tm.set("second test phrase", "вторая тестовая фраза")
        tm.flush()
        result = tm.bulk_lookup(["first test phrase", "second test phrase", "missing phrase"])
        assert result == {
            "first test phrase": "первая тестовая фраза",
            "second test phrase": "вторая тестовая фраза",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 9): два независимых бага вокруг статистики GlobalTM.
# ─────────────────────────────────────────────────────────────────────────────

def test_global_tm_bulk_lookup_increments_hit_count(tmp_path):
    """
    БАГ: get() инкрементирует hit_count на каждый хит, а bulk_lookup() —
    основной, горячий путь чтения TM (используется в ai_translator.py) —
    не инкрементировал его вообще. "top_entries по популярности" в
    get_stats() почти никогда не отражал реальное использование.
    """
    import importlib.util
    path = Path(__file__).parent.parent / "global_tm.py"
    spec = importlib.util.spec_from_file_location("global_tm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.GlobalTM.DB_PATH = tmp_path / "test_tm_hits.db"
    with mod.GlobalTM(target_lang="Russian") as tm:
        tm.set("test phrase", "тестовая фраза")
        tm.flush()

        row_before = tm._conn.execute(
            "SELECT hit_count FROM tm WHERE source=?", ("test phrase",)
        ).fetchone()
        assert row_before[0] == 1  # set() создаёт запись с hit_count=1

        tm.bulk_lookup(["test phrase"])
        tm.bulk_lookup(["test phrase"])  # два обращения

        row_after = tm._conn.execute(
            "SELECT hit_count FROM tm WHERE source=?", ("test phrase",)
        ).fetchone()
        assert row_after[0] == 3, (
            f"БАГ: hit_count не вырос после bulk_lookup — было 1, ожидали 3, получили {row_after[0]}"
        )


def test_tm_manage_dialog_shows_correct_total(tmp_path):
    """
    БАГ: _tm_manage_dialog() читал s.get("total_entries", 0), но
    get_stats() возвращает ключ "total" — .get() с дефолтом молча
    проглатывал несовпадение, диалог ВСЕГДА показывал "Записей: 0"
    независимо от реального содержимого базы. Проверяем не исходник,
    а реально отображаемый текст: создаём StatsPanel, наполняем TM,
    вызываем диалог, перехватываем модальный QMessageBox.exec().
    """
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd
    import global_tm

    global_tm.GlobalTM.DB_PATH = tmp_path / "test_tm_dialog.db"
    with global_tm.GlobalTM(target_lang="Russian") as tm:
        tm.set("hello world test", "привет мир тест")
        tm.flush()

    panel = rd.StatsPanel()
    captured = []

    def fake_exec(self):
        captured.append(self)
        return 0

    with patch.object(QMessageBox, "exec", fake_exec):
        panel._tm_manage_dialog()

    assert captured, "Диалог не был показан"
    assert "Записей:</b> 1" in captured[0].text(), (
        f"БАГ: диалог не показывает реальное количество записей: {captured[0].text()!r}"
    )


def test_translator_config_has_use_global_tm_field():
    """TranslatorConfig должен иметь поле use_global_tm (по умолчанию True)."""
    from ai_translator import TranslatorConfig
    cfg = TranslatorConfig(provider="openai", api_key="x")
    assert hasattr(cfg, "use_global_tm")
    assert cfg.use_global_tm is True


def test_translate_texts_integrates_global_tm():
    """translate_texts должен ссылаться на global_tm при use_global_tm=True."""
    src = (Path(__file__).parent.parent / "ai_translator.py").read_text(encoding="utf-8")
    assert "from global_tm import GlobalTM" in src, \
        "translate_texts должен импортировать GlobalTM"
    assert "config.use_global_tm" in src, \
        "translate_texts должен проверять config.use_global_tm"
    assert "global_tm.bulk_lookup" in src, \
        "translate_texts должен использовать bulk_lookup для эффективности"
    assert "global_tm.close()" in src, \
        "translate_texts должен закрывать соединение с TM"


def test_pipeline_translate_threads_use_global_tm():
    """Pipeline.translate должен принимать и передавать use_global_tm."""
    src = (Path(__file__).parent.parent / "pipeline.py").read_text(encoding="utf-8")
    assert "use_global_tm: bool = True" in src, \
        "Pipeline.translate должен иметь параметр use_global_tm"
    assert "use_global_tm=use_global_tm," in src, \
        "Pipeline.translate должен передавать use_global_tm в TranslatorConfig"


def test_settings_panel_has_global_tm_checkbox():
    """SettingsPanel должен иметь чекбокс use_global_tm, подключённый к dirty-tracking."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "self.use_global_tm = QCheckBox(" in src
    assert '"use_global_tm":      self.use_global_tm.isChecked(),' in src
    assert "self.use_global_tm.stateChanged.connect(self._on_setting_changed)" in src


def test_strings_viewer_has_retranslate_context_menu():
    """StringsViewer должен иметь контекстное меню для re-translate выбранных строк."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "_show_context_menu" in src
    assert "_retranslate_rows" in src
    assert "CustomContextMenu" in src
    assert "set_settings_provider" in src


def test_retranslate_rows_uses_all_rows_for_fname_not_user_role():
    """
    _retranslate_rows должен брать fname из self._all_rows (как save()),
    а не из file_item.data(UserRole), которое хранит entry path, не filename.
    """
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _retranslate_rows\(self.*?\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m, "_retranslate_rows не найден"
    body = m.group(1)
    assert "self._all_rows[row]" in body, \
        "_retranslate_rows должен использовать self._all_rows[row] для получения fname"


def test_retranslate_uses_use_context_false_and_global_tm_false():
    """Re-translate одной строки должен форсировать свежий перевод (без TM, без sliding context)."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _retranslate_rows\(self.*?\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert "use_context=False" in body
    assert "use_global_tm=False" in body


def test_retranslate_runs_off_ui_thread_not_blocking():
    """
    КРИТИЧЕСКИЙ регрессионный тест: _retranslate_rows раньше вызывал
    translate_texts() СИНХРОННО на UI-потоке — это замораживало весь интерфейс
    на время сетевых вызовов (включая retry-паузы при rate limit, до 300с).
    QApplication.processEvents() перед вызовом не спасает от последующего
    блокирующего вызова — он обрабатывает очередь событий один раз, а не
    постоянно во время долгой операции.

    Реальный функциональный тест (не string-matching): запускает
    _RetranslateThread с фейковой translate_texts, которая спит 0.3с, и
    проверяет что она выполняется на ДРУГОМ потоке, а обработчик результата
    получает управление обратно на главном потоке (корректный Qt marshalling).
    """
    import threading, time as _time
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QEventLoop, QTimer
    import rpgmtl_desktop as rd
    import ai_translator

    app = QApplication.instance() or QApplication([])
    main_thread_id = threading.get_ident()
    captured = {}

    original_fn = ai_translator.translate_texts

    def fake_translate_texts(texts, cfg, log_callback=None):
        captured["worker_thread_id"] = threading.get_ident()
        _time.sleep(0.2)
        class FakeResult:
            results = [
                type("R", (), {"success": True, "translated": f"T:{t}"})()
                for t in texts
            ]
        return FakeResult()

    ai_translator.translate_texts = fake_translate_texts
    try:
        th = rd._RetranslateThread(["a", "b"], cfg=None)
        loop = QEventLoop()
        holder = {}

        def on_finished(result, error):
            holder["result"] = result
            holder["error"] = error
            holder["handler_thread_id"] = threading.get_ident()
            loop.quit()

        th.finished_result.connect(on_finished)
        th.start()
        QTimer.singleShot(5000, loop.quit)  # safety timeout
        loop.exec()

        assert "worker_thread_id" in captured, "translate_texts никогда не вызвался"
        assert captured["worker_thread_id"] != main_thread_id, \
            "translate_texts выполнился на ГЛАВНОМ потоке — UI будет заморожен!"
        assert holder.get("handler_thread_id") == main_thread_id, \
            "Обработчик результата должен вызываться на главном потоке (Qt signal marshalling)"
        assert holder["error"] == ""
        assert holder["result"] is not None
        assert len(holder["result"].results) == 2
        assert holder["result"].results[0].translated == "T:a"
    finally:
        ai_translator.translate_texts = original_fn


def test_retranslate_thread_propagates_exceptions_via_signal():
    """_RetranslateThread должен ловить исключения и передавать их через сигнал, не падать молча."""
    import threading
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QEventLoop, QTimer
    import rpgmtl_desktop as rd
    import ai_translator

    app = QApplication.instance() or QApplication([])
    original_fn = ai_translator.translate_texts

    def failing_translate_texts(texts, cfg, log_callback=None):
        raise RuntimeError("simulated API failure")

    ai_translator.translate_texts = failing_translate_texts
    try:
        th = rd._RetranslateThread(["a"], cfg=None)
        loop = QEventLoop()
        holder = {}

        def on_finished(result, error):
            holder["result"] = result
            holder["error"] = error
            loop.quit()

        th.finished_result.connect(on_finished)
        th.start()
        QTimer.singleShot(3000, loop.quit)
        loop.exec()

        assert holder.get("result") is None
        assert "simulated API failure" in holder.get("error", "")
    finally:
        ai_translator.translate_texts = original_fn


def test_retranslate_rows_guards_against_reentrancy():
    """_retranslate_rows должен проверять self._retranslate_thread перед стартом нового перевода."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _retranslate_rows\(self.*?\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert '_retranslate_thread", None) is not None' in body, \
        "_retranslate_rows должен проверять что предыдущий перевод не выполняется"


def test_retranslate_rows_also_guards_against_concurrent_polish():
    """
    BUG FIX (UI-аудит): _polish_rows проверяет ОБА потока — свой
    (_polish_thread) и _retranslate_thread — прежде чем стартовать. Но
    _retranslate_rows проверял ТОЛЬКО свой поток, не _polish_thread —
    асимметричная защита. Если пользователь запускал AI Polish, а ПОКА он
    выполнялся, выбирал (возможно пересекающиеся) строки и жал "Перевести
    через AI" — стартовал ВТОРОЙ параллельный фоновый AI-вызов поверх
    первого. Оба потока пишут в self.table и self._file_map независимо;
    результат того, что завершится позже, молча перезаписывал бы результат
    другого без единого предупреждения.
    """
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _retranslate_rows\(self.*?\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert '_polish_thread", None) is not None' in body, (
        "БАГ: _retranslate_rows должен проверять _polish_thread так же, как "
        "_polish_rows уже проверяет _retranslate_thread — иначе защита от "
        "одновременного запуска асимметрична"
    )


def test_translation_confirmation_dialog_exists():
    """Запуск перевода должен показывать диалог подтверждения с оценкой стоимости."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "_confirm_translation_estimate" in src, \
        "_confirm_translation_estimate не найден"
    assert "_show_cost_estimate" not in src, \
        "старый метод _show_cost_estimate должен быть полностью заменён"
    # Должен реально использоваться как gate перед стартом перевода
    m = re.search(r"def _run_translate\(self\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m, "_run_translate не найден"
    body = m.group(1)
    assert "if not self._confirm_translation_estimate(output, s):" in body, \
        "_run_translate должен гейтить запуск через _confirm_translation_estimate"
    assert "return" in body.split("_confirm_translation_estimate(output, s):")[1][:30], \
        "При отказе пользователя _run_translate должен делать return"


def test_diagnostics_export_includes_global_tm_stats():
    """Экспорт диагностики должен включать статистику Global TM."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _export_diagnostics\(self\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m, "_export_diagnostics не найден"
    body = m.group(1)
    assert "from global_tm import GlobalTM" in body
    assert '"global_tm": tm_stats' in body


def test_export_diagnostics_redacts_api_key_leaked_through_log_text():
    """
    BUG: _export_diagnostics редактирует api_key в settings ("***"), но текст
    сессионного лога (full_log_widget) экспортировался как есть. Провайдер
    может вернуть ошибку авторизации, чей str(e) эхом содержит ключ (типичная
    картина для 401/invalid-key у части провайдеров, почти гарантированная
    для custom-эндпоинтов) — ai_translator._do_translate_batch логирует такие
    ошибки через `_log(f"...: {e}")` без какой-либо редакции. Файл при этом
    прямым текстом документирован как "без ключа" и предназначен для отправки
    наружу (сообщение в диалоге: "Отправьте этот файл для анализа").
    """
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    fake_key = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCDEF"
    win.settings_panel.api_key.setText(fake_key)
    win.settings_panel.provider.setCurrentText("openai")
    win.full_log_widget.setPlainText(
        "[Translate] Начало перевода...\n"
        f"  ✗ Попытка 1/3: Error code: 401 - Incorrect API key provided: {fake_key}.\n"
        "  ↻ Повтор через 2.0с...\n"
    )

    with tempfile.TemporaryDirectory() as home_dir:
        QMessageBox.information = staticmethod(lambda *a, **kw: None)
        with patch.object(Path, "home", lambda: Path(home_dir)):
            win._export_diagnostics()

        out_files = list(Path(home_dir).glob("rpgmtl_diagnostics_*.json"))
        assert out_files, "Файл диагностики не создан"
        data = json.loads(out_files[0].read_text(encoding="utf-8"))

        assert data["settings"]["api_key"] == "***"
        assert fake_key not in data["log"], (
            f"БАГ: ключ утёк через поле log: {data['log']!r}"
        )
        assert not any(fake_key in line for line in data["errors"]), (
            f"БАГ: ключ утёк через поле errors: {data['errors']!r}"
        )
        assert "[REDACTED]" in data["log"], (
            "Строка об ошибке должна остаться в логе (полезна для диагностики), "
            "просто без самого ключа"
        )


def test_qa_flags_translated_entry_not_matching_source_extract_record():
    """
    BUG (P0-4, внешний аудит): injector доверяет path/original ИЗ САМОГО
    translated.json, сверяя их только с ТЕКУЩИМ содержимым игрового файла
    (drift-защита) — но не с тем, что РЕАЛЬНО извлёк Extract для этого path.
    Если path+original в translated.json окажутся рассинхронизированы с
    исходной Extract-записью (например, из-за бага в пайплайне или ручной
    правки файла вне UI), у Inject нет способа это заметить: он может
    записать перевод не в ту строку. Source-файл (не _translated) — это
    неизменяемая запись того, что реально извлёк Extract; сверяем с ней.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from qa_report import run_qa_report

    # Легитимный случай: source и translated согласованы -> ничего не флагуется.
    with tempfile.TemporaryDirectory() as tmp:
        ext_dir = Path(tmp)
        (ext_dir / "Map001.json").write_text(json.dumps([
            {"file": "Map001.json", "path": "[0].name", "original": "勇者", "translated": ""},
        ], ensure_ascii=False), encoding="utf-8")
        (ext_dir / "Map001_translated.json").write_text(json.dumps([
            {"file": "Map001.json", "path": "[0].name", "original": "勇者", "translated": "Герой"},
        ], ensure_ascii=False), encoding="utf-8")
        report = run_qa_report(ext_dir)
        assert report["critical_count"] == 0, (
            f"Ложное срабатывание на легитимных согласованных данных: {report['issues']}"
        )

    # Испорченный случай: original в translated.json не совпадает с тем, что
    # Extract реально записал для этого path -- и путь, которого Extract
    # вообще не извлекал.
    with tempfile.TemporaryDirectory() as tmp:
        ext_dir = Path(tmp)
        (ext_dir / "Map001.json").write_text(json.dumps([
            {"file": "Map001.json", "path": "[0].name", "original": "勇者", "translated": ""},
        ], ensure_ascii=False), encoding="utf-8")
        (ext_dir / "Map001_translated.json").write_text(json.dumps([
            {"file": "Map001.json", "path": "[0].name", "original": "oldA", "translated": "X"},
            {"file": "Map001.json", "path": "[9].name", "original": "currentB", "translated": "Y"},
        ], ensure_ascii=False), encoding="utf-8")
        report = run_qa_report(ext_dir)
        types = {i["type"] for i in report["issues"]}
        assert "entry_original_mismatch" in types, (
            f"БАГ: расхождение original с Extract-записью не поймано: {report['issues']}"
        )
        assert "entry_path_unknown" in types, (
            f"БАГ: path, которого не было в Extract, не поймано: {report['issues']}"
        )
        assert report["critical_count"] == 2

    # Source-файл отсутствует/нечитаем -> ничего не флагуется (не можем
    # проверить — лучше молчать, чем дать необоснованный critical).
    with tempfile.TemporaryDirectory() as tmp:
        ext_dir = Path(tmp)
        (ext_dir / "Map001_translated.json").write_text(json.dumps([
            {"file": "Map001.json", "path": "[0].name", "original": "勇者", "translated": "Герой"},
        ], ensure_ascii=False), encoding="utf-8")
        report = run_qa_report(ext_dir)
        assert report["critical_count"] == 0, (
            f"Не должно флаговаться, когда source-файл недоступен: {report['issues']}"
        )



def test_qa_flags_code_wrap_collapsed_even_with_same_code_order():
    """
    только список кодов и их взаимный порядок — "\\C[2]Герой\\C[0]" (обёртка
    вокруг слова) и "Герой\\C[2]\\C[0]" (оба кода пришиты в конец) дают
    ОДИНАКОВЫЙ список в одинаковом порядке, поэтому базовая проверка (и, до
    этого фикса, run_qa_report) считали такой перевод полностью валидным —
    хотя визуально форматирование (обёртка) пропало: коды съехались вплотную
    и ничего больше не оборачивают. Это правдоподобная ошибка ИИ-перевода,
    особенно при сильно другом порядке слов (JA -> RU).
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from qa_report import check_entry

    # Коды по-прежнему валидны для _validate_codes (тот же список, тот же
    # порядок) — но обёртка вокруг "Hello"/"Герой" пропала в переводе.
    collapsed = check_entry({
        "original": r"\C[2]Hello\C[0]", "translated": r"Hello\C[2]\C[0]",
    })
    assert any(i["type"] == "code_position" for i in collapsed), (
        f"БАГ: схлопнувшаяся обёртка кодов не поймана QA: {collapsed}"
    )
    assert all(i["severity"] == "warning" for i in collapsed if i["type"] == "code_position"), (
        "code_position должен быть warning, а не critical — это эвристика, не факт поломки"
    )

    # Контроль: нормальный перевод с другим порядком слов, но обёртка на месте.
    normal = check_entry({
        "original": r"\C[2]勇者\C[0]よ、旅に出るのだ",
        "translated": r"\C[2]Герой\C[0], пора отправляться в путь",
    })
    assert not any(i["type"] == "code_position" for i in normal), (
        f"Ложное срабатывание на нормальном переводе: {normal}"
    )

    # Контроль: коды уже были соседними в оригинале (не оборачивали ничего) —
    # остаться соседними в переводе не должно давать warning.
    already_adjacent = check_entry({
        "original": r"\C[1]\C[2]text", "translated": r"\C[1]\C[2]другой текст",
    })
    assert not any(i["type"] == "code_position" for i in already_adjacent), (
        f"Ложное срабатывание, когда коды и так были соседними: {already_adjacent}"
    )



def test_apply_warp_never_truncates_translated_text_content():
    """
    имел default max_lines=4, а Pipeline._apply_warp() вызывал его НЕ передавая
    max_lines явно — то есть каждый перевод, который после переноса по ширине
    не помещался в 4 строки, молча обрезался с "…", безвозвратно теряя
    содержимое прямо во время Inject. Для строки в 466 символов на выходе
    оставалось всего 108 (потеря ~77%). Это происходило для КАЖДОГО достаточно
    длинного перевода при apply_warp=True (значение по умолчанию в конфиге).
    Фикс: max_lines по умолчанию теперь None (не обрезать); _apply_warp
    логирует warning, если перенос вышел за DEFAULT_MAX_LINES строк, но
    не удаляет ни одного символа перевода.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pipeline import Pipeline
    from conftest import write_extract_artifacts

    long_translation = (
        "Герой отправился в долгое и трудное путешествие через тёмный лес, "
        "полный опасностей и неожиданных поворотов судьбы, чтобы найти "
        "древний артефакт, способный спасти его родную деревню от "
        "неминуемой гибели, но по пути ему предстоит столкнуться с "
        "множеством врагов и союзников, каждый из которых изменит его "
        "судьбу навсегда, а также встретить старого мудреца, который "
        "откроет ему правду о его собственном происхождении и "
        "предназначении в этом жестоком мире, полном тайн."
    )
    assert len(long_translation) > 400, "фикстура должна быть длинной, иначе тест ничего не проверяет"

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        output_dir = Path(tmp) / "out"
        (game_dir / "data").mkdir(parents=True)
        (game_dir / "data" / "System.json").write_text(
            json.dumps({"gameTitle": "テストゲーム"}), encoding="utf-8")
        (game_dir / "data" / "Map001.json").write_text("{}", encoding="utf-8")
        output_dir.mkdir()
        ext_dir = output_dir / "extracted"
        ext_dir.mkdir()

        (ext_dir / "Map001.json").write_text(json.dumps([
            {"file": "Map001.json", "path": "[0].text", "original": "とても長いセリフ",
             "translated": "", "context": "dialog_text"},
        ], ensure_ascii=False), encoding="utf-8")
        (ext_dir / "Map001_translated.json").write_text(json.dumps([
            {"file": "Map001.json", "path": "[0].text", "original": "とても長いセリフ",
             "translated": long_translation, "context": "dialog_text"},
        ], ensure_ascii=False), encoding="utf-8")

        write_extract_artifacts(output_dir, game_dir, "rpgmv",
                                 {"Map001.json": "data/Map001.json"})

        logs = []
        pipeline = Pipeline(str(game_dir), str(output_dir), log_callback=logs.append)
        pipeline.inject(apply_warp=True, max_line_width=400)

        result = json.loads((ext_dir / "Map001_translated.json").read_text(encoding="utf-8"))
        warped_text = result[0]["translated"]

        # Весь текст должен сохраниться (сравниваем без учёта пробелов/переносов,
        # т.к. перенос сам по себе меняет пробелы на \n в местах разрыва строк).
        original_words = "".join(long_translation.split())
        warped_words = "".join(warped_text.replace("\n", " ").split())
        assert warped_words == original_words, (
            f"БАГ: текст потерян при переносе. Было {len(original_words)} символов, "
            f"осталось {len(warped_words)}: {warped_text!r}"
        )
        assert not warped_text.rstrip().endswith("…"), (
            f"Текст не должен обрезаться многоточием: {warped_text!r}"
        )
        line_count = warped_text.count("\n") + 1
        assert line_count > 4, (
            "Фикстура должна была реально не поместиться в 4 строки, иначе "
            "тест не проверяет то, что должен"
        )
        assert any("вышли за" in m and "строки" in m for m in logs), (
            "Ожидался лог-warning о том, что перенос вышел за пределы "
            "стандартного окна сообщения — для ручной проверки"
        )



def test_close_event_warns_before_discarding_unsaved_strings_edits():
    """
    Inject), но никогда не проверял self.strings_viewer._dirty_files. Ручная
    правка перевода в таблице живёт только в QTableWidget + _dirty_files, пока
    пользователь явно не нажмёт Save (кнопку или Ctrl+S, см. _ctrl_s() в
    _setup_shortcuts). Settings безопасны — _save_current_config() сохраняет
    их безусловно в этом же closeEvent. Закрытие окна с несохранённой ручной
    правкой перевода полностью её удаляло без единого предупреждения.
    """
    import sys, json, tempfile
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from PyQt6.QtGui import QCloseEvent
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    def make_win_with_dirty_edit(tmp):
        win = rd.MainWindow()
        output_dir = Path(tmp) / "out"
        ext_dir = output_dir / "extracted"
        ext_dir.mkdir(parents=True)
        (ext_dir / "Map001.json").write_text(
            json.dumps([{"original": "こんにちは", "translated": ""}]), encoding="utf-8")
        (ext_dir / "Map001_translated.json").write_text(
            json.dumps([{"original": "こんにちは", "translated": "Привет"}]), encoding="utf-8")
        win.strings_viewer.set_output_dir(str(output_dir))
        win.strings_viewer.load()
        win.strings_viewer.table.item(0, 3).setText("Здравствуй (правка юзера)")
        return win, ext_dir

    # Cancel: window must stay open, on-disk file untouched.
    with tempfile.TemporaryDirectory() as tmp:
        win, ext_dir = make_win_with_dirty_edit(tmp)
        assert win.strings_viewer._dirty_files, "фикстура сломана: dirty_files пуст"
        QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Cancel)
        ev = QCloseEvent()
        win.closeEvent(ev)
        assert not ev.isAccepted(), "БАГ: Cancel должен держать окно открытым"
        on_disk = json.loads((ext_dir / "Map001_translated.json").read_text(encoding="utf-8"))
        assert on_disk[0]["translated"] == "Привет"

    # Save: window closes, edit persisted to disk.
    with tempfile.TemporaryDirectory() as tmp:
        win, ext_dir = make_win_with_dirty_edit(tmp)
        QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Save)
        ev = QCloseEvent()
        win.closeEvent(ev)
        assert ev.isAccepted()
        on_disk = json.loads((ext_dir / "Map001_translated.json").read_text(encoding="utf-8"))
        assert on_disk[0]["translated"] == "Здравствуй (правка юзера)", (
            "БАГ: выбор Save должен был сохранить правку на диск перед закрытием"
        )

    # Discard: window closes, edit explicitly NOT persisted (user's own choice).
    with tempfile.TemporaryDirectory() as tmp:
        win, ext_dir = make_win_with_dirty_edit(tmp)
        QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Discard)
        ev = QCloseEvent()
        win.closeEvent(ev)
        assert ev.isAccepted()
        on_disk = json.loads((ext_dir / "Map001_translated.json").read_text(encoding="utf-8"))
        assert on_disk[0]["translated"] == "Привет"

    # No unsaved edits at all: closes immediately, no interrupting dialog.
    with tempfile.TemporaryDirectory() as tmp:
        win = rd.MainWindow()
        calls = {"n": 0}
        def _fail_if_called(*a, **kw):
            calls["n"] += 1
            return QMessageBox.StandardButton.Cancel
        QMessageBox.question = staticmethod(_fail_if_called)
        ev = QCloseEvent()
        win.closeEvent(ev)
        assert ev.isAccepted()
        assert calls["n"] == 0, (
            "Диалог не должен показываться, если нет несохранённых правок в Строках"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Round 6: Retry-After, XLSX export, keyboard shortcuts, glossary suggestions
# ═══════════════════════════════════════════════════════════════════════════

def test_extract_retry_after_from_exception():
    """_extract_retry_after должен находить задержку в заголовках httpx и в тексте."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from ai_translator import _extract_retry_after

    # Из текста ошибки (Gemini/другие SDK без httpx.Response)
    class FakeErr(Exception):
        pass

    err_text = FakeErr("Resource exhausted. Please retry in 42.5s after your quota resets.")
    result = _extract_retry_after(err_text)
    assert result == 42.5, f"Expected 42.5, got {result}"

    # Другой формат текста
    err2 = FakeErr("Rate limit exceeded. Try again in 30 seconds.")
    result2 = _extract_retry_after(err2)
    assert result2 == 30.0, f"Expected 30.0, got {result2}"

    # Нет информации → None
    err3 = FakeErr("Some other error without wait time.")
    assert _extract_retry_after(err3) is None


def test_extract_retry_after_from_httpx_headers():
    """_extract_retry_after должен читать заголовок Retry-After из .response.headers."""
    from ai_translator import _extract_retry_after

    class FakeHeaders:
        def get(self, key):
            return "60" if key in ("retry-after", "Retry-After") else None

    class FakeResponse:
        headers = FakeHeaders()

    class FakeAPIError(Exception):
        response = FakeResponse()

    result = _extract_retry_after(FakeAPIError("429 Rate limit"))
    assert result == 60.0, f"Expected 60.0, got {result}"


def test_rate_limit_wait_respects_server_header():
    """При rate limit код должен использовать server-specified задержку, а не фиксированную."""
    src = (Path(__file__).parent.parent / "ai_translator.py").read_text(encoding="utf-8")
    assert "_extract_retry_after(e)" in src, \
        "_do_translate_batch должен вызывать _extract_retry_after при rate limit"
    assert "server_wait" in src, \
        "Переменная server_wait должна хранить server-specified задержку"
    assert "Retry-After" in src, \
        "Лог должен упоминать Retry-After когда сервер указал задержку"


def test_strings_viewer_has_xlsx_export():
    """StringsViewer должен иметь кнопку и метод экспорта в XLSX."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "btn_export_xlsx" in src
    assert "_export_xlsx" in src
    assert "openpyxl" in src, "XLSX export должен использовать openpyxl"
    assert "✏ Правка переводчика" in src, "Должна быть колонка для правок переводчика"
    assert "📖 Инструкция" in src, "Должен быть лист с инструкцией"


def test_xlsx_export_gracefully_handles_missing_openpyxl():
    """_export_xlsx должен показать понятное сообщение если openpyxl не установлен."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _export_xlsx\(self\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert "openpyxl не установлен" in body, \
        "Должно быть понятное сообщение об установке openpyxl"
    assert "pip install openpyxl" in body


def test_keyboard_shortcuts_method_exists_with_all_keys():
    """_setup_shortcuts должен регистрировать F5, F6, F7, Ctrl+S."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _setup_shortcuts\(self\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m, "_setup_shortcuts не найдена"
    body = m.group(1)
    for key in ("F5", "F6", "F7", "Ctrl+S"):
        assert key in body, f"Горячая клавиша {key!r} не найдена"


def test_shortcuts_call_correct_actions():
    """F5→_run_extract, F6→_run_translate, F7→_run_inject."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _setup_shortcuts\(self\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert "_run_extract" in body
    assert "_run_translate" in body
    assert "_run_inject" in body


def test_ctrl_z_is_not_registered_as_a_global_shortcut():
    """
    UI-аудит: докстринг _setup_shortcuts раньше документировал "Ctrl+Z —
    Восстановить из резервной копии", но такой шорткат никогда не был
    реально зарегистрирован (не было _make("Ctrl+Z", ...) — код и
    документация разошлись). Сознательно НЕ реализуем его: Ctrl+Z — это
    почти универсальный шорткат "отменить правку текста", а в приложении
    есть текстовые поля (термины глоссария, ячейки таблицы Строк, поля
    настроек); WindowShortcut на Ctrl+Z срабатывал бы даже с фокусом в
    таком поле, неожиданно всплывая поверх обычного редактирования текста
    диалогом о ДЕСТРУКТИВНОМ восстановлении бэкапа. Функция остаётся
    доступна через явную кнопку с диалогом подтверждения.
    """
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _setup_shortcuts\(self\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert '_make("Ctrl+Z"' not in body, (
        "Ctrl+Z не должен быть зарегистрирован как глобальный шорткат — "
        "конфликтует с 'отменить правку текста' в текстовых полях приложения"
    )


def test_glossary_suggestion_triggered_after_extract():
    """После Extract должна запускаться _suggest_glossary_terms."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _on_finished\(self.*?\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m, "_on_finished не найдена"
    body = m.group(1)
    assert "_suggest_glossary_terms" in body
    assert "QTimer.singleShot" in body, \
        "Вызов через singleShot чтобы не блокировать UI"


def test_suggest_glossary_terms_method_handles_empty_gracefully():
    """_suggest_glossary_terms не должна падать если нет extracted файлов."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _suggest_glossary_terms\(self.*?\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m, "_suggest_glossary_terms не найдена"
    body = m.group(1)
    assert "ext_dir.exists()" in body
    assert "not all_entries" in body, \
        "Проверка на пустой список должна быть по all_entries (list[dict]), не all_originals"
    assert "except Exception" in body


def test_suggest_glossary_terms_passes_dicts_not_strings_to_extract_game_terms(tmp_path):
    """
    КРИТИЧЕСКИЙ регрессионный тест: extract_game_terms() ожидает list[dict]
    с ключами "original"/"context" и делает entry.get(...) на каждом элементе.

    Баг: _suggest_glossary_terms раньше собирал list[str] (голые original-тексты)
    вместо list[dict] и передавал их напрямую в extract_game_terms(), которая
    вызывала .get() на строке → AttributeError на КАЖДОМ вызове. Исключение
    тихо гасилось внешним try/except в _suggest_glossary_terms, поэтому фича
    молча не работала никогда — ни один из предыдущих string-matching тестов
    этого не поймал, так как они не выполняли реальный код.

    Этот тест реально ЧИТАЕТ extracted-файлы и вызывает ту же функцию с теми же
    данными, что и настоящий UI-код, а не проверяет наличие строк в исходнике.
    """
    import json, sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.glossary import extract_game_terms

    ext_dir = tmp_path / "extracted"
    ext_dir.mkdir()
    (ext_dir / "Map001.json").write_text(json.dumps([
        {"original": "勇者", "translated": "", "context": "dialogue"},
        {"original": "勇者", "translated": "", "context": "dialogue"},
        {"original": "勇者", "translated": "", "context": "dialogue"},
        {"original": "こんにちは", "translated": "", "context": "dialogue"},
    ]), encoding="utf-8")

    # Точно воспроизводим текущую логику сбора данных из _suggest_glossary_terms
    all_entries: list[dict] = []
    for jf in ext_dir.glob("*.json"):
        if jf.name.endswith("_translated.json"):
            continue
        entries = json.loads(jf.read_text(encoding="utf-8"))
        all_entries.extend(entries)

    assert len(all_entries) == 4
    assert isinstance(all_entries[0], dict), \
        "Собранные записи должны быть dict, не str — иначе extract_game_terms упадёт"

    # Реальный вызов — если сигнатура данных неверна, здесь будет AttributeError
    suggestions = extract_game_terms(all_entries, max_terms=100)
    assert "勇者" in suggestions, f"Ожидали найти повторяющийся термин, получили {suggestions}"


def test_extract_game_terms_rejects_list_of_strings():
    """
    Санити-тест, документирующий КОНТРАКТ extract_game_terms: если передать
    list[str] вместо list[dict], должен быть AttributeError — это фиксирует
    ожидаемое поведение самой функции, чтобы будущие вызовы не повторяли баг.
    """
    from engine.glossary import extract_game_terms
    with pytest.raises(AttributeError):
        extract_game_terms(["勇者", "勇者", "勇者"], max_terms=10)


# ═══════════════════════════════════════════════════════════════════════════
# Round 7: glossary delete confirmation, stale-extract detection,
#          per-file stats, API key format hint, TM management dialog
# ═══════════════════════════════════════════════════════════════════════════

def test_glossary_delete_asks_confirmation():
    """_del_row в GlossaryEditor должен спрашивать подтверждение перед удалением."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    m = re.search(r"def _del_row\(self\):(.*?)(?=\n    def |\n\n\n)", src, re.DOTALL)
    assert m, "_del_row не найден"
    body = m.group(1)
    assert "QMessageBox.question" in body, \
        "_del_row должен спрашивать подтверждение через QMessageBox.question"
    assert "StandardButton.No" in body, \
        "По умолчанию должна быть выбрана кнопка 'Нет' (защита от случайного клика)"


def test_pipeline_get_stats_includes_per_file_and_stale_detection():
    """get_stats() должен возвращать per_file статистику и stale_extract флаг."""
    src = (Path(__file__).parent.parent / "pipeline.py").read_text(encoding="utf-8")
    m = re.search(r"def get_stats\(self\) -> dict:(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m, "get_stats не найден"
    body = m.group(1)
    assert 'stats["per_file"]' in body, "get_stats должен собирать per_file статистику"
    assert "_check_stale_extract_throttled" in body, \
        "get_stats должен использовать троттлированную проверку stale_extract"


def test_extract_writes_index_metadata():
    """Pipeline.extract() должен сохранять extract_index.json с timestamp."""
    src = (Path(__file__).parent.parent / "pipeline.py").read_text(encoding="utf-8")
    m = re.search(r"def extract\(.*?\n(.*?)(?=\n    def translate)", src, re.DOTALL)
    assert m, "extract() не найден"
    body = m.group(1)
    assert "extract_index.json" in body
    assert '"timestamp"' in body
    assert '"game_dir"' in body
    # BUG FIX: должна инвалидироваться сразу после свежего Extract, иначе UI
    # мог до 45с показывать «⚠ игра изменилась» СРАЗУ ПОСЛЕ повторного Extract.
    assert "_stale_check_cache.pop" in body, \
        "extract() должен инвалидировать _stale_check_cache сразу после записи индекса"


def test_stale_extract_check_is_functionally_correct(tmp_path):
    """
    Функциональный тест: _compute_stale_extract должен реально детектировать
    изменения файлов игры относительно timestamp экстракта — не просто
    проверка наличия строк в исходнике, а фактическое поведение.
    """
    import sys, time, json, os
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    output_dir = tmp_path / "output"
    game_dir.mkdir()
    output_dir.mkdir()

    data_file = game_dir / "data.json"
    data_file.write_text("{}", encoding="utf-8")
    time.sleep(0.05)
    extract_ts = time.time()
    (output_dir / "extract_index.json").write_text(json.dumps({
        "game_dir": str(game_dir), "game_type": "rpgmv",
        "timestamp": extract_ts, "total_strings": 0, "files": 1,
    }), encoding="utf-8")

    p = Pipeline(str(game_dir), str(output_dir))

    assert p._compute_stale_extract() is False, \
        "Файл не менялся после extract_ts — не должно быть stale"

    time.sleep(0.05)
    os.utime(data_file, (time.time(), time.time()))
    assert p._compute_stale_extract() is True, \
        "Файл изменился ПОСЛЕ extract_ts — должно быть stale"


def test_stale_extract_throttle_actually_throttles(tmp_path):
    """
    Функциональный тест троттлинга: повторный вызов _check_stale_extract_throttled
    внутри окна троттла должен возвращать закэшированное значение, а НЕ пересчитывать —
    это была реальная perf/корректность проблема (полное сканирование game_dir
    синхронно на UI-потоке каждые 4с через таймер статистики).
    """
    import sys, time, json, os
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    output_dir = tmp_path / "output"
    game_dir.mkdir()
    output_dir.mkdir()

    data_file = game_dir / "data.json"
    data_file.write_text("{}", encoding="utf-8")
    time.sleep(0.05)
    extract_ts = time.time()
    (output_dir / "extract_index.json").write_text(json.dumps({
        "game_dir": str(game_dir), "game_type": "rpgmv",
        "timestamp": extract_ts, "total_strings": 0, "files": 1,
    }), encoding="utf-8")

    Pipeline._stale_check_cache.pop(str(output_dir), None)
    p = Pipeline(str(game_dir), str(output_dir))

    # Делаем файл устаревшим и кэшируем это через throttled-check
    time.sleep(0.05)
    os.utime(data_file, (time.time(), time.time()))
    assert p._check_stale_extract_throttled() is True

    # Возвращаем файл к состоянию "не устарел" (mtime раньше extract_ts) —
    # но throttled-check должен ИГНОРИРОВАТЬ эту смену и вернуть закэшированное True,
    # раз окно троттла ещё не истекло.
    os.utime(data_file, (extract_ts - 100, extract_ts - 100))
    result_within_window = p._check_stale_extract_throttled()
    assert result_within_window is True, \
        "Внутри окна троттла должно возвращаться закэшированное значение, а не пересчёт"

    # Форсируем истечение окна троттла — теперь должен произойти реальный пересчёт
    key = str(output_dir)
    old_time, _ = Pipeline._stale_check_cache[key]
    Pipeline._stale_check_cache[key] = (old_time - 999, True)
    result_after_expiry = p._check_stale_extract_throttled()
    assert result_after_expiry is False, \
        "После истечения окна троттла должен произойти свежий пересчёт"


def test_stale_check_cache_invalidated_by_clear_cache(tmp_path):
    """clear_cache() должен инвалидировать и _stale_check_cache, не только _stats_class_cache."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    output_dir = tmp_path / "output"
    game_dir.mkdir()
    output_dir.mkdir()

    p = Pipeline(str(game_dir), str(output_dir))
    Pipeline._stale_check_cache[str(output_dir)] = (0.0, True)
    p.clear_cache()
    assert str(output_dir) not in Pipeline._stale_check_cache, \
        "clear_cache() должен убирать запись из _stale_check_cache"


def test_stats_panel_has_stale_warning_label():
    """StatsPanel должен иметь label для предупреждения об устаревшем экстракте."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "self.lbl_stale" in src
    assert "stale_extract" in src


def test_api_key_format_validation_exists():
    """SettingsPanel должен проверять формат API ключа против выбранного провайдера."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "_validate_api_key_format" in src
    m = re.search(r"def _validate_api_key_format\(self.*?\):(.*?)(?=\n    def |\Z)", src, re.DOTALL)
    assert m
    body = m.group(1)
    # Должны быть известные префиксы для каждого провайдера
    for prefix in ("sk-ant-", "AIza", "gsk_"):
        assert prefix in body, f"Префикс {prefix!r} должен быть в EXPECTED"


def test_api_key_hint_label_wired_to_text_changed():
    """lbl_key_hint должен обновляться при изменении api_key."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "self.api_key.textChanged.connect(self._validate_api_key_format)" in src


def test_api_key_hint_updates_on_manual_provider_switch():
    """
    РЕАЛЬНЫЙ функциональный тест (не string-matching): _validate_api_key_format
    была подключена только к api_key.textChanged. Если пользователь вставлял
    ключ (auto-detect правильно выставлял провайдера, подсказка скрыта), а
    затем ВРУЧНУЮ переключал провайдера в выпадающем списке (текст ключа не
    менялся) — теперь несовпадающая пара ключ/провайдер никогда не
    перепроверялась, подсказка оставалась скрытой даже когда была реально не права.

    ВАЖНО про методологию: QWidget.isVisible() для НИКОГДА не показанного
    виджета (без .show()) всегда возвращает False независимо от внутренних
    вызовов setVisible() — поэтому тест обязан вызвать .show() + processEvents(),
    иначе assert visible==False будет тривиально верным даже при сломанном коде.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sp = rd.SettingsPanel()
    sp.show()
    app.processEvents()

    # Вставляем валидный Gemini-ключ — auto-detect переключает провайдера на gemini
    sp.api_key.setText("AIzaSyRealLookingGeminiKey1234567890")
    app.processEvents()
    assert sp.provider.currentText() == "gemini"
    assert sp.lbl_key_hint.isVisible() is False

    # Пользователь вручную переключает провайдера на openai — ключ остался gemini-формата
    sp.provider.setCurrentText("openai")
    app.processEvents()
    assert sp.lbl_key_hint.isVisible() is True, \
        "После ручного переключения провайдера с несовпадающим ключом подсказка должна появиться"
    assert "openai" in sp.lbl_key_hint.text()


def test_api_key_hint_shows_for_unrecognized_key_format():
    """Ключ, не подходящий ни под один известный префикс, должен показывать предупреждение."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sp = rd.SettingsPanel()
    sp.show()
    app.processEvents()

    sp.provider.setCurrentText("claude")
    sp.api_key.setText("totally-unrecognizable-key-format-12345")
    app.processEvents()
    assert sp.lbl_key_hint.isVisible() is True
    assert "claude" in sp.lbl_key_hint.text()


def test_tm_manage_dialog_exists_with_export_and_clear():
    """StatsPanel должен иметь кнопку и диалог управления Global TM."""
    src = (Path(__file__).parent.parent / "rpgmtl_desktop.py").read_text(encoding="utf-8")
    assert "btn_tm_manage" in src
    assert "_tm_manage_dialog" in src
    m = re.search(r"def _tm_manage_dialog\(self\):(.*?)(?=\n\n#|\nclass )", src, re.DOTALL)
    assert m, "_tm_manage_dialog не найден"
    body = m.group(1)
    assert "export_csv" in body, "TM dialog должен предлагать экспорт"
    assert "DELETE FROM tm" in body, "TM dialog должен предлагать очистку"
    assert "StandardButton.No" in body, "Очистка TM должна требовать подтверждения (default=No)"


def _tm_dialog_click(stats_panel, button_label):
    """Хелпер: симулирует клик по кнопке с заданным label в _tm_manage_dialog."""
    from PyQt6.QtWidgets import QMessageBox
    holder = {}
    original_add = QMessageBox.addButton

    def tracking_add(self, label, role):
        btn = original_add(self, label, role)
        if label == button_label:
            holder["btn"] = btn
        return btn

    QMessageBox.addButton = tracking_add
    QMessageBox.exec = lambda self: 0
    QMessageBox.clickedButton = lambda self: holder.get("btn")
    try:
        stats_panel._tm_manage_dialog()
    finally:
        QMessageBox.addButton = original_add


def test_tm_manage_dialog_export_writes_real_csv(tmp_path):
    """Реальный функциональный тест: клик «Экспорт CSV» должен создать корректный CSV-файл."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd
    import global_tm

    global_tm.GlobalTM.DB_PATH = tmp_path / "tm.db"
    with global_tm.GlobalTM(target_lang="Russian") as tm:
        tm.set("phrase one here", "фраза один тут")
        tm.flush()

    stats_panel = rd.StatsPanel()
    export_path = tmp_path / "export.csv"
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **kw: (str(export_path), "CSV"))
    QMessageBox.information = staticmethod(lambda *a, **kw: None)

    _tm_dialog_click(stats_panel, "📤 Экспорт CSV")

    assert export_path.exists(), "Экспорт не создал файл"
    content = export_path.read_text(encoding="utf-8-sig")
    assert "phrase one here" in content
    assert "фраза один тут" in content


def test_tm_manage_dialog_clear_requires_confirmation(tmp_path):
    """
    КРИТИЧЕСКИЙ тест безопасности: клик «Очистить» без подтверждения
    (StandardButton.No) НЕ должен удалять данные из TM.
    """
    import sys, sqlite3
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd
    import global_tm

    global_tm.GlobalTM.DB_PATH = tmp_path / "tm.db"
    with global_tm.GlobalTM(target_lang="Russian") as tm:
        tm.set("phrase one here", "фраза один тут")
        tm.flush()

    stats_panel = rd.StatsPanel()
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No)
    QMessageBox.information = staticmethod(lambda *a, **kw: None)

    _tm_dialog_click(stats_panel, "🗑 Очистить")

    conn = sqlite3.connect(str(global_tm.GlobalTM.DB_PATH))
    count = conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
    conn.close()
    assert count == 1, "Отказ от подтверждения должен СОХРАНЯТЬ данные, но они были удалены"


def test_tm_manage_dialog_clear_with_confirmation_deletes(tmp_path):
    """Клик «Очистить» С подтверждением (StandardButton.Yes) должен реально удалить все записи."""
    import sys, sqlite3
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd
    import global_tm

    global_tm.GlobalTM.DB_PATH = tmp_path / "tm.db"
    with global_tm.GlobalTM(target_lang="Russian") as tm:
        tm.set("phrase one here", "фраза один тут")
        tm.flush()

    stats_panel = rd.StatsPanel()
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes)
    QMessageBox.information = staticmethod(lambda *a, **kw: None)

    _tm_dialog_click(stats_panel, "🗑 Очистить")

    conn = sqlite3.connect(str(global_tm.GlobalTM.DB_PATH))
    count = conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
    conn.close()
    assert count == 0, "Подтверждённая очистка должна удалить все записи"


# ═══════════════════════════════════════════════════════════════════════════
# Round 8: реальные функциональные тесты (не string-matching) для фич,
# которые раньше были покрыты только проверками наличия строк в исходнике.
# ═══════════════════════════════════════════════════════════════════════════

def test_xlsx_export_preserves_rpg_escape_codes(tmp_path):
    """Реальный прогон _export_xlsx: escape-коды (\\C[2], \\n) не должны портиться."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd
    import openpyxl

    sv = rd.StringsViewer()
    sv._output_dir = str(tmp_path)
    sv._file_map = {
        "Map001.json": [
            {"original": r"\C[2]勇者\C[0]は言った。\n「こんにちは」",
             "translated": r"\C[2]Герой\C[0] сказал: \n«Привет»",
             "context": "dialogue", "path": "events[0]"},
        ],
    }
    out_path = tmp_path / "test.xlsx"
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **kw: (str(out_path), "Excel (*.xlsx)"))
    QMessageBox.information = staticmethod(lambda *a, **kw: None)

    sv._export_xlsx()

    wb = openpyxl.load_workbook(out_path)
    ws = wb["Перевод"]
    row = list(ws.iter_rows(min_row=2, max_row=2, values_only=True))[0]
    assert row[1] == r"\C[2]勇者\C[0]は言った。\n「こんにちは」"
    assert row[2] == r"\C[2]Герой\C[0] сказал: \n«Привет»"


def test_xlsx_export_missing_openpyxl_shows_install_instructions(tmp_path):
    """Если openpyxl не установлен, должно быть понятное сообщение, а не краш."""
    import sys, builtins
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sv = rd.StringsViewer()
    sv._output_dir = str(tmp_path)
    sv._file_map = {"Map001.json": [{"original": "a", "translated": "b", "context": "", "path": ""}]}
    out_path = tmp_path / "x.xlsx"
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **kw: (str(out_path), "Excel (*.xlsx)"))

    warned = {}
    QMessageBox.warning = staticmethod(lambda *a, **kw: warned.update(text=a[2]))

    real_import = builtins.__import__
    def fake_import(name, *a, **kw):
        if name == "openpyxl":
            raise ImportError("No module named 'openpyxl'")
        return real_import(name, *a, **kw)
    builtins.__import__ = fake_import
    try:
        sv._export_xlsx()
    finally:
        builtins.__import__ = real_import

    assert "pip install openpyxl" in warned.get("text", "")
    assert not out_path.exists()


def test_cost_estimate_does_not_misleadingly_show_zero():
    """
    РЕГРЕССИЯ: {cost:.4f} печатал "~$0.0000" для дешёвых моделей на маленьких
    батчах, что выглядело как "бесплатно" и было неотличимо от реально
    бесплатных провайдеров (Gemini Flash, Groq). Малые ненулевые суммы должны
    показываться с достаточной точностью, чтобы не вводить в заблуждение.
    """
    import sys, tempfile, json
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    output_dir = tempfile.mkdtemp()
    ext_dir = Path(output_dir) / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Map001.json").write_text(json.dumps([
        {"original": "こんにちは", "translated": ""},
        {"original": "さようなら", "translated": ""},
    ]), encoding="utf-8")

    captured = {}
    QMessageBox.question = staticmethod(
        lambda *a, **kw: captured.update(text=a[2]) or QMessageBox.StandardButton.Yes
    )
    settings = {"model": "gpt-4o-mini", "provider": "openai", "batch_size": 20,
                "max_workers": 3, "request_delay": 0}
    win._confirm_translation_estimate(output_dir, settings)

    cost_line = [l for l in captured["text"].splitlines() if "Стоимость" in l][0]
    # BUG было: f"~${cost:.4f}" печатал ТОЧНО "~$0.0000" для маленьких сумм —
    # выглядело как "бесплатно". Проверяем что старый ТОЧНЫЙ формат (4 нуля и
    # больше ничего после $) больше не встречается — а не просто "нет подстроки
    # $0.0000", т.к. правильный "$0.000004" тоже содержит эту подстроку как префикс.
    assert not re.search(r"~\$0\.0000(?!\d)", cost_line), \
        f"Стоимость показана как обманчивый точный ноль: {cost_line!r}"
    assert "менее $0.0001" in cost_line, \
        f"Малая ненулевая сумма должна иметь поясняющую пометку: {cost_line!r}"


def test_cost_estimate_shows_free_for_truly_free_providers():
    """Реально бесплатные провайдеры (cost==0) должны показывать 'бесплатно', не $0.00."""
    import sys, tempfile, json
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    output_dir = tempfile.mkdtemp()
    ext_dir = Path(output_dir) / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Map001.json").write_text(json.dumps([
        {"original": "こんにちは", "translated": ""},
    ]), encoding="utf-8")

    captured = {}
    QMessageBox.question = staticmethod(
        lambda *a, **kw: captured.update(text=a[2]) or QMessageBox.StandardButton.Yes
    )
    settings = {"model": "gemini-3.6-flash", "provider": "gemini", "batch_size": 20,
                "max_workers": 3, "request_delay": 0}
    win._confirm_translation_estimate(output_dir, settings)

    cost_line = [l for l in captured["text"].splitlines() if "Стоимость" in l][0]
    assert "бесплатно" in cost_line


def test_keyboard_shortcuts_actually_trigger_bound_methods():
    """
    Реальный функциональный тест: F5/F6/F7 должны реально вызывать
    _run_extract/_run_translate/_run_inject через настоящий QShortcut,
    а не просто присутствовать как строки в исходнике.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QShortcut
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    # BUG FIX (test hygiene): патчинг rd.MainWindow._run_extract/... — это
    # CLASS-LEVEL замена, а не instance-level. Без восстановления в finally
    # она навсегда подменяет метод для ВСЕХ последующих MainWindow() во всём
    # процессе pytest — это сломало другой тест (test_run_translate_launches_
    # worker_when_confirmed), который позже вызывал win._run_translate() и
    # получал эту заглушку-счётчик вместо реального метода. Обнаружено только
    # при прогоне ПОЛНОГО test suite — по отдельности тесты проходили, потому
    # что порядок/набор запускаемых тестов не задевал сломанное состояние.
    calls = {"extract": 0, "translate": 0, "inject": 0}
    orig_extract = rd.MainWindow._run_extract
    orig_translate = rd.MainWindow._run_translate
    orig_inject = rd.MainWindow._run_inject
    rd.MainWindow._run_extract = lambda self: calls.__setitem__("extract", calls["extract"] + 1)
    rd.MainWindow._run_translate = lambda self: calls.__setitem__("translate", calls["translate"] + 1)
    rd.MainWindow._run_inject = lambda self: calls.__setitem__("inject", calls["inject"] + 1)

    try:
        win = rd.MainWindow()
        by_key = {sc.key().toString(): sc for sc in win.findChildren(QShortcut)}

        by_key["F5"].activated.emit()
        by_key["F6"].activated.emit()
        by_key["F7"].activated.emit()

        assert calls == {"extract": 1, "translate": 1, "inject": 1}
    finally:
        rd.MainWindow._run_extract = orig_extract
        rd.MainWindow._run_translate = orig_translate
        rd.MainWindow._run_inject = orig_inject


def test_ctrl_s_only_saves_when_dirty_and_routes_by_active_tab():
    """Ctrl+S должен сохранять активную вкладку, но только если есть несохранённые изменения."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QShortcut
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    by_key = {sc.key().toString(): sc for sc in win.findChildren(QShortcut)}

    save_calls = {"n": 0}
    win.strings_viewer.save = lambda: save_calls.__setitem__("n", save_calls["n"] + 1)

    # Не dirty -> Ctrl+S не должен ничего сохранять
    win.strings_viewer._dirty_files = set()
    win.tabs.setCurrentWidget(win.strings_viewer)
    by_key["Ctrl+S"].activated.emit()
    assert save_calls["n"] == 0, "Ctrl+S не должен сохранять когда нет изменений"

    # Dirty -> должен сохранить
    win.strings_viewer._dirty_files = {"Map001.json"}
    by_key["Ctrl+S"].activated.emit()
    assert save_calls["n"] == 1


def test_esc_only_cancels_when_busy():
    """Esc должен отменять операцию только если она реально выполняется."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QShortcut
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    by_key = {sc.key().toString(): sc for sc in win.findChildren(QShortcut)}

    cancel_calls = {"n": 0}
    win._request_cancel = lambda: cancel_calls.__setitem__("n", cancel_calls["n"] + 1)

    win._is_busy = False
    by_key["Esc"].activated.emit()
    assert cancel_calls["n"] == 0, "Esc не должен отменять когда ничего не выполняется"

    win._is_busy = True
    by_key["Esc"].activated.emit()
    assert cancel_calls["n"] == 1


def test_bulk_replace_full_roundtrip_load_replace_save_disk(tmp_path):
    """
    Полный функциональный тест старого бага (потеря данных в _bulk_replace):
    load() из диска -> bulk_replace() -> проверка таблицы И _file_map ->
    save() -> проверка что на диске реально лежат новые данные.
    """
    import sys, json
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sv = rd.StringsViewer()
    ext_dir = tmp_path / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Map001.json").write_text(json.dumps([
        {"original": "こんにちは", "translated": ""},
        {"original": "さようなら", "translated": ""},
    ]), encoding="utf-8")
    (ext_dir / "Map001_translated.json").write_text(json.dumps([
        {"original": "こんにちは", "translated": "Привет勇者"},
        {"original": "さようなら", "translated": "До свидания勇者"},
    ]), encoding="utf-8")

    sv.set_output_dir(str(tmp_path))
    sv.load()
    assert sv.table.rowCount() == 2

    call_n = {"n": 0}
    def fake_gettext(*a, **kw):
        call_n["n"] += 1
        return ("勇者", True) if call_n["n"] == 1 else ("Герой", True)
    QInputDialog.getText = staticmethod(fake_gettext)
    QMessageBox.information = staticmethod(lambda *a, **kw: None)

    sv._bulk_replace()

    after = [sv.table.item(r, 3).text() for r in range(sv.table.rowCount())]
    assert after == ["ПриветГерой", "До свиданияГерой"]

    fm = sv._file_map["Map001.json"]
    assert fm[0]["translated"] == "ПриветГерой", \
        "_file_map не обновлён — Save запишет устаревшие данные (регрессия старого бага)"
    assert fm[1]["translated"] == "До свиданияГерой"

    sv.save()
    saved = json.loads((ext_dir / "Map001_translated.json").read_text(encoding="utf-8"))
    assert saved[0]["translated"] == "ПриветГерой"
    assert saved[1]["translated"] == "До свиданияГерой"


def test_inject_dialog_overflow_lines_are_merged_not_truncated():
    """
    BUG (внешний аудит): RPG Maker выделяет фиксированное число command-слотов
    под многострочный диалог — inject не может добавить/убрать команды.
    Раньше избыток строк перевода молча обрезался (lines[:count]),
    безвозвратно теряя хвост текста, если AI вернул больше строк, чем было
    в оригинале (обычное дело — например при более многословном русском).
    Теперь избыток схлопывается в последнюю доступную строку — контент не
    теряется.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.injector import _inject_dialog

    data = {"events": [None, None, None, {"pages": [{"list": [
        {"code": 101, "parameters": []},
        {"code": 401, "parameters": ["line1"]},
        {"code": 401, "parameters": ["line2"]},
    ]}]}]}
    path = "events[3].pages[0].list[1..2].dialog"
    original = "line1\nline2"
    translation = "Раз\nДва\nТри которая раньше терялась\nЧетыре тоже терялась"
    logs = []
    ok = _inject_dialog(data, path, translation, original, log_callback=logs.append)
    assert ok is True
    l1 = data["events"][3]["pages"][0]["list"][1]["parameters"][0]
    l2 = data["events"][3]["pages"][0]["list"][2]["parameters"][0]
    full = l1 + " " + l2
    for word in ("Раз", "Два", "Три", "которая", "раньше", "терялась", "Четыре"):
        assert word in full, f"БАГ: слово {word!r} потеряно при обрезке: {full!r}"
    assert any("не помещается" in m for m in logs), "ожидался warning-лог о переполнении"


def test_bulk_replace_slash_only_search_does_not_mangle_every_string():
    """
    BUG: search_str "/" (или "//") проходит startswith("/") and endswith("/"),
    и search_str[1:-1] даёт "" — валидный regex, совпадающий на каждой позиции
    нулевой ширины. re.sub с пустым паттерном вставляет replace_str между
    КАЖДЫМ символом всех переводов сразу ("Герой" -> "XГXеXрXоXйX"), молча,
    без единого предупреждения. Пользователь, который просто ищет буквальный
    символ "/", одной опечаткой ломает весь загруженный проект.
    Ожидаемое поведение: "/" без текста внутри — не режим regex, обычный
    буквальный поиск (символ "/" в переводах не встречается — ничего не меняется).
    """
    import sys, json
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sv = rd.StringsViewer()
        ext_dir = tmp_path / "extracted"
        ext_dir.mkdir(parents=True)
        (ext_dir / "Map001.json").write_text(json.dumps([
            {"original": "こんにちは", "translated": ""},
        ]), encoding="utf-8")
        (ext_dir / "Map001_translated.json").write_text(json.dumps([
            {"original": "こんにちは", "translated": "Герой отправился в путь"},
        ]), encoding="utf-8")

        sv.set_output_dir(str(tmp_path))
        sv.load()

        call_n = {"n": 0}
        def fake_gettext(*a, **kw):
            call_n["n"] += 1
            return ("/", True) if call_n["n"] == 1 else ("X", True)
        QInputDialog.getText = staticmethod(fake_gettext)
        QMessageBox.information = staticmethod(lambda *a, **kw: None)

        sv._bulk_replace()

        after = sv.table.item(0, 3).text()
        assert after == "Герой отправился в путь", (
            f"БАГ: пустой regex-паттерн из '/' испортил перевод: {after!r}"
        )

        # "//" должно вести себя так же безопасно (тоже пустое тело).
        (ext_dir / "Map001_translated.json").write_text(json.dumps([
            {"original": "こんにちは", "translated": "Ещё одна строка"},
        ]), encoding="utf-8")
        sv.load()
        call_n["n"] = 0
        def fake_gettext2(*a, **kw):
            call_n["n"] += 1
            return ("//", True) if call_n["n"] == 1 else ("X", True)
        QInputDialog.getText = staticmethod(fake_gettext2)
        sv._bulk_replace()
        after2 = sv.table.item(0, 3).text()
        assert after2 == "Ещё одна строка", (
            f"БАГ: пустой regex-паттерн из '//' испортил перевод: {after2!r}"
        )

        # Контроль: настоящий regex с непустым телом должен по-прежнему работать.
        (ext_dir / "Map001_translated.json").write_text(json.dumps([
            {"original": "こんにちは", "translated": "болван болван"},
        ]), encoding="utf-8")
        sv.load()
        call_n["n"] = 0
        def fake_gettext3(*a, **kw):
            call_n["n"] += 1
            return ("/болван/", True) if call_n["n"] == 1 else ("герой", True)
        QInputDialog.getText = staticmethod(fake_gettext3)
        sv._bulk_replace()
        after3 = sv.table.item(0, 3).text()
        assert after3 == "герой герой", (
            f"Regex с непустым телом должен продолжать работать: {after3!r}"
        )


def test_global_tm_end_to_end_through_translate_texts(tmp_path):
    """
    Полный интеграционный тест: translate_texts() должен (1) пропускать
    API-вызов для строк уже в GlobalTM, (2) реально вызывать провайдера для
    новых строк, (3) записывать новые переводы обратно в GlobalTM.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import ai_translator
    from ai_translator import TranslatorConfig, translate_texts
    import global_tm

    global_tm.GlobalTM.DB_PATH = tmp_path / "tm.db"
    # BUG FIX P1-22: tm_namespace теперь включает source_lang (по умолчанию
    # "Japanese" в TranslatorConfig) — используем ту же составную строку,
    # что и сам translate_texts(), иначе тест бьёт мимо реального namespace.
    tm_ns = "Japanese|Russian"
    with global_tm.GlobalTM(target_lang=tm_ns) as tm:
        tm.set("common greeting phrase", "общая фраза приветствия")
        tm.flush()

    call_log = []
    def fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event):
        call_log.append(list(batch))
        return True, [f"ПЕРЕВЕЛ: {t}" for t in batch]

    original = ai_translator._do_translate_batch
    ai_translator._do_translate_batch = fake_do_translate_batch
    try:
        cfg = TranslatorConfig(provider="openai", api_key="fake", target_lang="Russian",
                                use_global_tm=True, batch_size=10, max_workers=1)
        texts = ["common greeting phrase", "a brand new unique phrase never seen"]
        result = translate_texts(texts, cfg, log_callback=None)

        sent_to_api = [t for batch in call_log for t in batch]
        assert "common greeting phrase" not in sent_to_api, \
            "Строка из GlobalTM не должна отправляться в API повторно"
        assert any("brand new unique phrase" in t for t in sent_to_api)

        with global_tm.GlobalTM(target_lang=tm_ns) as tm2:
            cached = tm2.get("a brand new unique phrase never seen")
            assert cached == "ПЕРЕВЕЛ: a brand new unique phrase never seen", \
                "Новый перевод должен быть записан обратно в GlobalTM"
    finally:
        ai_translator._do_translate_batch = original

# ═══════════════════════════════════════════════════════════════════════════
# Round 9: CSV import с пустым path (реальный краш-баг, найден через выполнение)
# ═══════════════════════════════════════════════════════════════════════════

def test_csv_import_does_not_silently_drop_empty_path_rows(tmp_path):
    """
    КРИТИЧЕСКИЙ регрессионный тест. Баг: `if fname and path_val and trans:`
    трактовал ПУСТУЮ строку "" в колонке "Путь" как falsy — поэтому ЛЮБАЯ
    строка без уникального path (легитимное состояние — не у всех типов
    записей есть path) молча выбрасывалась из import_map. Результат:
    "Импортировано 0 строк" без единой ошибки, даже когда CSV содержал
    корректно заполненные переводы.

    Этот тест реально прогоняет export -> edit -> import с ДУБЛИРУЮЩИМСЯ
    original текстом и БЕЗ path (репрезентативно для повторяющихся UI-строк
    типа "はい"), и проверяет что обе строки получают СВОИ РАЗНЫЕ переводы,
    а не 0 импортированных и не коллизию на одно значение.
    """
    import sys, csv as csv_mod
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sv = rd.StringsViewer()
    ext_dir = tmp_path / "extracted"
    ext_dir.mkdir(parents=True)
    entries = [
        {"original": "はい", "translated": ""},
        {"original": "はい", "translated": ""},
    ]
    (ext_dir / "Common.json").write_text(json.dumps(entries), encoding="utf-8")
    (ext_dir / "Common_translated.json").write_text(json.dumps(
        [{"original": e["original"], "translated": ""} for e in entries]
    ), encoding="utf-8")
    sv.set_output_dir(str(tmp_path))
    sv.load()
    assert sv.table.rowCount() == 2

    csv_path = tmp_path / "export.csv"
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **kw: (str(csv_path), "CSV"))
    QMessageBox.information = staticmethod(lambda *a, **kw: None)
    sv._export_csv()
    assert csv_path.exists()

    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv_mod.DictReader(f, delimiter=";")
        for row in reader:
            rows.append(row)
    assert len(rows) == 2
    rows[0]["Перевод"] = "Да (обычное)"
    rows[1]["Перевод"] = "Да (вежливое)"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv_mod.DictWriter(
            f, fieldnames=["Файл", "Контекст", "Оригинал", "Перевод", "Путь"], delimiter=";"
        )
        writer.writeheader()
        writer.writerows(rows)

    QFileDialog.getOpenFileName = staticmethod(lambda *a, **kw: (str(csv_path), "CSV"))
    info_holder = {}
    QMessageBox.information = staticmethod(lambda *a, **kw: info_holder.update(text=a[2]))
    sv._import_csv()

    after = [sv.table.item(r, 3).text() for r in range(sv.table.rowCount())]
    assert after == ["Да (обычное)", "Да (вежливое)"], \
        f"CSV import с пустым path сломан: получено {after}"
    assert "Импортировано 2" in info_holder.get("text", ""), \
        f"Ожидали 2 импортированные строки, получили: {info_holder.get('text')!r}"


def test_csv_import_still_works_with_unique_paths(tmp_path):
    """Регрессия: обычный случай с уникальными path должен продолжать работать после фикса."""
    import sys, csv as csv_mod
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sv = rd.StringsViewer()
    ext_dir = tmp_path / "extracted"
    ext_dir.mkdir(parents=True)
    entries = [
        {"original": "こんにちは", "translated": "", "path": "a"},
        {"original": "さようなら", "translated": "", "path": "b"},
    ]
    (ext_dir / "Map001.json").write_text(json.dumps(entries), encoding="utf-8")
    (ext_dir / "Map001_translated.json").write_text(json.dumps(
        [{"original": e["original"], "translated": ""} for e in entries]
    ), encoding="utf-8")
    sv.set_output_dir(str(tmp_path))
    sv.load()

    csv_path = tmp_path / "export.csv"
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **kw: (str(csv_path), "CSV"))
    QMessageBox.information = staticmethod(lambda *a, **kw: None)
    sv._export_csv()

    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv_mod.DictReader(f, delimiter=";")
        for row in reader:
            rows.append(row)
    rows[0]["Перевод"] = "Привет"
    rows[1]["Перевод"] = "До свидания"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv_mod.DictWriter(
            f, fieldnames=["Файл", "Контекст", "Оригинал", "Перевод", "Путь"], delimiter=";"
        )
        writer.writeheader()
        writer.writerows(rows)

    QFileDialog.getOpenFileName = staticmethod(lambda *a, **kw: (str(csv_path), "CSV"))
    QMessageBox.information = staticmethod(lambda *a, **kw: None)
    sv._import_csv()

    after = [sv.table.item(r, 3).text() for r in range(sv.table.rowCount())]
    assert after == ["Привет", "До свидания"]


def test_qa_check_detects_all_three_problem_categories():
    """Реальный прогон QA-проверки: пустые/одинаковые/недо-переведённые строки."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    sv = rd.StringsViewer()
    sv._file_map = {
        "Map001.json": [
            {"original": "こんにちは", "translated": ""},
            {"original": "さようなら", "translated": "さようなら"},
            {"original": "勇者", "translated": "勇者だ"},
            {"original": "ありがとう", "translated": "Спасибо"},
        ],
    }
    captured = {}
    QMessageBox.warning = staticmethod(lambda *a, **kw: captured.update(kind="warning", text=a[2]))
    sv._qa_check()
    assert captured["kind"] == "warning"
    assert "Пустые переводы: 1" in captured["text"]
    assert "Одинаковые с оригиналом: 1" in captured["text"]
    assert "Японские символы в переводе: 1" in captured["text"]


def test_run_translate_aborts_when_cost_dialog_declined():
    """
    Реальный интеграционный тест: если пользователь отклоняет диалог
    подтверждения стоимости, _run_translate НЕ должен запускать worker thread.
    """
    import sys, tempfile
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    game_dir = tempfile.mkdtemp()
    output_dir = tempfile.mkdtemp()
    Path(game_dir, "www", "data").mkdir(parents=True)
    (Path(game_dir, "www", "data", "Map001.json")).write_text("{}", encoding="utf-8")
    ext_dir = Path(output_dir) / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Map001.json").write_text(
        json.dumps([{"original": "こんにちは", "translated": ""}]), encoding="utf-8"
    )
    win.path_panel.game_dir.setText(game_dir)
    win.path_panel.output_dir.setText(output_dir)
    win.path_panel._detected_type = "rpgmv"
    win.settings_panel.api_key.setText("sk-test-fake-key")
    win.settings_panel.provider.setCurrentText("openai")

    thread_calls = {"n": 0}
    win._run_in_thread = lambda *a, **kw: thread_calls.__setitem__("n", thread_calls["n"] + 1)
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No)

    win._run_translate()
    assert thread_calls["n"] == 0, "Worker не должен запускаться при отказе от подтверждения"


def test_run_translate_launches_worker_when_confirmed():
    """Реальный интеграционный тест: подтверждение всех диалогов запускает worker с валидной task factory."""
    import sys, tempfile
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    game_dir = tempfile.mkdtemp()
    output_dir = tempfile.mkdtemp()
    Path(game_dir, "www", "data").mkdir(parents=True)
    (Path(game_dir, "www", "data", "Map001.json")).write_text("{}", encoding="utf-8")
    ext_dir = Path(output_dir) / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Map001.json").write_text(
        json.dumps([{"original": "こんにちは", "translated": ""}]), encoding="utf-8"
    )
    win.path_panel.game_dir.setText(game_dir)
    win.path_panel.output_dir.setText(output_dir)
    win.path_panel._detected_type = "rpgmv"
    win.settings_panel.api_key.setText("sk-test-fake-key")
    win.settings_panel.provider.setCurrentText("openai")

    captured = {}
    win._run_in_thread = lambda make_task, op: captured.update(make_task=make_task, op=op)
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes)

    win._run_translate()
    assert captured.get("op") == "Translate"
    task_fn = captured["make_task"](lambda m: None, lambda *a: None, None)
    assert callable(task_fn)


# ═══════════════════════════════════════════════════════════════════════════
# Round 10: свежий аудит всего проекта. cli.py translate был полностью
# нерабочим (2 независимых краш-бага), бэкап Unity портился при повторном
# Inject, ручные правки в StringsViewer откатывались следующим Translate,
# пустые термины глоссария попадали в промпт как "term → ", отмена перевода
# пропускала проверку/восстановление escape-кодов, а Godot _unescape ломал
# порядок замен \\n/\\\\ так же, как это уже когда-то было в Ren'Py.
# ═══════════════════════════════════════════════════════════════════════════

def test_cli_translate_base_url_argument_exists():
    """
    cli.py ссылался на args.base_url в cmd_translate() (и документировал флаг
    в docstring модуля), но argparse никогда не регистрировал --base-url —
    ЛЮБОЙ вызов `cli.py translate` падал с
    "AttributeError: 'Namespace' object has no attribute 'base_url'"
    ещё до первого обращения к провайдеру.
    """
    import cli
    import ai_translator

    def fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event):
        return True, [f"ПЕРЕВОД: {t}" for t in batch]

    original = ai_translator._do_translate_batch
    ai_translator._do_translate_batch = fake_do_translate_batch
    try:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "game"
            output_dir = Path(tmp) / "out"
            (game_dir / "www" / "data").mkdir(parents=True)
            (game_dir / "www" / "data" / "Map001.json").write_text("{}", encoding="utf-8")
            output_dir.mkdir()
            ext_dir = output_dir / "extracted"
            ext_dir.mkdir()
            (ext_dir / "Map001.json").write_text(
                json.dumps([{"file": "Map001.json", "path": "x",
                             "original": "こんにちは", "translated": ""}], ensure_ascii=False),
                encoding="utf-8",
            )
            # Без manifest'а (как оставляет за собой настоящий Extract())
            # Translate блокируется Project Lock'ом ещё до args.base_url —
            # тест бил бы мимо проверяемой цели (CLI argparse wiring).
            from engine.project_lock import write_manifest
            (output_dir / "extract_index.json").write_text(
                json.dumps({"game_type": "rpgmv",
                            "sources": {"Map001.json": "www/data/Map001.json"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            write_manifest(output_dir, game_dir, "rpgmv")

            sys.argv = [
                "cli.py", "translate",
                "--game", str(game_dir),
                "--output", str(output_dir),
                "--provider", "openai",
                "--api-key", "sk-fake",
                "--base-url", "http://localhost:1234/v1",  # must parse without AttributeError
            ]
            rc = cli.main()
            assert rc == 0, "cli.py translate должен успешно завершиться с мок-провайдером"

            saved = json.loads((ext_dir / "Map001_translated.json").read_text(encoding="utf-8"))
            assert saved[0]["translated"] == "ПЕРЕВОД: こんにちは"
    finally:
        ai_translator._do_translate_batch = original


def test_cli_progress_callback_accepts_four_args():
    """
    Pipeline.translate() всегда вызывает progress_callback с 4 позиционными
    аргументами (cur, total, eta, fname) — см. p_wrap/_overall_progress и
    все extractor/injector движки. Локальная progress() внутри cmd_translate
    принимала только 3, поэтому самый первый вызов (безусловный, ещё до
    начала перевода) падал с
    "TypeError: progress() takes 3 positional arguments but 4 were given".
    """
    import inspect
    import cli

    src = inspect.getsource(cli.cmd_translate)
    m = re.search(r"def progress\(([^)]*)\)", src)
    assert m, "cmd_translate должен определять локальную функцию progress()"
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    assert len(params) >= 4, (
        f"progress() внутри cmd_translate принимает параметров: {params} — "
        f"но Pipeline.translate() зовёт его с 4 позиционными аргументами "
        f"(cur, total, eta, fname). Регрессия старого краш-бага."
    )


def test_unity_inject_backup_not_corrupted_by_repeated_inject():
    """
    unity_injector._backup_file() безусловно копировал ТЕКУЩЕЕ содержимое
    файла в бэкап на каждом вызове — Unity был единственным из восьми
    движков без маркера идемпотентности. Второй Inject подряд (обычный
    сценарий: перевели ещё немного и повторили Inject) молча уничтожал
    истинный оригинал в бэкапе своим же переведённым содержимым, и
    «Восстановить оригинал» после этого вернул бы уже переведённый текст
    вместо японского — без предупреждения и без возможности отмены.
    """
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        out_dir = tmp / "out"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        (out_dir / "extracted").mkdir(parents=True)

        loc_file = game_dir / "StreamingAssets" / "dialogue.json"
        loc_file.write_text(json.dumps({"greeting": "こんにちは"}, ensure_ascii=False), encoding="utf-8")

        (out_dir / "extract_index.json").write_text(
            json.dumps({"sources": {"dialogue": "StreamingAssets/dialogue.json"}}),
            encoding="utf-8",
        )
        (out_dir / "extracted" / "dialogue_translated.json").write_text(
            json.dumps([{
                "file": "dialogue.json", "path": "greeting",
                "original": "こんにちは", "translated": "Привет",
                "context": "unity_json",
            }], ensure_ascii=False),
            encoding="utf-8",
        )

        backup_file = game_dir / "_backup_unity_original" / "StreamingAssets" / "dialogue.json"

        inject_unity(game_dir, out_dir, backup=True)
        assert json.loads(loc_file.read_text(encoding="utf-8"))["greeting"] == "Привет"
        assert json.loads(backup_file.read_text(encoding="utf-8"))["greeting"] == "こんにちは"

        # Типичный сценарий: перевели ещё немного, повторили Inject (дважды).
        inject_unity(game_dir, out_dir, backup=True)
        inject_unity(game_dir, out_dir, backup=True)

        assert json.loads(loc_file.read_text(encoding="utf-8"))["greeting"] == "Привет", \
            "Игровой файл должен остаться корректно переведён"
        assert json.loads(backup_file.read_text(encoding="utf-8"))["greeting"] == "こんにちは", \
            "БАГ: бэкап перезаписан переведённым текстом — истинный оригинал потерян"


def test_unity_backup_file_is_idempotent_in_isolation():
    """
    Изолированная проверка на уровне самой _backup_file(), не полагающаяся на
    механизм маркер+restore: тот факт, что restore-before-reinject тоже чинит
    типичный сценарий, не должен маскировать то, что _backup_file() сама по
    себе обязана быть idempotent (create-once). Если маркер по какой-то
    причине отсутствует или бэкап-папка была создана без него, только эта
    защита и остаётся.
    """
    from engine.unity_injector import _backup_file

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        backup_root = Path(tmp) / "backup"
        (game_dir / "sub").mkdir(parents=True)
        src = game_dir / "sub" / "f.json"

        src.write_text("original-true-content", encoding="utf-8")
        _backup_file(src, backup_root, game_dir)
        dst = backup_root / "sub" / "f.json"
        assert dst.read_text(encoding="utf-8") == "original-true-content"

        # Файл в игре теперь "переведён" (или просто изменён), _backup_file
        # вызывается ПОВТОРНО (как на каждом Inject) — бэкап не должен измениться.
        src.write_text("now-translated-content", encoding="utf-8")
        _backup_file(src, backup_root, game_dir)
        assert dst.read_text(encoding="utf-8") == "original-true-content", (
            "БАГ: _backup_file перезаписала уже существующий бэкап новым содержимым файла"
        )


def test_unity_inject_marker_restores_before_reinject():
    """
    Unity был единственным движком без маркера предыдущей инъекции — Inject
    никогда не восстанавливал файлы из бэкапа перед повторным запуском (в
    отличие от Wolf/VXAce/Kirikiri/Tyrano/Godot/Ren'Py). Проверяем не только
    что маркер создаётся, но и что он реально используется: если игровой
    файл между двумя Inject оказался в промежуточном/повреждённом состоянии,
    следующий Inject должен сперва восстановить его из бэкапа, а не
    попытаться (и не суметь) заново пропарсить испорченный файл.
    """
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        out_dir = tmp / "out"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        (out_dir / "extracted").mkdir(parents=True)
        loc_file = game_dir / "StreamingAssets" / "d.json"
        loc_file.write_text(json.dumps({"g": "こんにちは"}, ensure_ascii=False), encoding="utf-8")
        (out_dir / "extract_index.json").write_text(
            json.dumps({"sources": {"d": "StreamingAssets/d.json"}}), encoding="utf-8"
        )
        (out_dir / "extracted" / "d_translated.json").write_text(
            json.dumps([{"file": "d.json", "path": "g", "original": "こんにちは",
                         "translated": "Привет"}], ensure_ascii=False),
            encoding="utf-8",
        )

        marker = game_dir / "_backup_unity_original" / ".rpgmtl_injected"
        assert not marker.exists()
        summary1 = inject_unity(game_dir, out_dir, backup=True)
        assert summary1["files_modified"] == 1
        assert marker.exists(), "Маркер должен быть создан после успешной инъекции"
        assert json.loads(loc_file.read_text(encoding="utf-8"))["g"] == "Привет"

        # Симулируем, что игровой файл оказался в постороннем/битом состоянии
        # (не валидный JSON) между двумя запусками Inject.
        loc_file.write_text("NOT VALID JSON AT ALL", encoding="utf-8")

        summary2 = inject_unity(game_dir, out_dir, backup=True)
        # Если бы restore-before-reinject не сработал, инжектор наткнулся бы
        # на невалидный JSON и не смог бы применить перевод.
        assert summary2["files_modified"] == 1, (
            "БАГ: маркер не восстановил файл из бэкапа перед повторной инъекцией — "
            f"инъекция не смогла применить перевод: {summary2}"
        )
        assert json.loads(loc_file.read_text(encoding="utf-8"))["g"] == "Привет"


def test_translate_does_not_revert_manual_stringsviewer_edit():
    """
    _sync_existing_translations пропускал обновление state целиком, если
    state.has(orig) уже True — даже когда файл на диске (после ручной
    правки через StringsViewer: Save / Bulk Replace / Re-translate)
    отличается от закэшированного значения. Следующий запуск Translate брал
    старое значение из state, и apply_dedup_to_files() тихо перезаписывал
    ручную правку пользователя обратно на устаревший AI-перевод.
    """
    from pipeline import Pipeline
    from engine.state import load_state
    from engine.project_lock import write_manifest

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        output_dir = Path(tmp) / "out"
        (game_dir / "www" / "data").mkdir(parents=True)
        (game_dir / "www" / "data" / "Actors.json").write_text("{}", encoding="utf-8")
        output_dir.mkdir()
        ext_dir = output_dir / "extracted"
        ext_dir.mkdir()

        src_entries = [{"file": "Actors.json", "path": "[0].name",
                        "original": "勇者", "translated": "", "context": "Actor: 勇者"}]
        (ext_dir / "Actors.json").write_text(
            json.dumps(src_entries, ensure_ascii=False), encoding="utf-8"
        )
        # Настоящий Extract() пишет extract_index.json и project_manifest.json —
        # без них Translate блокируется Project Lock'ом (verify_manifest) ещё до
        # того, как дело доходит до _sync_existing_translations. Воспроизводим
        # то, что реально оставляет после себя Extract, чтобы тест бил именно
        # в проверяемую логику, а не в непричастную защиту от чужой игры.
        (output_dir / "extract_index.json").write_text(
            json.dumps({"game_type": "rpgmv",
                        "sources": {"Actors.json": "www/data/Actors.json"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        write_manifest(output_dir, game_dir, "rpgmv")

        state = load_state(output_dir, log_cb=lambda m: None)
        state.set("勇者", "Герой (AI)")
        state.save()

        # Пользователь открыл StringsViewer и вручную исправил перевод —
        # это пишет ПРЯМО в *_translated.json, минуя state.
        tr_entries = [{"file": "Actors.json", "path": "[0].name",
                       "original": "勇者", "translated": "Герой (ручная правка)",
                       "context": "Actor: 勇者"}]
        (ext_dir / "Actors_translated.json").write_text(
            json.dumps(tr_entries, ensure_ascii=False), encoding="utf-8"
        )

        # Пользователь снова жмёт Translate (например, чтобы подхватить
        # новый текст после патча игры). "勇者" уже в кэше — новый API-вызов
        # для него не нужен, так что мок провайдера тут не требуется.
        pipeline = Pipeline(str(game_dir), str(output_dir), log_callback=lambda m: None)
        pipeline.translate(provider="openai", api_key="sk-fake", target_lang="Russian",
                            use_global_tm=False)

        result = json.loads((ext_dir / "Actors_translated.json").read_text(encoding="utf-8"))
        assert result[0]["translated"] == "Герой (ручная правка)", (
            f"БАГ: ручная правка отменена, файл содержит {result[0]['translated']!r}"
        )
        state2 = load_state(output_dir, log_cb=lambda m: None)
        assert state2.get("勇者") == "Герой (ручная правка)", (
            "БАГ: state не обновлён под ручную правку — следующий Translate снова её сотрёт"
        )


def test_glossary_prompt_block_skips_empty_translations():
    """
    Glossary.to_prompt_block() включал термины без перевода (только что
    обнаруженные auto_detect_actors / extract_game_terms, ещё не заполненные
    пользователем или auto_translate_glossary) как "  term → " — пустая цель
    после стрелки внутри промпта, который прямо требует «always use these
    translations, never change them». apply() уже был защищён от этого же
    случая (см. её собственный BUG FIX #1); to_prompt_block — нет.
    """
    from engine.glossary import Glossary

    g = Glossary()
    g.terms["勇者"] = "Герой"
    g.terms["アリス"] = ""   # только обнаружен, ещё не переведён
    g.terms["ボブ"] = ""

    block = g.to_prompt_block("Japanese", "Russian")
    assert "アリス" not in block
    assert "ボブ" not in block
    assert "勇者 → Герой" in block

    g2 = Glossary()
    g2.terms["x"] = ""
    assert g2.to_prompt_block() == "", "Глоссарий из одних пустых терминов не должен давать промпт-блок"


def test_cancelled_translate_still_repairs_broken_codes():
    """
    Проход валидации (проверка/восстановление escape-кодов) стоял ПОСЛЕ
    `if was_cancelled: return` — при отмене перевода уже собранные
    результаты (в т.ч. с кодами, испорченными если cancel_event сработал
    прямо в середине retry-цикла для конкретной строки — translate_texts()
    в этом случае всё равно помечает её success=True) оставались сломанными
    и в state, и в *_translated.json файлах без единой попытки
    восстановления, хотя apply_dedup_to_files() уже применил их к диску.
    """
    import ai_translator

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        output_dir = Path(tmp) / "out"
        (game_dir / "www" / "data").mkdir(parents=True)
        (game_dir / "www" / "data" / "Map001.json").write_text("{}", encoding="utf-8")
        output_dir.mkdir()
        ext_dir = output_dir / "extracted"
        ext_dir.mkdir()

        original_text = "\\C[1]Hello"
        (ext_dir / "Map001.json").write_text(
            json.dumps([{"file": "Map001.json", "path": "x",
                         "original": original_text, "translated": ""}], ensure_ascii=False),
            encoding="utf-8",
        )
        # Без manifest'а Translate блокируется Project Lock'ом ещё до
        # cancel-сценария — ok=False был бы верным, но по неверной причине.
        from engine.project_lock import write_manifest
        (output_dir / "extract_index.json").write_text(
            json.dumps({"game_type": "rpgmv",
                        "sources": {"Map001.json": "www/data/Map001.json"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        write_manifest(output_dir, game_dir, "rpgmv")

        # Точный конечный результат сценария "cancel сработал в середине
        # retry-цикла": cancelled=True, но единственный результат помечен
        # success=True, хотя \C[1] потерян.
        fake_result = ai_translator.BatchResult(
            results=[ai_translator.TranslationResult(
                original=original_text, translated="Привет", success=True,
            )],
            total=1, succeeded=1, failed=0, from_cache=0, cancelled=True,
        )

        original_fn = ai_translator.translate_texts
        ai_translator.translate_texts = lambda *a, **kw: fake_result
        try:
            from pipeline import Pipeline
            pipeline = Pipeline(str(game_dir), str(output_dir), log_callback=lambda m: None)
            ok, msg = pipeline.translate(
                provider="openai", api_key="sk-fake", target_lang="Russian",
                use_global_tm=False,
            )
        finally:
            ai_translator.translate_texts = original_fn

        assert ok is False, "Отменённый перевод должен вернуть ok=False"

        from engine.state import load_state
        state = load_state(output_dir, log_cb=lambda m: None)
        assert state.get(original_text) == "\\C[1]Привет", (
            f"БАГ: escape-код не восстановлен при отмене, state содержит {state.get(original_text)!r}"
        )

        saved = json.loads((ext_dir / "Map001_translated.json").read_text(encoding="utf-8"))
        assert saved[0]["translated"] == "\\C[1]Привет", (
            f"БАГ: escape-код не восстановлен в файле при отмене: {saved[0]['translated']!r}"
        )


def test_godot_unescape_does_not_corrupt_escaped_backslash_before_n():
    """
    _unescape() в godot_extractor.py повторял ошибку порядка замен, уже
    исправленную ранее в renpy_extractor._unquote(): обработка \\n раньше
    \\\\ портила экранированный бэкслеш, за которым сразу следует буква "n"
    (например путь "C:\\network" в исходнике GDScript — один бэкслеш и
    "network") в реальный перевод строки вместо literal backslash+"n".
    """
    from engine.godot_extractor import GodotExtractor

    g = GodotExtractor("/tmp")
    raw = "C:" + chr(92) * 2 + "network"        # "C:\\network" как в исходнике
    expected = "C:" + chr(92) + "network"       # один бэкслеш + "network"
    decoded = g._unescape(raw)
    assert decoded == expected, f"БАГ: decoded={decoded!r}, expected={expected!r}"
    assert "\n" not in decoded, "БАГ: экранированный бэкслеш перед 'n' породил реальный перевод строки"

    # Обычные escape-последовательности должны продолжать работать как раньше.
    assert g._unescape("Line1" + chr(92) + "nLine2") == "Line1\nLine2"
    assert g._unescape('Quote: ' + chr(92) + '"hi' + chr(92) + '"') == 'Quote: "hi"'


# ═══════════════════════════════════════════════════════════════════════════
# Round 11: устранение известного ограничения (не бага) в восстановлении
# escape-кодов — позиция разреза считалась строго по пропорции символов и
# могла разрезать слово перевода пополам.
# ═══════════════════════════════════════════════════════════════════════════

def test_repair_codes_does_not_split_word_in_half():
    """
    _split_text_by_weights резала строго по пропорции символов от длины
    сегментов оригинала, из-за чего восстанавливаемый код мог оказаться
    внутри слова перевода (например "героя" -> "ге" + код + "роя") —
    коды оставались валидными, но визуально эффект переключался посреди
    слова. Теперь позиция разреза подтягивается к ближайшему пробелу.
    """
    from ai_translator import _repair_codes, _validate_codes

    original = "\\C[2]Yuusha\\C[0]\u306e\u904b\u547d\u306f\u541b\u306b\u304b\u304b\u3063\u3066\u3044\u308b\u3002"
    bad_translation = "Судьба героя зависит от тебя."

    repaired = _repair_codes(original, bad_translation)
    assert _validate_codes(original, repaired), f"Коды невалидны после repair: {repaired!r}"
    assert "ге\\C[0]роя" not in repaired, f"БАГ: код вставлен внутрь слова 'героя': {repaired!r}"

    # Множественные коды — каждый должен попасть на границу слова, а не в середину.
    original2 = "\\C[1]Alice\\C[0] and \\C[1]Bob\\C[0] are friends."
    bad2 = "Алиса и Боб — друзья."
    repaired2 = _repair_codes(original2, bad2)
    assert _validate_codes(original2, repaired2)
    for broken_fragment in ("ис\\C", "ис\\N", "об\\C"):
        assert broken_fragment not in repaired2, (
            f"БАГ: код разрезал слово посередине: {repaired2!r}"
        )


def test_split_text_by_weights_falls_back_when_no_spaces_nearby():
    """Для языков без пробелов между словами (или одного длинного слова)
    снап к границе не должен ничего ломать — используется как раньше
    посимвольная позиция, раз лучшего варианта нет."""
    from ai_translator import _split_text_by_weights

    parts = _split_text_by_weights("абвгдеёжзиклмнопрстуфхцчшщ", [1, 1])
    assert "".join(parts) == "абвгдеёжзиклмнопрстуфхцчшщ"
    assert all(parts)

    parts2 = _split_text_by_weights("", [1, 1])
    assert parts2 == ["", ""]


# ═══════════════════════════════════════════════════════════════════════════
# Round 12: ретроспективный аудит новой AI Polish функциональности вскрыл
# паттерн, унаследованный от уже существующего кода перевода — retry/delay
# паузы без cancel_event (т.е. каждый прогон через cli.py) пропускались
# полностью вместо реального ожидания, и паузы между батчами не реагировали
# на отмену даже когда cancel_event был передан.
# ═══════════════════════════════════════════════════════════════════════════

def test_do_translate_batch_actually_waits_without_cancel_event(monkeypatch):
    """
    БАГ: "cancel_event.wait(wait_time) if cancel_event else False" пропускал
    ожидание ПОЛНОСТЬЮ, когда cancel_event не передан — ровно случай
    `cli.py translate` (cli.py вообще не создаёт cancel_event). Retry после
    ошибки/rate-limit срабатывал мгновенно, без единой паузы, вместо
    рассчитанного wait_time — это могло долбить ещё активный rate limit
    все max_retries попыток подряд без единого реального ожидания.
    """
    import ai_translator
    from ai_translator import _do_translate_batch, TranslatorConfig

    sleep_calls = []
    monkeypatch.setattr(ai_translator.time, "sleep", lambda s: sleep_calls.append(s))

    calls = {"n": 0}

    class FlakyProvider:
        def translate_batch(self, texts, log_cb=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporary failure")
            return texts

    cfg = TranslatorConfig(max_retries=3, retry_delay=5.0)
    ok, out = _do_translate_batch(FlakyProvider(), ["hello"], cfg, None, None)  # cancel_event=None
    assert ok is True
    assert sleep_calls == [5.0], (
        f"БАГ: ожидалась одна пауза ~5.0с перед повтором, а вызовов sleep было: {sleep_calls}"
    )


def test_do_polish_batch_actually_waits_without_cancel_event(monkeypatch):
    """Тот же баг и тот же фикс в _do_polish_batch (унаследовано при
    написании polish по образцу _do_translate_batch)."""
    import ai_translator
    from ai_translator import _do_polish_batch, TranslatorConfig

    sleep_calls = []
    monkeypatch.setattr(ai_translator.time, "sleep", lambda s: sleep_calls.append(s))

    calls = {"n": 0}

    class FlakyProvider:
        def polish_batch(self, pairs, log_cb=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporary failure")
            return [d for _, d in pairs]

    cfg = TranslatorConfig(max_retries=3, retry_delay=5.0)
    ok, out = _do_polish_batch(FlakyProvider(), [("A", "draftA")], cfg, None, None)
    assert ok is True
    assert sleep_calls == [5.0], (
        f"БАГ: ожидалась одна пауза ~5.0с перед повтором, а вызовов sleep было: {sleep_calls}"
    )


def test_do_translate_batch_wait_is_still_interruptible_with_cancel_event():
    """Убеждаемся что фикс не сломал обратную совместимость: если
    cancel_event ПЕРЕДАН, ожидание должно идти через cancel_event.wait()
    (прерываемо), а не через time.sleep()."""
    from ai_translator import _do_translate_batch, TranslatorConfig
    import threading

    class AlwaysFailsProvider:
        def translate_batch(self, texts, log_cb=None):
            raise RuntimeError("always fails")

    cancel = threading.Event()

    # Отменяем сразу после первой неудачи, до истечения wait_time.
    class TrackedEvent:
        def __init__(self, real):
            self._real = real
            self.wait_calls = []

        def is_set(self):
            return self._real.is_set()

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self._real.set()  # симулируем отмену пользователем во время ожидания
            return True

    tracked = TrackedEvent(cancel)
    cfg = TranslatorConfig(max_retries=3, retry_delay=10.0)
    ok, out = _do_translate_batch(AlwaysFailsProvider(), ["hello"], cfg, None, tracked)
    assert ok is False
    assert len(tracked.wait_calls) == 1, "cancel_event.wait() должен быть вызван ровно один раз"
    assert out == [""]  # отменено -> пустой результат, не зависание на 10с


def test_translate_texts_sequential_request_delay_is_cancel_responsive(monkeypatch):
    """
    БАГ: пауза между батчами (request_delay) в последовательном режиме
    translate_texts была обычным time.sleep(config.request_delay),
    полностью игнорируя cancel_event — нажатие "Отмена" посреди этой паузы
    не действовало, пока пауза не закончится сама. Теперь должен
    вызываться cancel_event.wait(), прерываемый сразу при установке события.
    """
    import ai_translator
    from ai_translator import translate_texts, TranslatorConfig
    import threading

    def fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event):
        return True, [f"t:{t}" for t in batch]

    original = ai_translator._do_translate_batch
    ai_translator._do_translate_batch = fake_do_translate_batch

    sleep_calls = []
    monkeypatch.setattr(ai_translator.time, "sleep", lambda s: sleep_calls.append(s))

    cancel = threading.Event()
    wait_calls = []
    original_wait = cancel.wait
    def tracked_wait(timeout=None):
        wait_calls.append(timeout)
        return original_wait(timeout)
    cancel.wait = tracked_wait

    try:
        cfg = TranslatorConfig(
            provider="openai", api_key="fake-key-for-test",
            batch_size=1, max_workers=1, request_delay=0.01, use_global_tm=False,
        )
        translate_texts(["один", "два", "три"], cfg, cancel_event=cancel)
        assert wait_calls, "БАГ: пауза между батчами должна идти через cancel_event.wait(), а не time.sleep()"
        assert not sleep_calls, f"БАГ: обнаружены вызовы time.sleep() вместо cancel_event.wait(): {sleep_calls}"
    finally:
        ai_translator._do_translate_batch = original


# ═══════════════════════════════════════════════════════════════════════════
# Round 13: реальный краш игры пользователя после Extract→Translate→Inject.
# MZ Plugin Command содержал аргумент-идентификатор картинки/спрайта
# ("巨乳JK_立ち絵" — имя, используемое плагином как ключ для динамического
# JS-свойства/переменной), который извлекался и переводился как обычный
# текст. После инъекции перевода один из плагинов/скриптов, ссылающихся на
# этот идентификатор напрямую (bare identifier в JS), больше не находил его
# — игра падала с "ReferenceError: 巨乳JK_立ち絵 is not defined".
# ═══════════════════════════════════════════════════════════════════════════

def test_should_translate_rejects_underscore_identifier():
    """
    Обнаруженный в реальном проекте краш: строка вида "巨乳JK_立ち絵"
    (идентификатор картинки/спрайта персонажа с подчёркиванием) успешно
    проходила should_translate() как обычный переводимый текст. Обычный
    японский текст (диалоги, имена, системные строки) практически никогда
    не содержит буквального ASCII-подчёркивания — это почти всегда признак
    имени файла, ключа или идентификатора, а не прозы.
    """
    from engine.text_filter import should_translate

    # Реальный кейс, вызвавший краш игры.
    assert should_translate("巨乳JK_立ち絵") is False
    assert should_translate("キャラA_立ち絵") is False
    assert should_translate("Actor1_face") is False
    assert should_translate("chara_001") is False

    # Обычный диалог/текст должен продолжать переводиться как раньше.
    assert should_translate("こんにちは") is True
    assert should_translate("勇者よ、旅に出るのだ。") is True
    assert should_translate("はい") is True
    assert should_translate("いいえ、それは違います。") is True


def test_should_translate_recognizes_fullwidth_latin_as_letters():
    """
    БАГ: HAS_LETTERS не включал полноширинную латиницу (Ａ-Ｚ, ａ-ｚ —
    U+FF21-FF3A/U+FF41-FF5A) — распространённое японское типографское
    соглашение (fullwidth roman) для визуального выравнивания с окружающим
    японским текстом (звукоподражания, стилизованные надписи). Строка,
    состоящая ТОЛЬКО из такой латиницы, молча классифицировалась как
    "no_letters" и никогда не попадала в extract — без единого следа в
    логах, что напрямую противоречит запросу "находить ошибки точнее".
    """
    from engine.text_filter import classify_text_for_translation, should_translate

    # Обычное слово полноширинной латиницей — должно переводиться.
    # ("ＴＥＳＴ" не годится для этой проверки: 4 буквы — само попадает под
    # правило аббревиатур ниже, как и ASCII "TEST" уже вело себя раньше.)
    assert should_translate("ＷＥＬＣＯＭＥ") is True   # 7 букв, вне диапазона аббревиатур
    assert should_translate("ＷＨＡＭ！") is True         # звукоподражание с пунктуацией

    # НО короткие полноширинные аббревиатуры (ＭＰ/ＨＰ) — та же логика,
    # что и для ASCII HP/MP: это стат-лейблы, не нужно переводить.
    ok, reason = classify_text_for_translation("ＭＰ")
    assert ok is False and reason == "abbreviation"
    ok2, reason2 = classify_text_for_translation("ＨＰ２")
    assert ok2 is False and reason2 == "abbreviation"

    # Контроль: обычный ASCII-кейс не должен был сломаться.
    assert should_translate("HP") is False
    assert should_translate("TEST") is False  # уже было так и раньше (та же эвристика)
    assert should_translate("こんにちは") is True


def test_mz_plugin_command_identifier_argument_is_not_extracted():
    """
    Сквозная проверка через реальный extractor.py: MZ Plugin Command (357)
    с аргументом-идентификатором картинки не должен извлекаться как
    переводимый текст. Именно так этот баг проявился в реальной игре —
    через plugin_cmd_arg, а не через обычный диалог.
    """
    import json
    from engine.extractor import RPGMVExtractor

    event_list = [
        {
            "code": 357, "indent": 0,
            "parameters": [
                "PictureCharaShow", "PictureCharaShow", "show",
                json.dumps({"charaName": "巨乳JK_立ち絵", "message": "こんにちは！"},
                           ensure_ascii=False),
            ],
        },
    ]

    ext = RPGMVExtractor("/tmp")
    ext._extract_event_list(event_list, "Map001.json", "events[1].pages[0].list")

    extracted_originals = [e.original for e in ext.entries]
    assert "巨乳JK_立ち絵" not in extracted_originals, (
        "БАГ: идентификатор картинки/спрайта извлечён как переводимый текст — "
        "перевод и повторная инъекция сломают ссылку на него в скрипте плагина"
    )
    # Но обычный текстовый аргумент того же plugin command должен по-прежнему
    # извлекаться — фикс не должен ломать легитимные переводимые аргументы.
    assert "こんにちは！" in extracted_originals, (
        "БАГ: фикс слишком агрессивен — обычный текстовый аргумент плагина "
        "перестал извлекаться вместе с идентификатором"
    )


def test_renpy_should_translate_rejects_underscore_identifier():
    """
    Ren'Py-экстрактор использует ОТДЕЛЬНУЮ, не общую с остальными движками
    реализацию _should_translate() — фикс в engine/text_filter.py её не
    затрагивает автоматически. Та же уязвимость: тег изображения вида
    "image имя_варианта = ..." в Ren'Py ссылается на себя по имени в разных
    местах скрипта (show/hide/at), перевод одного вхождения без другого
    рассинхронит ссылку так же, как в реальном MZ-краше.
    """
    from renpy_extractor import _should_translate

    assert _should_translate("巨乳JK_立ち絵") is False
    assert _should_translate("chara_normal") is False
    assert _should_translate("bg_room_night") is False

    assert _should_translate("こんにちは") is True
    assert _should_translate("勇者よ、旅に出るのだ。") is True


def test_vxace_orphaned_dialogue_line_is_not_silently_lost():
    """
    В engine/vxace_extractor.py сбор строк диалога (код 101 + следующие 401)
    обрывался целиком, если МЕЖДУ заголовком 101 и первой строкой 401
    оказывалась любая другая команда (например 408) — весь диалоговый блок
    молча терялся: ноль извлечённых строк, ноль предупреждений. Теперь
    "осиротевшие" 401/405 команды всё равно извлекаются — по отдельности,
    хуже сгруппированного случая, но без потери текста.

    Проверяет ПОЛНЫЙ цикл extract → inject, а не только экстракцию: первая
    версия этого фикса извлекала "осиротевшие" строки с путём вида
    "list[2].dialog" (голый индекс) — но vxace_injector._apply_translation()
    для .dialog/.scroll ищет именно ('range', (start,end)) шаг перед
    последним, и голый индекс там не распознаётся, поэтому инъекция
    проваливалась на 100% таких строк (перевод "терялся" на шаге инъекции
    вместо шага экстракции). Путь должен быть self-range "[i..i]".
    """
    from rubymarshal.classes import RubyObject, RubyString
    from engine.vxace_extractor import VXAceExtractor
    from engine.vxace_injector import _apply_translation

    def make_cmd(code, params):
        return RubyObject("RPG::EventCommand", {"@code": code, "@indent": 0, "@parameters": params})

    # Обычный случай (101 сразу за которым 401) должен продолжать работать
    # сгруппированным, как раньше.
    normal = [
        make_cmd(101, ["", 0, 0, 2]),
        make_cmd(401, [RubyString("Привет,")]),
        make_cmd(401, [RubyString("путник!")]),
    ]
    ext_normal = VXAceExtractor("/tmp")
    ext_normal._extract_event_list("Map.rvdata2", "events[1].pages[0].list", normal)
    assert len(ext_normal.entries) == 1, "Обычный случай должен остаться сгруппирован в одну запись"
    assert ext_normal.entries[0]["original"] == "Привет,\nпутник!"

    # Патологический случай: что-то встряло между 101 и первой 401.
    with_gap = [
        make_cmd(101, ["", 0, 0, 2]),
        make_cmd(408, [RubyString("что-то постороннее")]),
        make_cmd(401, [RubyString("Привет,")]),
        make_cmd(401, [RubyString("путник!")]),
    ]
    ext_gap = VXAceExtractor("/tmp")
    ext_gap._extract_event_list("Map.rvdata2", "list", with_gap)
    extracted_texts = [e["original"] for e in ext_gap.entries]
    assert extracted_texts, "БАГ: диалог полностью потерян — ноль извлечённых строк"
    assert "Привет," in extracted_texts, f"БАГ: текст диалога потерян при обрыве сбора: {extracted_texts!r}"
    assert "путник!" in extracted_texts, f"БАГ: текст диалога потерян при обрыве сбора: {extracted_texts!r}"

    # Полный цикл: инъекция извлечённых "осиротевших" строк должна реально
    # применяться, а не молча проваливаться из-за неверного формата пути.
    page = RubyObject("RPG::Event::Page", {"@list": with_gap})
    for e in ext_gap.entries:
        ok = _apply_translation(page, e["path"], e["original"], f"[ПЕРЕВОД] {e['original']}")
        assert ok, f"БАГ: инъекция провалилась для осиротевшей строки, путь={e['path']!r}"

    injected_texts = [
        cmd.attributes.get("@parameters", [None])[0].text
        for cmd in with_gap if cmd.attributes.get("@code") == 401
    ]
    assert injected_texts == ["[ПЕРЕВОД] Привет,", "[ПЕРЕВОД] путник!"], (
        f"БАГ: перевод не применился корректно после инъекции: {injected_texts!r}"
    )

    # Та же проверка для "осиротевшей" строки прокручиваемого текста (405) —
    # использует тот же self-range путь и ту же логику фикса.
    scroll_with_gap = [
        make_cmd(105, ["", 2, False]),
        make_cmd(408, [RubyString("постороннее")]),
        make_cmd(405, [RubyString("Прокручиваемый")]),
        make_cmd(405, [RubyString("текст")]),
    ]
    ext_scroll = VXAceExtractor("/tmp")
    ext_scroll._extract_event_list("Map.rvdata2", "list", scroll_with_gap)
    assert [e["original"] for e in ext_scroll.entries] == ["Прокручиваемый", "текст"], (
        "БАГ: осиротевшая строка scroll-текста потеряна"
    )
    scroll_page = RubyObject("RPG::Event::Page", {"@list": scroll_with_gap})
    for e in ext_scroll.entries:
        ok = _apply_translation(scroll_page, e["path"], e["original"], f"[ПЕРЕВОД] {e['original']}")
        assert ok, f"БАГ: инъекция scroll-текста провалилась, путь={e['path']!r}"


def test_mv_dialogue_line_between_header_and_text_is_not_silently_lost():
    """
    Тот же класс бага, что и в vxace_extractor.py (см. соседний тест выше),
    но в engine/extractor.py (RPG Maker MV/MZ): сбор строк диалога (101 +
    следующие 401) обрывался целиком, если МЕЖДУ заголовком и первой
    строкой оказывалась любая другая команда (например 108 — комментарий
    разработчика) — весь диалог молча терялся. Проверяет полный цикл
    extract → inject (путь обязан быть self-range "[i..i]", а не голым
    индексом — injector._inject_dialog() матчит только "[N..M].dialog").
    """
    from engine.extractor import RPGMVExtractor
    from engine.injector import _inject_dialog

    # Обычный случай — должен остаться сгруппирован, как раньше.
    normal = [
        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
        {"code": 401, "indent": 0, "parameters": ["Привет,"]},
        {"code": 401, "indent": 0, "parameters": ["путник!"]},
    ]
    ext_normal = RPGMVExtractor("/tmp")
    ext_normal._extract_event_list(normal, "Map001.json", "events[1].pages[0].list")
    assert len(ext_normal.entries) == 1
    assert ext_normal.entries[0].original == "Привет,\nпутник!"

    # Патологический случай: комментарий разработчика (108) между 101 и 401.
    with_gap = [
        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
        {"code": 108, "indent": 0, "parameters": ["комментарий разработчика"]},
        {"code": 401, "indent": 0, "parameters": ["Привет,"]},
        {"code": 401, "indent": 0, "parameters": ["путник!"]},
    ]
    ext_gap = RPGMVExtractor("/tmp")
    ext_gap._extract_event_list(with_gap, "Map001.json", "events[1].pages[0].list")
    extracted = [e.original for e in ext_gap.entries]
    assert "Привет," in extracted, f"БАГ: диалог потерян: {extracted!r}"
    assert "путник!" in extracted, f"БАГ: диалог потерян: {extracted!r}"

    data = {"events": {"1": {"pages": [{"list": with_gap}]}}}
    for e in ext_gap.entries:
        ok = _inject_dialog(data, e.path, f"[ПЕРЕВОД] {e.original}")
        assert ok, f"БАГ: инъекция провалилась для осиротевшей строки, путь={e.path!r}"

    injected = [cmd["parameters"][0] for cmd in with_gap if cmd["code"] == 401]
    assert injected == ["[ПЕРЕВОД] Привет,", "[ПЕРЕВОД] путник!"], (
        f"БАГ: перевод применился некорректно: {injected!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Round 14: второй реальный краш игры пользователя, найден сразу после
# исправления первого (ReferenceError). На этот раз — SyntaxError. Строка
# script_literal (355/655) — это ПОДСТРОКА внутри более крупной строки
# JS-кода, но инъекция обрабатывала её как generic-путь и заменяла ВСЮ
# строку кода одним переводом целиком, теряя весь окружающий JS.
# ═══════════════════════════════════════════════════════════════════════════

def test_script_literal_injection_preserves_surrounding_js_code():
    """
    Реальный краш игры: '$gameMessage.add("こんにちは、旅人よ！");' после
    перевода становился просто 'Привет, путник!' — вся обвязка JS-кода
    терялась, потому что путь "...[i].parameters[0]" не имел собственной
    логики инъекции и проваливался в generic _set_nested(), заменяющий
    ВСЁ значение по пути. Игра падала с "SyntaxError: Invalid or
    unexpected token" при попытке выполнить получившийся мусор как код.
    """
    from engine.extractor import RPGMVExtractor
    from engine.injector import _apply_entry

    event_list = [
        {"code": 355, "indent": 0, "parameters": ['$gameMessage.add("こんにちは、旅人よ！");']},
    ]
    ext = RPGMVExtractor("/tmp")
    ext._extract_event_list(event_list, "Map001.json", "events[1].pages[0].list")
    assert len(ext.entries) == 1
    assert ext.entries[0].context == "script_literal"

    data = {"events": {"1": {"pages": [{"list": event_list}]}}}
    e = ext.entries[0]
    ok = _apply_entry(data, e.path, "Привет, путник!", e.original)
    assert ok

    result_line = event_list[0]["parameters"][0]
    assert result_line == '$gameMessage.add("Привет, путник!");', (
        f"БАГ: окружающий JS-код потерян, итог: {result_line!r}"
    )


def test_script_literal_injection_escapes_quotes_in_translation():
    """
    Если перевод сам содержит символ кавычки того же типа, которым была
    обёрнута исходная строка (например ИИ перевёл японские кавычки 「」
    обычными ASCII-кавычками), нужно экранировать — иначе кавычка внутри
    перевода преждевременно закроет JS-строку и сломает синтаксис остатка
    строки тем же способом, что и исходный баг.
    """
    from engine.extractor import RPGMVExtractor
    from engine.injector import _apply_entry

    event_list = [
        {"code": 355, "indent": 0, "parameters": ['$gameMessage.add("彼は「やあ」と言った");']},
    ]
    ext = RPGMVExtractor("/tmp")
    ext._extract_event_list(event_list, "Map001.json", "events[1].pages[0].list")
    e = ext.entries[0]

    data = {"events": {"1": {"pages": [{"list": event_list}]}}}
    ok = _apply_entry(data, e.path, 'Он сказал "привет"', e.original)
    assert ok

    result_line = event_list[0]["parameters"][0]
    # Ровно 4 кавычки: открывающая/закрывающая всей JS-строки + 2 экранированные внутри.
    assert result_line == '$gameMessage.add("Он сказал \\"привет\\"");', (
        f"БАГ: кавычка не экранирована, синтаксис сломан: {result_line!r}"
    )


def test_script_literal_injection_escapes_backslash_and_newline():
    """Обратный слэш и перенос строки в переводе тоже должны экранироваться —
    иначе тот же класс синтаксической поломки JS-строки."""
    from engine.extractor import RPGMVExtractor
    from engine.injector import _apply_entry

    event_list = [
        {"code": 355, "indent": 0, "parameters": ['$gameMessage.add("こんにちは");']},
    ]
    ext = RPGMVExtractor("/tmp")
    ext._extract_event_list(event_list, "Map001.json", "events[1].pages[0].list")
    e = ext.entries[0]

    data = {"events": {"1": {"pages": [{"list": event_list}]}}}
    ok = _apply_entry(data, e.path, "Привет\nдруг \\ путник", e.original)
    assert ok

    result_line = event_list[0]["parameters"][0]
    assert result_line == '$gameMessage.add("Привет\\nдруг \\\\ путник");', (
        f"БАГ: перенос строки/бэкслеш не экранированы: {result_line!r}"
    )


def test_script_literal_injection_handles_multiple_strings_on_same_line():
    """
    Если в одной строке скрипта — два отдельных переводимых литерала (одна
    и та же строка-путь "...parameters[0]" для обеих записей, различаются
    только original) — обе замены должны примениться корректно и не
    затереть друг друга.
    """
    from engine.extractor import RPGMVExtractor
    from engine.injector import _apply_entry

    line = 'if (x) { $gameMessage.add("こんにちは"); } else { $gameMessage.add("さようなら"); }'
    event_list = [{"code": 355, "indent": 0, "parameters": [line]}]
    ext = RPGMVExtractor("/tmp")
    ext._extract_event_list(event_list, "Map001.json", "events[1].pages[0].list")
    assert len(ext.entries) == 2

    data = {"events": {"1": {"pages": [{"list": event_list}]}}}
    translations = {"こんにちは": "Привет", "さようなら": "Пока"}
    for e in ext.entries:
        ok = _apply_entry(data, e.path, translations[e.original], e.original)
        assert ok, f"инъекция провалилась для {e.original!r}"

    result_line = event_list[0]["parameters"][0]
    assert result_line == (
        'if (x) { $gameMessage.add("Привет"); } else { $gameMessage.add("Пока"); }'
    ), f"БАГ: одна из двух строк на той же строке кода не применилась корректно: {result_line!r}"


def test_script_literal_injection_does_not_corrupt_longer_string_containing_shorter_one():
    """
    Найден при проверке краевых случаев сразу после предыдущего фикса (Round
    14): голый .find(original) без привязки к границам кавычек находил
    original как ПОДСТРОКУ более длинной, ОТДЕЛЬНОЙ цитируемой строки, если
    та встречалась раньше в той же строке кода — например
    'a.add("こんにちは、世界"); b.add("こんにちは");' — инъекция перевода
    для короткой "こんにちは" портила часть длинной "こんにちは、世界",
    оставляя настоящую цель (в b.add(...)) непереведённой. Теперь ищем
    сначала anchored-вариант "кавычка+original+та_же_кавычка".

    ВАЖНО: короткую запись нужно инжектировать ПЕРВОЙ, пока длинная строка
    ещё не тронута (всё ещё японская) — именно так баг воспроизводится.
    Если инжектировать в порядке извлечения (длинная первая), длинная
    строка перестаёт содержать короткую как подстроку ДО того, как для
    короткой вообще случится инъекция, и баг случайно не проявляется —
    первая версия этого теста ошибочно проходила по этой причине.
    """
    from engine.extractor import RPGMVExtractor
    from engine.injector import _apply_entry

    line = 'a.add("こんにちは、世界"); b.add("こんにちは");'
    event_list = [{"code": 355, "indent": 0, "parameters": [line]}]
    ext = RPGMVExtractor("/tmp")
    ext._extract_event_list(event_list, "Map001.json", "events[1].pages[0].list")
    assert len(ext.entries) == 2

    entries_by_original = {e.original: e for e in ext.entries}
    assert "こんにちは、世界" in entries_by_original
    assert "こんにちは" in entries_by_original

    data = {"events": {"1": {"pages": [{"list": event_list}]}}}

    # Короткая — ПЕРВОЙ, пока длинная строка всё ещё японская.
    short_entry = entries_by_original["こんにちは"]
    ok_short = _apply_entry(data, short_entry.path, "ПРИВЕТ", short_entry.original)
    assert ok_short

    long_entry = entries_by_original["こんにちは、世界"]
    ok_long = _apply_entry(data, long_entry.path, "ПРИВЕТ_МИР", long_entry.original)
    assert ok_long

    result_line = event_list[0]["parameters"][0]
    assert result_line == 'a.add("ПРИВЕТ_МИР"); b.add("ПРИВЕТ");', (
        f"БАГ: короткая строка испортила часть более длинной вместо своего "
        f"собственного вхождения: {result_line!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Round 15: третий реальный краш из живого тестирования пользователя. На
# этот раз — не ReferenceError и не SyntaxError, а тихий сбой: плагин
# управления мимикой лица не находит анимацию по переведённому ключу и
# логирует "Error: Animation not found: <переведённый_ключ>", а нужное
# выражение лица просто не проигрывается. Ключ — это тег категории в
# бракетах + конкретное имя состояния без пробела/пунктуации между ними
# (типичная конвенция "layered facial expression" плагинов, например
# "【頬】薄い赤面" — "[Щёки]лёгкий_румянец"), который выглядит как
# обычный текст, но извлекается и переводится как диалог.
# ═══════════════════════════════════════════════════════════════════════════

def test_should_translate_rejects_bracket_tag_identifier():
    """
    Реальный краш: ключ поиска анимации мимики вида "【頬】薄い赤面"
    переводился как обычный текст, что рассинхронизировало его с
    внутренним реестром анимаций плагина ("Error: Animation not found").
    Правило намеренно узкое: скобка должна стоять В НАЧАЛЕ строки, а хвост
    после закрывающей скобки — не содержать пробелов/пунктуации предложения
    и не быть пустым. Обычная проза, где 【】 используется как акцент в
    середине предложения или где дальше идёт полноценное предложение с
    пунктуацией, должна оставаться переводимой.
    """
    from engine.text_filter import should_translate

    # Реальный кейс краша и однотипные варианты (тег категории + имя состояния).
    assert should_translate("【頬】薄い赤面") is False
    assert should_translate("【目】笑い") is False
    assert should_translate("【口】怒り") is False
    assert should_translate("【表情】通常") is False

    # Голый лейбл без значения после — недостаточно уверенности, что это
    # идентификатор, а не короткий переводимый заголовок; должен остаться
    # переводимым.
    assert should_translate("【プロローグ】") is True

    # Обычная проза с 【】 НЕ в начале строки (акцент в середине предложения)
    # — не должна блокироваться.
    assert should_translate("先生は【重要】と言った。") is True
    assert should_translate("『あなたのことが【好き】よ』と彼女は言った。") is True

    # Скобка В НАЧАЛЕ строки, но дальше идёт полноценное предложение с
    # пунктуацией — это стилистический акцент в прозе, а не идентификатор;
    # не должна блокироваться.
    assert should_translate("【隠しダンジョン】への入り口を見つけた。") is True
    assert should_translate("【注意】このダンジョンは危険です。") is True

    # Обычный диалог без скобок вообще — не задет фиксом.
    assert should_translate("こんにちは、旅人よ！") is True

    # Предыдущий фикс (подчёркивание-идентификатор) не должен был сломаться.
    assert should_translate("巨乳JK_立ち絵") is False


def test_mz_plugin_command_bracket_tag_argument_is_not_extracted():
    """
    Сквозная проверка через реальный extractor.py: если тег-идентификатор
    вида "【頬】薄い赤面" встречается как значение аргумента Plugin Command
    (типичное место для таких ключей — вызов смены выражения лица), он не
    должен извлекаться как переводимый текст, при этом обычный текстовый
    аргумент того же plugin command должен продолжать извлекаться.
    """
    import json
    from engine.extractor import RPGMVExtractor

    event_list = [
        {
            "code": 357, "indent": 0,
            "parameters": [
                "FaceExpressionChange", "FaceExpressionChange", "change",
                json.dumps({"expressionKey": "【頬】薄い赤面", "message": "こんにちは！"},
                           ensure_ascii=False),
            ],
        },
    ]

    ext = RPGMVExtractor("/tmp")
    ext._extract_event_list(event_list, "Map001.json", "events[1].pages[0].list")

    extracted_originals = [e.original for e in ext.entries]
    assert "【頬】薄い赤面" not in extracted_originals, (
        "БАГ: ключ анимации мимики извлечён как переводимый текст — "
        "перевод рассинхронизирует его с реестром анимаций плагина"
    )
    assert "こんにちは！" in extracted_originals, (
        "БАГ: фикс слишком агрессивен — обычный текстовый аргумент плагина "
        "перестал извлекаться вместе с ключом"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Round 16: глубокий аудит Global TM (SQLite, общая база между ВСЕМИ
# проектами — цена ошибки здесь выше, чем в одном файле одной игры).
# ═══════════════════════════════════════════════════════════════════════════

def test_global_tm_close_commits_pending_writes_without_explicit_flush(tmp_path, monkeypatch):
    """
    GlobalTM.set() коммитит только каждые 200 записей (self._writes % 200).
    Python sqlite3 НЕ коммитит незафиксированные изменения при close() —
    откатывает их. close() раньше вызывал только self._conn.close() без
    предварительного commit — любая сессия с < 200 записей, дошедшая до
    close() БЕЗ явного предварительного flush(), молча теряла эти записи.

    Особенно коварно, что собственный докстринг класса демонстрирует
    "with GlobalTM(...) as tm: ... tm.set(...)" как штатный способ
    использования — а __exit__ вызывал только close(), никогда flush().
    Значит следование ДОКУМЕНТИРОВАННОМУ паттерну само по себе было
    небезопасным. Теперь close() сам делает commit перед закрытием —
    какой бы путь к close() ни использовался.
    """
    import global_tm
    monkeypatch.setattr(global_tm.GlobalTM, "DB_PATH", tmp_path / "test_tm.db")

    # Ровно паттерн из докстринга класса: with-контекст, запись внутри,
    # БЕЗ единого явного вызова flush().
    with global_tm.GlobalTM(target_lang="Russian") as tm:
        for i in range(50):  # заведомо меньше порога коммита (200)
            tm.set(f"текст{i}", f"перевод{i}")

    import sqlite3
    raw = sqlite3.connect(str(tmp_path / "test_tm.db"))
    count = raw.execute("SELECT COUNT(*) FROM tm WHERE target_lang='Russian'").fetchone()[0]
    raw.close()
    assert count == 50, (
        f"БАГ: {50 - count} из 50 записей потеряны — with-блок из докстринга "
        f"класса не сохранил данные без явного flush()"
    )


def test_global_tm_direct_close_without_flush_still_persists(tmp_path, monkeypatch):
    """То же самое, но при прямом .open()/.close() (не через with-контекст) —
    защита должна работать одинаково независимо от способа закрытия."""
    import global_tm
    monkeypatch.setattr(global_tm.GlobalTM, "DB_PATH", tmp_path / "test_tm.db")

    tm = global_tm.GlobalTM(target_lang="English").open()
    for i in range(30):
        tm.set(f"other{i}", f"translated{i}")
    tm.close()  # намеренно БЕЗ вызова flush() перед close()

    import sqlite3
    raw = sqlite3.connect(str(tmp_path / "test_tm.db"))
    count = raw.execute("SELECT COUNT(*) FROM tm WHERE target_lang='English'").fetchone()[0]
    raw.close()
    assert count == 30, f"БАГ: {30 - count} из 30 записей потеряны при close() без flush()"


def test_config_load_does_not_invert_string_booleans(tmp_path, monkeypatch):
    """
    БАГ: bool("false") в Python возвращает True — любая непустая строка
    truthy, содержимое не проверяется. load_config() наивно приводил
    значения к типу дефолта через type(default_val)(val); для булевых
    полей (apply_warp, auto_game_profile, use_context) это означало, что
    config.json с "apply_warp": "false" (строка в кавычках — естественная
    ошибка при ручном редактировании, если человек привык к другим
    форматам конфигов) молча ИНВЕРТИРОВАЛСЯ в True — ровно противоположное
    тому, что написал пользователь.
    """
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "cfg.json")

    config.CONFIG_PATH.write_text(
        json.dumps({"apply_warp": "false", "auto_game_profile": "0", "use_context": "no"}),
        encoding="utf-8",
    )
    loaded = config.load_config()
    assert loaded["apply_warp"] is False, (
        f"БАГ: 'false' в конфиге стал {loaded['apply_warp']!r} вместо False"
    )
    assert loaded["auto_game_profile"] is False
    assert loaded["use_context"] is False


def test_config_load_recognizes_string_true_variants(tmp_path, monkeypatch):
    """Симметричная проверка: строковые true-варианты должны корректно
    становиться Python True, а не оставаться строкой."""
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "cfg.json")

    config.CONFIG_PATH.write_text(
        json.dumps({"apply_warp": "true", "auto_game_profile": "1", "use_context": "yes"}),
        encoding="utf-8",
    )
    loaded = config.load_config()
    assert loaded["apply_warp"] is True
    assert loaded["auto_game_profile"] is True
    assert loaded["use_context"] is True


def test_config_load_normal_json_booleans_unaffected(tmp_path, monkeypatch):
    """Штатный путь (правильный JSON true/false из save_config()) не должен
    быть затронут фиксом — это самый частый случай."""
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "cfg.json")

    config.CONFIG_PATH.write_text(
        json.dumps({"apply_warp": True, "auto_game_profile": False}),
        encoding="utf-8",
    )
    loaded = config.load_config()
    assert loaded["apply_warp"] is True
    assert loaded["auto_game_profile"] is False


def test_config_load_unrecognized_bool_string_falls_back_to_default(tmp_path, monkeypatch):
    """Нераспознанная строка (не false/0/no/true/1/yes) не должна угадываться
    наугад — безопаснее откатиться к дефолтному значению."""
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "cfg.json")

    config.CONFIG_PATH.write_text(json.dumps({"apply_warp": "maybe"}), encoding="utf-8")
    loaded = config.load_config()
    assert loaded["apply_warp"] == config.DEFAULTS["apply_warp"]


# ─────────────────────────────────────────────────────────────────────────────
# Bugs (v50, обнаружены при readiness-аудите Unity-движка):
#   1) CSV extract/inject теряли embedded-переносы строк в quoted-ячейках
#   2) stale-extract detection не покрывал Unity-специфичные форматы
#   3) openpyxl использовался, но не был объявлен в requirements.txt
#   4) 3 из 6 unity_* контекстов не имели подсказки для AI-промпта
# ─────────────────────────────────────────────────────────────────────────────

def _make_unity_project(tmp: Path) -> tuple[Path, Path]:
    """Общий helper: минимальный распознаваемый Unity-проект + пустой output_dir."""
    game_dir = tmp / "MyGame_Data"
    (game_dir / "StreamingAssets").mkdir(parents=True)
    (tmp / "ProjectSettings").mkdir()
    (tmp / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1")
    out_dir = tmp / "out"
    out_dir.mkdir()
    return game_dir, out_dir


def test_unity_csv_extract_preserves_embedded_newline_in_quoted_cell():
    """
    БАГ: _extract_csv строил csv.reader(text.splitlines(), ...) —
    .splitlines() резало файл на физические строки ДО того, как CSV-парсер
    его увидел, поэтому quoted-ячейка с реальным переносом строки внутри
    (частый случай — многострочный диалог в Unity-локализации) теряла "\n"
    молча уже на этапе извлечения, и следующая строка файла могла съехать
    по индексу.
    """
    from engine.unity_extractor import extract_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir, out_dir = _make_unity_project(tmp)

        csv_file = game_dir / "StreamingAssets" / "dialogue.csv"
        csv_file.write_bytes(
            'Key,JA\r\nGREET,"こんにちは、\n旅人よ"\r\nBYE,さようなら\r\n'.encode("utf-8")
        )

        extract_unity(tmp, out_dir, log_callback=lambda m: None)
        ext_dir = out_dir / "extracted"
        jf = next(f for f in ext_dir.glob("*.json") if "dialogue" in f.name)
        entries = json.loads(jf.read_text(encoding="utf-8"))

        greet = next(e for e in entries if "こんにちは" in e["original"])
        assert greet["original"] == "こんにちは、\n旅人よ", (
            f"БАГ: embedded newline потерян при извлечении: {greet['original']!r}"
        )
        assert any(e["original"] == "さようなら" for e in entries), (
            "БАГ: строка после multi-line ячейки не извлечена — разрыв строк CSV сбил парсинг"
        )


def test_unity_csv_inject_roundtrips_multiline_cell_and_stays_valid_csv():
    """
    БАГ: тот же splitlines()-баг был и в unity_injector.py при повторном
    чтении файла для вставки. Инъекция перезаписывает ВСЮ CSV-таблицу
    (writerows(rows)), а не только изменённые ячейки, поэтому ЛЮБАЯ
    multi-line ячейка в файле теряла "\n" при каждом inject-проходе —
    даже колонка NOTE, которую вообще не переводили в этом батче.

    (Row-count при этом не менялся — csv.reader продолжает поглощать
    элементы списка как продолжение открытой quoted-ячейки — поэтому
    сам перевод обычно попадал в правильную ячейку и раньше; ломалось
    именно содержимое непереведённых multi-line ячеек при round-trip.)
    """
    import csv as csv_mod
    import io as io_mod
    from engine.unity_extractor import extract_unity
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir, out_dir = _make_unity_project(tmp)

        csv_file = game_dir / "StreamingAssets" / "dialogue.csv"
        csv_file.write_bytes(
            ('Key,JA,NOTE\r\n'
             'GREET,"こんにちは、\n旅人よ","開発メモ\n二行目"\r\n'
             'BYE,さようなら,オーケー\r\n').encode("utf-8")
        )

        extract_unity(tmp, out_dir, log_callback=lambda m: None)
        ext_dir = out_dir / "extracted"
        jf = next(f for f in ext_dir.glob("*.json") if "dialogue" in f.name)
        entries = json.loads(jf.read_text(encoding="utf-8"))

        # Переводим только JA-колонку; multi-line NOTE ("開発メモ\n二行目")
        # намеренно оставляем непереведённой (translated == "") — как
        # частично готовый батч.
        for e in entries:
            if "こんにちは" in e["original"]:
                e["translated"] = "Привет,\nпутник"
            elif e["original"] == "さようなら":
                e["translated"] = "Пока"
        (ext_dir / (jf.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_unity(tmp, out_dir, backup=True)
        final_text = csv_file.read_text(encoding="utf-8")
        rows = list(csv_mod.reader(io_mod.StringIO(final_text)))

        assert rows[0] == ["Key", "JA", "NOTE"]
        assert rows[1][1] == "Привет,\nпутник", (
            f"Перевод не встал в правильную multi-line ячейку: {rows[1][1]!r}"
        )
        assert rows[1][2] == "開発メモ\n二行目", (
            f"БАГ: непереведённая multi-line ячейка потеряла перенос строки "
            f"при round-trip через инъекцию: {rows[1][2]!r}"
        )
        assert rows[2] == ["BYE", "Пока", "オーケー"]


def test_unity_stale_extract_detects_change_in_non_json_source_format(tmp_path, monkeypatch):
    """
    БАГ: _compute_stale_extract() в pipeline.py использовал data_exts =
    {".json", ".rpy", ".ks", ".wolf", ".rvdata2", ".mps", ".dat"} — набор
    расширений ДО-Unity движков. unity_extractor.py читает ещё .csv/.tsv/
    .xml/.yaml/.yml/.po/.txt/.md (см. TEXT_EXTS), но они так и не попали в
    data_exts. Правка Unity YAML/PO/XML/CSV/TXT-файла после Extract молча
    НЕ показывала предупреждение «экстракт устарел» (показывала только
    для .json).
    """
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    out_dir = tmp_path / "output"
    (game_dir / "MyGame_Data" / "StreamingAssets").mkdir(parents=True)
    (game_dir / "ProjectSettings").mkdir()
    (game_dir / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1")
    out_dir.mkdir()

    yaml_file = game_dir / "MyGame_Data" / "StreamingAssets" / "strings.yaml"
    yaml_file.write_text("greeting: こんにちは\n", encoding="utf-8")

    monkeypatch.setattr(Pipeline, "_STALE_CHECK_THROTTLE_SEC", 0)
    Pipeline._stale_check_cache.clear()

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    ok, _ = p.extract()
    assert ok
    assert p.get_stats().get("stale_extract") is False

    time.sleep(1.1)
    yaml_file.write_text("greeting: おはよう\n", encoding="utf-8")
    Pipeline._stale_check_cache.clear()
    assert p.get_stats().get("stale_extract") is True, (
        "БАГ: изменение .yaml-источника Unity не помечено как устаревший экстракт"
    )


def test_stale_extract_extension_check_scoped_to_unity_only(tmp_path, monkeypatch):
    """
    Регрессия для фикса выше: расширенный data_exts не должен задевать
    остальные движки — .txt/.md в папке RPG Maker/Godot/etc. игры (readme,
    лицензии) никогда не были частью translatable-данных и не должны
    вызывать ложное «экстракт устарел».
    """
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    out_dir = tmp_path / "output"
    data_dir = game_dir / "data"
    js_dir = game_dir / "js"
    data_dir.mkdir(parents=True)
    js_dir.mkdir(parents=True)
    out_dir.mkdir()

    (data_dir / "System.json").write_text(json.dumps({"gameTitle": "T"}), encoding="utf-8")
    (data_dir / "Actors.json").write_text(
        json.dumps([None, {"id": 1, "name": "こんにちは"}], ensure_ascii=False), encoding="utf-8"
    )
    (js_dir / "rpg_core.js").write_text("// core", encoding="utf-8")
    readme = game_dir / "readme.txt"
    readme.write_text("v1.0", encoding="utf-8")

    monkeypatch.setattr(Pipeline, "_STALE_CHECK_THROTTLE_SEC", 0)
    Pipeline._stale_check_cache.clear()

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    ok, _ = p.extract()
    assert ok
    index = json.loads((out_dir / "extract_index.json").read_text())
    assert index["game_type"] == "rpgmv"

    time.sleep(1.1)
    readme.write_text("v1.1", encoding="utf-8")
    Pipeline._stale_check_cache.clear()
    assert p.get_stats().get("stale_extract") is False, (
        "РЕГРЕССИЯ: .txt теперь ошибочно триггерит stale_extract для не-Unity движка"
    )


def test_requirements_txt_declares_openpyxl_for_xlsx_export():
    """
    БАГ: rpgmtl_desktop.py импортирует openpyxl (см. «Экспорт для
    переводчика», .xlsx) и это покрыто тестом
    test_xlsx_export_preserves_rpg_escape_codes, но requirements.txt не
    объявлял эту зависимость — `pip install -r requirements.txt` не давал
    рабочую фичу из коробки, только рантайм-диалог «openpyxl не
    установлен» при первом клике «Экспорт XLSX».
    """
    req_path = Path(__file__).parent.parent / "requirements.txt"
    content = req_path.read_text(encoding="utf-8")
    assert re.search(r"^openpyxl\b", content, re.MULTILINE), (
        "БАГ: openpyxl отсутствует в requirements.txt, хотя используется в rpgmtl_desktop.py"
    )


def test_unity_all_extraction_contexts_have_translation_hint_labels():
    """
    БАГ: unity_extractor.py производит 6 разных context-значений
    (unity_text/unity_json/unity_csv/unity_xml/unity_po/unity_yaml), но
    _CTX_LABELS в ai_translator.py знал только про первые три — для
    unity_xml/unity_po/unity_yaml _ctx_label() падал в fallback-цепочку и
    возвращал сырую строку контекста вместо человекочитаемой подсказки типа
    контента для AI-промпта.
    """
    from ai_translator import _ctx_label

    unity_contexts = {
        "unity_text", "unity_json", "unity_csv",
        "unity_xml", "unity_po", "unity_yaml",
    }
    known_labels = {"UI text", "Game text", "Dialog", "Narration", "Choice option"}
    for ctx in unity_contexts:
        label = _ctx_label(ctx)
        assert label != ctx, f"БАГ: {ctx!r} не имеет читаемой метки, вернулась сырая строка контекста"
        assert label in known_labels, f"Неожиданная метка для {ctx!r}: {label!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Bug (v50, cross-engine audit): позиционные/path-based инжекторы писали
# вслепую, без проверки, что значение, которое сейчас лежит по этому пути,
# всё ещё совпадает с original, который видел extractor. Если файл данных
# игры менялся между Extract и Inject — перевод СТАРОГО текста молча
# перезаписывал ТЕКУЩЕЕ значение. Уже была защита в Unity XML/PO/YAML,
# Wolf RPG (бинарный patch) и Kirikiri — теперь то же самое добавлено в
# RPG Maker MV/MZ, Unity JSON/CSV/TXT, VX/Ace и Tyrano. Ren'Py уже имел
# основную защищённую ветку (exact match) плюс более рискованный fallback
# — fallback оставлен (это осознанный компромисс для устойчивости), но
# теперь логируется явно, а не молча.
# ─────────────────────────────────────────────────────────────────────────────

def test_rpgmv_set_nested_skips_when_current_value_differs_from_original():
    """RPG Maker MV/MZ: generic path-инжектор (_set_nested через _apply_entry)."""
    from engine.injector import _apply_entry

    data = {"name": "さようなら"}  # изменилось с момента Extract
    ok = _apply_entry(data, "name", "Привет", original="こんにちは")
    assert ok is False, "БАГ: запись применилась несмотря на несовпадение с original"
    assert data["name"] == "さようなら", f"БАГ: значение перезаписано: {data['name']!r}"

    # Контроль: если original совпадает, запись должна пройти как обычно
    data2 = {"name": "こんにちは"}
    assert _apply_entry(data2, "name", "Привет", original="こんにちは") is True
    assert data2["name"] == "Привет"


def test_rpgmv_plugin_arg_skips_when_current_value_differs_from_original():
    """RPG Maker MZ: _inject_plugin_arg (MZ plugin-command JSON-аргументы)."""
    from engine.injector import _apply_entry

    data = {"events": [{"pages": [{"list": [
        {"code": 357, "parameters": ["", "", "", json.dumps({"Text": "さようなら"}, ensure_ascii=False)]}
    ]}]}]}
    path = "events[0].pages[0].list[0].plugin_arg.Text"
    ok = _apply_entry(data, path, "Привет", original="こんにちは")
    assert ok is False, "БАГ: plugin_arg перезаписан несмотря на несовпадение с original"
    current = json.loads(data["events"][0]["pages"][0]["list"][0]["parameters"][3])
    assert current["Text"] == "さようなら"


def test_unity_json_csv_text_writers_skip_on_drift():
    """Unity: все три позиционных writer'а (_set_json_value/_set_csv_cell/_set_line)."""
    from engine.unity_injector import _set_json_value, _set_csv_cell, _set_line

    data = {"lines": ["さようなら"]}
    assert _set_json_value(data, "lines[0]", "Привет", original="こんにちは") is False
    assert data["lines"][0] == "さようなら"

    rows = [["Key", "JA"], ["A", "さようなら"]]
    assert _set_csv_cell(rows, "row[1].col[1]", "Привет", original="こんにちは") is False
    assert rows[1][1] == "さようなら"

    lines = ["header", "さようなら"]
    assert _set_line(lines, "line[1]", "Привет", original="こんにちは") is False
    assert lines[1] == "さようなら"


def test_vxace_attr_and_index_writers_skip_on_drift():
    """VX/Ace: _apply_translation принимал original, но никогда его не проверял."""
    from engine.vxace_injector import _apply_translation
    from rubymarshal.reader import RubyString

    class FakeRubyObject:
        def __init__(self, attrs):
            self.attributes = attrs

    # @name-атрибут (напр. Actors.rvdata2)
    obj = FakeRubyObject({"@name": RubyString("さようなら")})
    data_attr = [None, obj]
    assert _apply_translation(data_attr, "[1]@name", "こんにちは", "Привет") is False
    assert str(obj.attributes["@name"]) == "さようなら"

    # Индекс в parameters (напр. диалог в списке команд события)
    data_idx = [None, None, {"pages": [{"list": [
        {"code": 401, "parameters": [RubyString("さようなら")]}
    ]}]}]
    assert _apply_translation(data_idx, "[2].pages[0].list[0].parameters[0][0]", "こんにちは", "Привет") is False
    assert str(data_idx[2]["pages"][0]["list"][0]["parameters"][0]) == "さようなら"


def test_vxace_dialog_scroll_writer_skips_on_drift():
    """
    BUG (найдено при аудите покрытия): в отличие от 'attr' и 'index' веток
    (проверено выше в test_vxace_attr_and_index_writers_skip_on_drift), ветка
    dialog/scroll в _apply_translation получала `original` четвёртым
    параметром, но НИКОГДА его не сравнивала с текущим содержимым — писала
    перевод устаревшего текста поверх диалога, который разработчик игры
    поменял после Extract, без единой проверки. Единственная из трёх веток
    без drift-защиты, при том что MV/MZ-аналог (_inject_dialog/_inject_scroll
    в injector.py) её имеет с явным комментарием "Never patch a shifted/stale
    event block".
    """
    from engine.vxace_injector import _apply_translation
    from rubymarshal.reader import RubyString

    class FakeRubyObject:
        def __init__(self, attrs):
            self.attributes = attrs

    def make_cmd_list(line1, line2):
        return [
            FakeRubyObject({"@code": 101, "@parameters": []}),
            FakeRubyObject({"@code": 401, "@parameters": [RubyString(line1)]}),
            FakeRubyObject({"@code": 401, "@parameters": [RubyString(line2)]}),
        ]

    path = "events[1].pages[0].list[1..2].dialog"
    extracted_original = "こんにちは\n元気ですか"
    stale_translation = "Привет\nКак дела"

    # Первая строка блока поменялась в игре после Extract -> весь диапазон
    # должен быть пропущен целиком (не патчим только часть — собьётся
    # соответствие индексов).
    cmds = make_cmd_list("さようなら", "元気ですか")
    data = {"events": {1: {"pages": [{"list": cmds}]}}}
    result = _apply_translation(data, path, extracted_original, stale_translation)
    assert result is False, "БАГ: инъекция в изменившийся диалог должна была быть отклонена"
    assert str(cmds[1].attributes["@parameters"][0]) == "さようなら", (
        "БАГ: новая реплика разработчика была затёрта переводом устаревшего текста"
    )
    assert str(cmds[2].attributes["@parameters"][0]) == "元気ですか", "неизменённая строка не должна была тронуться"

    # Контроль: ничего не поменялось с Extract -> инъекция проходит как обычно.
    cmds_ok = make_cmd_list("こんにちは", "元気ですか")
    data_ok = {"events": {1: {"pages": [{"list": cmds_ok}]}}}
    result_ok = _apply_translation(data_ok, path, extracted_original, stale_translation)
    assert result_ok is True
    assert str(cmds_ok[1].attributes["@parameters"][0]) == "Привет"
    assert str(cmds_ok[2].attributes["@parameters"][0]) == "Как дела"


def test_tyrano_inject_skips_when_line_changed_since_extract():
    """Tyrano: строка перезаписывалась по индексу без сверки с текущим содержимым."""
    from engine.tyrano_injector import inject_tyrano

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        scenario_dir = tmp / "data" / "scenario"
        scenario_dir.mkdir(parents=True)
        ks_file = scenario_dir / "scene1.ks"
        ks_file.write_text("さようなら\n", encoding="utf-8")  # изменилось с момента Extract
        out_dir = tmp / "out"
        (out_dir / "extracted").mkdir(parents=True)

        entries = [{
            "file": "scene1.ks.json", "_source_ks": "scene1.ks", "path": "line:0",
            "original": "こんにちは", "full_line": "こんにちは", "translated": "Привет",
            "game_type": "tyrano", "context": "narrator",
        }]
        (out_dir / "extracted" / "scene1_translated.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        result = inject_tyrano(tmp, out_dir, log_callback=lambda m: None)
        assert result["total_injected"] == 0, "БАГ: перевод устаревшего оригинала применился"
        assert result["total_errors"] == 1
        assert ks_file.read_text(encoding="utf-8") == "さようなら\n"


def test_renpy_fuzzy_fallback_replacement_is_logged_not_silent():
    """
    Ren'Py: точное совпадение original уже было защищено (проверяется
    отдельно ниже), но fallback на «первую строку в кавычках» применялся
    молча — неотличимо от подтверждённого точного совпадения в логах.
    Резистентность сохранена (замена всё ещё применяется), но теперь это
    явно видно в логах.
    """
    from renpy_injector import inject_renpy

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_sub = tmp / "game"
        game_sub.mkdir()
        rpy = game_sub / "script.rpy"
        # Строка при Extract была "こんにちは"; разработчик поменял на
        # "さようなら" до Inject — но кавычки на строке остались.
        rpy.write_text('e "さようなら"\n', encoding="utf-8")
        out_dir = tmp / "out"
        (out_dir / "extracted").mkdir(parents=True)

        entries = [{"_source_rpy": "script.rpy", "line_start": 0, "original": "こんにちは",
                    "translated": "Привет", "quote_char": '"'}]
        (out_dir / "extracted" / "script_translated.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        logs = []
        result = inject_renpy(tmp, out_dir, log_callback=lambda m: logs.append(m))

        assert result["total_injected"] == 1  # резистентность сохранена
        assert rpy.read_text(encoding="utf-8") == 'e "Привет"\n'
        assert any("best-effort" in l or "не совпал" in l for l in logs), (
            "БАГ: fuzzy-fallback применился молча, без предупреждения в логах"
        )


def test_renpy_exact_match_still_applies_without_fuzzy_warning():
    """Контроль: когда original точно совпадает, предупреждения быть не должно."""
    from renpy_injector import inject_renpy

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_sub = tmp / "game"
        game_sub.mkdir()
        rpy = game_sub / "script.rpy"
        rpy.write_text('e "こんにちは"\n', encoding="utf-8")
        out_dir = tmp / "out"
        (out_dir / "extracted").mkdir(parents=True)

        entries = [{"_source_rpy": "script.rpy", "line_start": 0, "original": "こんにちは",
                    "translated": "Привет", "quote_char": '"'}]
        (out_dir / "extracted" / "script_translated.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        logs = []
        result = inject_renpy(tmp, out_dir, log_callback=lambda m: logs.append(m))
        assert result["total_injected"] == 1
        assert rpy.read_text(encoding="utf-8") == 'e "Привет"\n'
        assert not any("best-effort" in l for l in logs), (
            "Точное совпадение не должно давать предупреждение о fuzzy-замене"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug (v50, round 3 — 0% coverage sweep): run.py's pre-flight dependency
# check only recognized "google.genai" (the new SDK). requirements.txt
# installs google-generativeai (the legacy SDK) by default — google-genai is
# commented out as an optional upgrade — and ai_translator.py's Gemini
# provider already falls back to google.generativeai correctly. A completely
# standard `pip install -r requirements.txt` followed by `python run.py`
# reported Gemini as NOT installed anyway.
# ─────────────────────────────────────────────────────────────────────────────

def test_run_py_detects_gemini_via_either_sdk_name():
    """
    OPTIONAL["gemini"] must list both import names, and the availability
    check must treat either one as "Gemini available" — matching what
    ai_translator.py actually accepts at runtime.
    """
    import importlib
    import run

    importlib.reload(run)
    assert run.OPTIONAL["gemini"] == ("google.genai", "google.generativeai"), (
        "БАГ: OPTIONAL['gemini'] должен проверять оба варианта SDK, а не только google.genai"
    )

    # Симулируем окружение как после `pip install -r requirements.txt`:
    # google-generativeai есть, google-genai — нет (именно так поставлено
    # requirements.txt по умолчанию).
    def fake_check(name: str) -> bool:
        return name == "google.generativeai"

    with patch.object(run, "check", side_effect=fake_check):
        detected = any(run.check(n) for n in run.OPTIONAL["gemini"])
    assert detected is True, (
        "БАГ: run.py не увидел Gemini, хотя google-generativeai (fallback SDK) установлен"
    )


def test_run_py_main_reports_gemini_ok_with_legacy_sdk_only(capsys, monkeypatch):
    """Сквозная проверка main(): вывод должен показывать Gemini как ✓, а не как отсутствующий."""
    import run

    def fake_check(name: str) -> bool:
        # PyQt6 (REQUIRED) должен считаться установленным, чтобы не падать раньше;
        # из Gemini-пары доступен только устаревший google.generativeai.
        return name in ("PyQt6", "google.generativeai", "openai", "anthropic")

    monkeypatch.setattr(run, "check", fake_check)
    monkeypatch.setattr(run, "main", run.main)  # no-op, keep original reference clear
    # Подменяем импорт GUI, чтобы main() не пытался реально запустить PyQt6
    monkeypatch.setitem(sys.modules, "rpgmtl_desktop", type(sys)("rpgmtl_desktop"))
    sys.modules["rpgmtl_desktop"].main = lambda: None

    run.main()
    out = capsys.readouterr().out
    assert "✓ gemini" in out, f"БАГ: Gemini не показан как доступный при установленном google-generativeai:\n{out}"
    assert "- gemini" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Feature (round 4): карантин low-confidence строк для Wolf RPG.
# _scan_strings — байтовая эвристика без понимания структуры файла: не может
# отличить диалог от имени ресурса/анимации в таблицах БД (.wolf). Репорт
# пользователя: перевод сломал игру (анимации). CommonEvent.dat/Map*.mps —
# реальные диалоговые команды, там риск ниже.
# ─────────────────────────────────────────────────────────────────────────────

def _make_wolf_str(text: str) -> bytes:
    raw = text.encode("cp932")
    return struct.pack("<I", len(raw)) + raw


def test_wolf_extractor_flags_wolf_files_low_confidence_not_dat_or_mps():
    """.wolf → _low_confidence=True; .dat/.mps → _low_confidence=False."""
    from engine.wolf_extractor import WolfExtractor

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        basic = tmp / "Data" / "BasicData"
        mapdata = tmp / "Data" / "MapData"
        basic.mkdir(parents=True)
        mapdata.mkdir(parents=True)

        (basic / "DataBase.wolf").write_bytes(_make_wolf_str("爆発エフェクト"))
        (basic / "CommonEvent.dat").write_bytes(_make_wolf_str("こんにちは、旅人よ"))
        (mapdata / "Map001.mps").write_bytes(_make_wolf_str("さようなら"))

        entries = WolfExtractor(tmp, log_cb=lambda m: None).extract()
        by_orig = {e["original"]: e for e in entries}

        assert by_orig["爆発エフェクト"]["_low_confidence"] is True, (
            "БАГ: строка из .wolf-файла БД не помечена как low-confidence"
        )
        assert by_orig["こんにちは、旅人よ"]["_low_confidence"] is False, (
            "БАГ: реальный диалог из CommonEvent.dat ошибочно попал в карантин"
        )
        assert by_orig["さようなら"]["_low_confidence"] is False, (
            "БАГ: реальный диалог из Map*.mps ошибочно попал в карантин"
        )


def test_pipeline_translate_quarantines_low_confidence_wolf_entries(tmp_path, monkeypatch):
    """
    Сквозная проверка: Pipeline.translate() не должен отправлять
    low-confidence строки в AI, но обязан оставить их в extracted/*.json
    нетронутыми (translated="") — а не потерять совсем.
    """
    import ai_translator
    from ai_translator import BatchResult, TranslationResult
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    basic = game_dir / "Data" / "BasicData"
    basic.mkdir(parents=True)
    (basic / "DataBase.wolf").write_bytes(_make_wolf_str("爆発エフェクト"))
    (basic / "CommonEvent.dat").write_bytes(_make_wolf_str("こんにちは、旅人よ"))
    out_dir = tmp_path / "out"

    logs: list[str] = []
    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=logs.append)
    ok, msg = p.extract()
    assert ok, msg

    seen_texts: list[str] = []

    def fake_translate_texts(texts, config, log_callback=None, progress_callback=None,
                              state=None, cancel_event=None, contexts=None):
        seen_texts.extend(texts)
        results = [TranslationResult(original=t, translated=f"TR:{t}", success=True) for t in texts]
        return BatchResult(results=results, total=len(texts), succeeded=len(texts), failed=0)

    monkeypatch.setattr(ai_translator, "translate_texts", fake_translate_texts)

    ok2, msg2 = p.translate(provider="openai", api_key="fake-key", model="fake-model")
    assert ok2, msg2

    assert "爆発エフェクト" not in seen_texts, "БАГ: low-confidence строка отправлена в AI на перевод"
    assert "こんにちは、旅人よ" in seen_texts, "Обычная строка должна переводиться как раньше"
    assert any("карантин" in l.lower() for l in logs), "Должно быть предупреждение о карантине в логе"

    extract_dir = out_dir / "extracted"
    all_final = []
    for jf in extract_dir.glob("*_translated.json"):
        all_final.extend(json.loads(jf.read_text(encoding="utf-8")))
    by_orig = {e["original"]: e for e in all_final}

    assert by_orig["爆発エフェクト"]["translated"] == "", (
        "БАГ: low-confidence строка получила перевод, хотя должна была остаться нетронутой"
    )
    assert by_orig["こんにちは、旅人よ"]["translated"] == "TR:こんにちは、旅人よ"


def test_pipeline_translate_include_low_confidence_true_disables_quarantine(tmp_path, monkeypatch):
    """include_low_confidence=True должен снимать фильтр, если пользователь так решил."""
    import ai_translator
    from ai_translator import BatchResult, TranslationResult
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    basic = game_dir / "Data" / "BasicData"
    basic.mkdir(parents=True)
    (basic / "DataBase.wolf").write_bytes(_make_wolf_str("爆発エフェクト"))
    out_dir = tmp_path / "out"

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    ok, msg = p.extract()
    assert ok, msg

    seen_texts: list[str] = []

    def fake_translate_texts(texts, config, log_callback=None, progress_callback=None,
                              state=None, cancel_event=None, contexts=None):
        seen_texts.extend(texts)
        results = [TranslationResult(original=t, translated=f"TR:{t}", success=True) for t in texts]
        return BatchResult(results=results, total=len(texts), succeeded=len(texts), failed=0)

    monkeypatch.setattr(ai_translator, "translate_texts", fake_translate_texts)

    ok2, msg2 = p.translate(
        provider="openai", api_key="fake-key", model="fake-model",
        include_low_confidence=True,
    )
    assert ok2, msg2
    assert "爆発エフェクト" in seen_texts, "include_low_confidence=True должен снимать карантин"


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 5, репорт пользователя): Unity-игра определялась как RPG Maker.
# Причина — порядок проверок: RPG Maker (data/System.json — общий маркер,
# ничто не мешает Unity-игре иметь такую же папку/файл для своих нужд)
# проверялся ДО Unity (ProjectSettings/ProjectVersion.txt и т.п. — маркеры,
# которые практически невозможно случайно совпасть). Специфичные маркеры
# должны проверяться раньше общих.
# ─────────────────────────────────────────────────────────────────────────────

def test_detect_unity_project_version_wins_over_coincidental_rpgmaker_data_folder(tmp_path):
    """
    Unity-проект (ProjectSettings/ProjectVersion.txt), у которого ЕСТЬ своя
    папка data/System.json + js/rpg_core.js (совпадение — не имеет отношения
    к RPG Maker) — должен определиться как Unity, а не RPG Maker MV.
    """
    from game_detector import detect_game_type

    (tmp_path / "ProjectSettings").mkdir()
    (tmp_path / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2022.3.1f1", encoding="utf-8"
    )
    # Совпадение с RPG Maker маркером — не должно перебивать Unity
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text("{}", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "rpg_core.js").write_text("", encoding="utf-8")

    assert detect_game_type(tmp_path) == "unity", (
        "БАГ: Unity-проект определился как RPG Maker из-за совпадения data/System.json"
    )


def test_detect_unity_built_game_data_folder_wins_over_coincidental_rpgmaker_data_folder(tmp_path):
    """То же самое, но для собранной (не исходной) Unity-игры: <Имя>_Data/."""
    from game_detector import detect_game_type

    data_dir = tmp_path / "MyGame_Data"
    data_dir.mkdir()
    (data_dir / "globalgamemanagers").write_bytes(b"")

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text("{}", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "rpg_core.js").write_text("", encoding="utf-8")

    assert detect_game_type(tmp_path) == "unity", (
        "БАГ: собранная Unity-игра определилась как RPG Maker из-за совпадения data/System.json"
    )


def test_detect_game_type_verbose_reports_matched_marker(tmp_path):
    """
    Диагностика: detect_game_type_verbose() должен объяснять, какой именно
    маркер сработал — чтобы будущую путаницу движков можно было
    диагностировать по одной строке лога, а не гадать.
    """
    from game_detector import detect_game_type_verbose

    (tmp_path / "ProjectSettings").mkdir()
    (tmp_path / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2022.3.1f1", encoding="utf-8"
    )
    game_type, reason = detect_game_type_verbose(tmp_path)
    assert game_type == "unity"
    assert "ProjectVersion.txt" in reason

    (tmp_path / "ProjectSettings" / "ProjectVersion.txt").unlink()
    (tmp_path / "ProjectSettings").rmdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text("{}", encoding="utf-8")
    game_type2, reason2 = detect_game_type_verbose(tmp_path)
    assert game_type2 == "rpgmv"
    assert "System.json" in reason2


def test_pipeline_extract_logs_detection_reason(tmp_path):
    """Pipeline.extract() должен логировать причину детекции, не только тип."""
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    (game_dir / "ProjectSettings").mkdir(parents=True)
    (game_dir / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2022.3.1f1", encoding="utf-8"
    )
    out_dir = tmp_path / "out"

    logs: list[str] = []
    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=logs.append)
    p.extract()

    assert any("ProjectVersion.txt" in l for l in logs), (
        f"БАГ: причина детекции не попала в лог Extract:\n{logs}"
    )



# ─────────────────────────────────────────────────────────────────────────────
# Feature (round 4): post-write верификация для Wolf RPG инжектора.
# Смещения при обратном порядке патчинга (см. sorted_trans в inject_wolf) не
# сдвигаются друг относительно друга, поэтому после записи можно честно
# перечитать файл и подтвердить, что по каждому применённому offset реально
# лежит записанный перевод — а не просто "запись не упала с исключением".
# ─────────────────────────────────────────────────────────────────────────────

def _write_wolf_translated_entry(extract_dir: Path, src_path: str, offset: int,
                                  original: str, translated: str):
    (extract_dir).mkdir(parents=True, exist_ok=True)
    entries = [{
        "file": "test.json", "_source_path": src_path, "path": str(offset),
        "original": original, "translated": translated, "_encoding": "cp932",
        "game_type": "wolf", "context": "wolf_string",
    }]
    (extract_dir / "test_translated.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )


def test_wolf_inject_post_write_check_silent_on_correct_patch(tmp_path):
    """Нормальный корректный патч не должен давать предупреждение post-write."""
    from engine.wolf_injector import inject_wolf

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_file = game_dir / "CommonEvent.dat"
    game_file.write_bytes(_make_wolf_str("こんにちは"))

    out_dir = tmp_path / "out"
    _write_wolf_translated_entry(out_dir / "extracted", "CommonEvent.dat", 0, "こんにちは", "Привет")

    logs: list[str] = []
    result = inject_wolf(game_dir, out_dir, log_callback=logs.append)
    assert result["total_injected"] == 1
    assert not any("НЕ подтвердила" in l for l in logs), (
        f"Ложное срабатывание post-write проверки на корректном патче:\n{logs}"
    )


def test_wolf_inject_post_write_check_catches_simulated_corruption(tmp_path, monkeypatch):
    """
    Если _patch_file (гипотетически, будущей регрессией) вернёт "успех", но
    реально записанные байты не соответствуют переводу — post-write проверка
    должна это заметить и явно предупредить, а не молчать.
    """
    import engine.wolf_injector as wolf_injector_mod

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_file = game_dir / "CommonEvent.dat"
    game_file.write_bytes(_make_wolf_str("こんにちは"))

    out_dir = tmp_path / "out"
    _write_wolf_translated_entry(out_dir / "extracted", "CommonEvent.dat", 0, "こんにちは", "Привет")

    # Симулируем баг в _patch_file: заявляет успех (new_data is not data),
    # но реально пишет не тот перевод, что ожидался.
    def broken_patch_file(data, offset, original, translated, original_encoding="cp932"):
        wrong_bytes = "СОВСЕМ ДРУГОЙ ТЕКСТ".encode("utf-8")
        new_data = bytearray(data)
        return new_data[:offset] + bytearray(struct.pack('<I', len(wrong_bytes)) + wrong_bytes) + new_data[offset + 4 + len("こんにちは".encode("cp932")):], 0, "utf-8"

    monkeypatch.setattr(wolf_injector_mod, "_patch_file", broken_patch_file)

    logs: list[str] = []
    result = wolf_injector_mod.inject_wolf(game_dir, out_dir, log_callback=logs.append)
    assert any("НЕ подтвердила" in l for l in logs), (
        f"БАГ: post-write проверка не поймала смоделированную порчу записи:\n{logs}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 7): NARROW_CHARS/WIDE_CHARS в font_analyzer.py были объявлены,
# но нигде не проверялись в get_char_width() — любой символ не из основной
# таблицы (вся кириллица, если не передана своя table) сразу падал в
# грубый cp > 0x2000 тест и получал плоские 14px. Учитывая, что перевод
# идёт в основном на русский, а Smart Warp включён по умолчанию
# (apply_warp=True) почти для всех движков — это был не периферийный
# нюанс, а основной путь измерения ширины для большинства реального
# переведённого текста.
# ─────────────────────────────────────────────────────────────────────────────

def test_font_analyzer_narrow_wide_sets_are_actually_consulted():
    """NARROW_CHARS/WIDE_CHARS должны реально влиять на get_char_width, не быть мёртвым кодом."""
    from font_analyzer import get_char_width, NARROW_CHARS, WIDE_CHARS, DEFAULT_CHAR_WIDTH

    assert NARROW_CHARS, "NARROW_CHARS не должен быть пустым"
    assert WIDE_CHARS, "WIDE_CHARS не должен быть пустым"

    narrow_sample = next(iter(NARROW_CHARS - set(" \t\n\r")))
    wide_sample = next(iter(WIDE_CHARS))

    assert get_char_width(narrow_sample, table={}) < DEFAULT_CHAR_WIDTH, (
        f"БАГ: символ {narrow_sample!r} из NARROW_CHARS не получил уменьшенную ширину"
    )
    assert get_char_width(wide_sample, table={}) > DEFAULT_CHAR_WIDTH, (
        f"БАГ: символ {wide_sample!r} из WIDE_CHARS не получил увеличенную ширину"
    )


def test_font_analyzer_differentiates_cyrillic_letter_widths():
    """
    БАГ: до фикса ЛЮБАЯ кириллическая буква (без явной записи в table)
    получала одинаковые 14px — "ш" (типографски широкая) и "ь" (узкая)
    были неотличимы. Проверяем и характерные широкие/узкие буквы, и что
    таблица (если передана явно) по-прежнему в приоритете.
    """
    from font_analyzer import get_char_width

    assert get_char_width("ш") > get_char_width("ь"), (
        "БАГ: широкая 'ш' и узкая 'ь' получили одинаковую или обратную ширину"
    )
    assert get_char_width("м") > get_char_width("о"), (
        "БАГ: широкая 'м' не шире обычной 'о'"
    )

    # Явно переданная таблица должна оставаться в приоритете над NARROW/WIDE.
    custom_table = {"ш": 99}
    assert get_char_width("ш", table=custom_table) == 99, (
        "БАГ: NARROW_CHARS/WIDE_CHARS перебивают явную запись в table"
    )


def test_measure_text_width_cyrillic_is_not_flat_per_character():
    """
    Сквозная проверка: ширина строки из ТОЛЬКО кириллических букв (без
    пробелов/пунктуации — те уже были в ASCII-таблице и раньше, это не
    показательно) не должна быть просто len(text) * 14 — иначе Smart Warp
    переносит строки без учёта того, что разные буквы реально занимают
    разное место.
    """
    from font_analyzer import measure_text_width, DEFAULT_CHAR_WIDTH

    text = "широкийтекст"  # только буквы, есть и широкие (ш,ж→нет,к,и), и обычные
    width = measure_text_width(text)
    flat_estimate = len(text) * DEFAULT_CHAR_WIDTH
    assert width != flat_estimate, (
        "БАГ: ширина строки из кириллических букв равна len(text) * 14 — "
        "буквы не дифференцируются вообще"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 8): apply_dedup_to_files() — O(n²) вместо O(n).
# Для каждого вхождения каждого уникального текста делался линейный поиск
# нужной entry по path внутри ВСЕХ entries файла. На синтетическом тесте:
# N=3000 → 0.2с, N=6000 → 0.8с, N=12000 → 3.0с, N=24000 → 11с — время
# растёт в ~4 раза при удвоении N (квадратично). Для большого
# CommonEvents.json/карты на тысячи строк в одном файле это уже реально
# заметно на глаз при каждом Translate. Индекс path→entry строится один
# раз на файл — дальше поиск O(1), весь проход O(n).
# ─────────────────────────────────────────────────────────────────────────────

def test_apply_dedup_to_files_correctness_preserved_after_indexing(tmp_path):
    """Базовая корректность не должна была измениться после перехода на индекс."""
    from engine.deduplicator import build_dedup_map, apply_dedup_to_files

    entries_a = [
        {"file": "a.json", "path": "[0]", "original": "こんにちは", "translated": ""},
        {"file": "a.json", "path": "[1]", "original": "こんにちは", "translated": ""},  # повтор
        {"file": "a.json", "path": "[2]", "original": "さようなら", "translated": ""},
    ]
    entries_b = [
        {"file": "b.json", "path": "[0]", "original": "こんにちは", "translated": ""},  # тот же текст, другой файл
    ]
    (tmp_path / "a.json").write_text(json.dumps(entries_a, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(entries_b, ensure_ascii=False), encoding="utf-8")

    dm = build_dedup_map(entries_a + entries_b, log_cb=lambda m: None)
    assert dm.unique_count == 2
    dm.set_translation("こんにちは", "Привет")
    dm.set_translation("さようなら", "Пока")

    applied = apply_dedup_to_files(dm, tmp_path, log_cb=lambda m: None)
    assert applied == 4

    result_a = json.loads((tmp_path / "a_translated.json").read_text(encoding="utf-8"))
    result_b = json.loads((tmp_path / "b_translated.json").read_text(encoding="utf-8"))
    assert result_a[0]["translated"] == "Привет"
    assert result_a[1]["translated"] == "Привет"
    assert result_a[2]["translated"] == "Пока"
    assert result_b[0]["translated"] == "Привет"


def test_apply_dedup_to_files_path_collision_first_entry_wins(tmp_path):
    """
    Защитный edge case: если у двух entries в одном файле совпал path
    (в норме не должно происходить, но раньше линейный поиск брал первую
    подходящую по .strip()==original — индекс должен вести себя так же,
    не «последний выигрывает» по умолчанию для dict).
    """
    from engine.deduplicator import build_dedup_map, apply_dedup_to_files

    entries = [
        {"file": "a.json", "path": "[0]", "original": "текст1", "translated": ""},
        {"file": "a.json", "path": "[0]", "original": "текст2", "translated": ""},  # коллизия path
    ]
    (tmp_path / "a.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    dm = build_dedup_map(entries, log_cb=lambda m: None)
    dm.set_translation("текст1", "перевод1")
    dm.set_translation("текст2", "перевод2")
    apply_dedup_to_files(dm, tmp_path, log_cb=lambda m: None)

    result = json.loads((tmp_path / "a_translated.json").read_text(encoding="utf-8"))
    assert result[0]["translated"] == "перевод1", "Первая entry с этим path должна была выиграть"


def test_apply_dedup_to_files_scales_linearly_not_quadratically(tmp_path):
    """
    Perf-guard: N=20000 строк в одном файле должно укладываться в разумное
    время. При квадратичном поведении (баг, который тут чинился) это заняло
    бы порядка 8-10 секунд по замерам на этой же машине; после фикса —
    доли секунды. Порог в 3 секунды даёт большой запас на медленный CI,
    но всё ещё чётко ловит регресс обратно к O(n²).
    """
    import time
    from engine.deduplicator import build_dedup_map, apply_dedup_to_files

    n = 20000
    unique_pool = [f"Строка диалога номер {i} с содержимым" for i in range(int(n * 0.4))]
    entries = [
        {"file": "Big.json", "path": f"events[{i}]", "original": unique_pool[i % len(unique_pool)], "translated": ""}
        for i in range(n)
    ]
    (tmp_path / "Big.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    dm = build_dedup_map(entries, log_cb=lambda m: None)
    for t in dm.unique_texts():
        dm.set_translation(t, "TR:" + t)

    t0 = time.perf_counter()
    applied = apply_dedup_to_files(dm, tmp_path, log_cb=lambda m: None)
    elapsed = time.perf_counter() - t0

    assert applied == n
    assert elapsed < 3.0, (
        f"БАГ: apply_dedup_to_files заняла {elapsed:.2f}с на {n} строк — похоже на возврат к O(n²)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 8, продолжение): Glossary.apply() пересортировывала self.terms
# на КАЖДЫЙ вызов, а вызывается один раз на каждую переведённую строку в
# _apply_glossary_postprocess (pipeline.py) — потенциально тысячи раз за
# один Translate, хотя результат сортировки идентичен, пока glossary.terms
# не менялся. apply_batch() считает сортировку один раз на весь батч строк.
# ─────────────────────────────────────────────────────────────────────────────

def test_glossary_apply_batch_matches_per_string_apply():
    """Корректность: apply_batch() должен давать те же результаты, что и apply() построчно."""
    from engine.glossary import Glossary

    g = Glossary()
    g.add("勇者", "Герой")
    g.add("魔王", "Король демонов")
    g.add(r"\N[1]たろう", "не должно тронуть код")  # с RPG-кодом внутри

    texts = [
        "勇者は魔王と戦った。",
        r"\N[1]は勇者だ。",
        "普通のテキスト",
        "",  # пустая строка — edge case
    ]
    batch_result = g.apply_batch(texts)
    per_string_result = [g.apply(t) for t in texts]
    assert batch_result == per_string_result


def test_glossary_apply_batch_is_faster_than_per_call_sorting(tmp_path):
    """
    Perf-guard: apply_batch() на большом глоссарии+объёме строк должен быть
    заметно быстрее эквивалентного цикла apply() построчно (который заново
    сортирует термины на каждый вызов). Порог — с большим запасом, чтобы не
    флапать на медленном CI, но чётко ловит регресс к построчной сортировке.
    """
    import time
    from engine.glossary import Glossary

    g = Glossary()
    for i in range(200):
        g.add(f"термин{i}яп", f"Термин{i}")
    texts = [f"Строка {i} с термин5яп внутри." for i in range(8000)]

    t0 = time.perf_counter()
    g.apply_batch(texts)
    batch_time = time.perf_counter() - t0

    t0b = time.perf_counter()
    for t in texts:
        g.apply(t)
    per_call_time = time.perf_counter() - t0b

    assert batch_time < per_call_time / 1.5, (
        f"БАГ: apply_batch ({batch_time:.3f}с) не быстрее построчного apply ({per_call_time:.3f}с) "
        f"— похоже, батч больше не переиспользует одну сортировку"
    )


def test_pipeline_glossary_postprocess_uses_apply_batch(tmp_path):
    """
    Интеграция: _apply_glossary_postprocess должен корректно применять
    глоссарий через apply_batch(), включая случай нескольких файлов и
    записей без перевода (которые не должны попадать в батч).
    """
    from pipeline import Pipeline
    from engine.glossary import Glossary

    out_dir = tmp_path / "out"
    extract_dir = out_dir / "extracted"
    extract_dir.mkdir(parents=True)

    entries = [
        {"file": "a.json", "path": "[0]", "original": "勇者", "translated": " Герой сказал что-то"},
        {"file": "a.json", "path": "[1]", "original": "", "translated": ""},  # ещё не переведено
    ]
    (extract_dir / "a_translated.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    g = Glossary()
    g.add("Герой", "ГЕРОЙ")  # replace() регистрозависим — термин должен совпадать по регистру с текстом

    p = Pipeline(game_dir=tmp_path / "game", output_dir=out_dir, log_callback=lambda m: None)
    p._apply_glossary_postprocess(extract_dir, g)

    result = json.loads((extract_dir / "a_translated.json").read_text(encoding="utf-8"))
    assert result[0]["translated"] == " ГЕРОЙ сказал что-то"
    assert result[1]["translated"] == ""  # пустая запись не тронута


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 10, из анализа конкурентов): частичный провал перевода (часть
# строк не перевелась из-за ошибок API, но сам процесс не был отменён
# пользователем — was_cancelled=False) сообщался как "✓ Переведено N/M" —
# несовпадение N и M нужно было заметить и вычесть самому, число неудачных
# строк было только во второй, отдельной строке лога. Похожая проблема
# документирована как известный недостаток в стороннем инструменте того же
# класса ("Partial translation is not clearly indicated to the user after
# a run").
# ─────────────────────────────────────────────────────────────────────────────

def test_pipeline_translate_message_clearly_flags_partial_failures(tmp_path, monkeypatch):
    """Главное возвращаемое сообщение должно явно показывать неудачные строки, не только числа для вычитания."""
    import ai_translator
    from ai_translator import BatchResult, TranslationResult
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    (game_dir / "data").mkdir(parents=True)
    (game_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "T"}), encoding="utf-8")
    (game_dir / "data" / "Actors.json").write_text(
        json.dumps([None,
                    {"id": 1, "name": "こんにちは"},
                    {"id": 2, "name": "さようなら"},
                    {"id": 3, "name": "ありがとう"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (game_dir / "js").mkdir()
    (game_dir / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    ok, msg = p.extract()
    assert ok, msg

    def fake_translate_texts(texts, config, log_callback=None, progress_callback=None,
                              state=None, cancel_event=None, contexts=None):
        # Одна строка успешно, одна — "не удалось" (retry исчерпан, но
        # was_cancelled остаётся False — сам процесс не прерывался).
        results = []
        for i, t in enumerate(texts):
            if i == 0:
                results.append(TranslationResult(original=t, translated="", success=False, error="rate limit"))
            else:
                results.append(TranslationResult(original=t, translated=f"TR:{t}", success=True))
        n_failed = sum(1 for r in results if not r.success)
        return BatchResult(results=results, total=len(texts), succeeded=len(texts) - n_failed, failed=n_failed)

    monkeypatch.setattr(ai_translator, "translate_texts", fake_translate_texts)

    ok2, msg2 = p.translate(provider="openai", api_key="fake-key", model="fake-model")

    assert "НЕ УДАЛОСЬ" in msg2, f"БАГ: частичный провал не отражён явно в главном сообщении: {msg2!r}"
    assert "⚠" in msg2, f"БАГ: должен быть предупреждающий значок, а не только ✓: {msg2!r}"


def test_pipeline_translate_message_stays_clean_when_all_succeed(tmp_path, monkeypatch):
    """Контроль: когда всё перевелось успешно, сообщение не должно содержать лишних предупреждений."""
    import ai_translator
    from ai_translator import BatchResult, TranslationResult
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    (game_dir / "data").mkdir(parents=True)
    (game_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "T"}), encoding="utf-8")
    (game_dir / "data" / "Actors.json").write_text(
        json.dumps([None, {"id": 1, "name": "こんにちは"}], ensure_ascii=False), encoding="utf-8"
    )
    (game_dir / "js").mkdir()
    (game_dir / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    p.extract()

    def fake_translate_texts(texts, config, log_callback=None, progress_callback=None,
                              state=None, cancel_event=None, contexts=None):
        results = [TranslationResult(original=t, translated=f"TR:{t}", success=True) for t in texts]
        return BatchResult(results=results, total=len(texts), succeeded=len(texts), failed=0)

    monkeypatch.setattr(ai_translator, "translate_texts", fake_translate_texts)
    ok, msg = p.translate(provider="openai", api_key="fake-key", model="fake-model")

    assert ok is True
    assert "НЕ УДАЛОСЬ" not in msg
    assert "✓" in msg


# ─────────────────────────────────────────────────────────────────────────────
# Feature (round 11, из исследования конкурентов): GameStringer (близкий
# конкурент, 20+ движков) явно считает confidence score при детекции движка,
# а не жёстко фиксированный ответ. detect_game_type_verbose() раньше
# возвращался при ПЕРВОМ совпадении — если папка подходила под маркеры
# двух движков одновременно, об этом не было ни строки в логах (именно так
# была устроена уже исправленная в прошлом раунде путаница Unity/RPG Maker).
# Теперь собираются совпадения СО ВСЕХ проверок движков, и если их больше
# одного — это явно видно в reason, а не тонет в приоритете порядка.
# ─────────────────────────────────────────────────────────────────────────────

def test_detect_game_type_reports_conflict_when_multiple_engines_match(tmp_path):
    """
    Искусственный конфликт: Unity-маркеры + RPG Maker-маркеры в одной папке.
    Побеждает Unity (выше приоритетом, специфичнее), но RPG Maker должен
    быть явно упомянут в reason как альтернатива — не молчать.
    """
    from game_detector import detect_game_type_verbose

    (tmp_path / "ProjectSettings").mkdir()
    (tmp_path / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2022.3.1f1", encoding="utf-8"
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text(
        json.dumps({"gameTitle": "coincidence"}), encoding="utf-8"
    )
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "rpg_core.js").write_text("", encoding="utf-8")

    game_type, reason = detect_game_type_verbose(tmp_path)
    assert game_type == "unity"
    assert "также похоже на" in reason, f"БАГ: конфликт с RPG Maker не отражён в reason: {reason!r}"
    assert "rpgmv" in reason


def test_detect_game_type_no_conflict_warning_for_unambiguous_match(tmp_path):
    """Контроль: однозначная детекция без пересечений не должна давать ложных предупреждений."""
    from game_detector import detect_game_type_verbose

    (tmp_path / "ProjectSettings").mkdir()
    (tmp_path / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2022.3.1f1", encoding="utf-8"
    )
    game_type, reason = detect_game_type_verbose(tmp_path)
    assert game_type == "unity"
    assert "также похоже на" not in reason


def test_detect_game_type_content_awareness_does_not_block_minimal_system_json(tmp_path):
    """
    Регрессия: попытка content-валидации System.json изначально была
    сделана блокирующей (требовала 1-2 узнаваемых ключа) и ломала
    легитимные минимальные/пустые фикстуры — в т.ч. полностью пустой
    System.json (реалистично для урезанной/модифицированной игры).
    Content теперь только обогащает reason, не блокирует детекцию.
    """
    from game_detector import detect_game_type

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text("{}", encoding="utf-8")  # полностью пустой
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "rpg_core.js").write_text("", encoding="utf-8")

    assert detect_game_type(tmp_path) == "rpgmv", (
        "БАГ: полностью пустой System.json не должен блокировать детекцию RPG Maker"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 12, запрос "диагностика и логи должны находить скрытые баги
# точно"): систематически прошёлся по молчаливым except-блокам (~50 голых
# "except Exception: pass" вне best-effort очистки tmp-файлов). Самый
# весомый — get_stats() молча пропускал файлы, которые не распарсились:
# total_strings/unique_strings оказывались тихо занижены без единого
# признака в интерфейсе. Если бы где-то ещё была скрытая порча файла —
# именно эта диагностика должна была её показать, а вместо этого прятала.
# ─────────────────────────────────────────────────────────────────────────────

def test_pipeline_get_stats_reports_parse_errors_for_corrupt_files(tmp_path):
    """get_stats() должен явно считать и возвращать файлы, которые не удалось распарсить."""
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    out_dir = tmp_path / "out"
    extract_dir = out_dir / "extracted"
    extract_dir.mkdir(parents=True)

    (extract_dir / "good.json").write_text(
        json.dumps([{"original": "こんにちは", "translated": ""}], ensure_ascii=False), encoding="utf-8"
    )
    (extract_dir / "corrupt.json").write_text("{ this is not valid json !!!", encoding="utf-8")

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    stats = p.get_stats()

    assert stats["parse_errors"] == 1, (
        f"БАГ: повреждённый файл не учтён в parse_errors: {stats}"
    )
    assert stats["total_strings"] == 1, "Данные из корректного файла всё равно должны считаться"


def test_pipeline_get_stats_parse_errors_zero_when_all_files_valid(tmp_path):
    """Контроль: когда все файлы читаются нормально, parse_errors должен быть 0, а не отсутствовать."""
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    out_dir = tmp_path / "out"
    extract_dir = out_dir / "extracted"
    extract_dir.mkdir(parents=True)
    (extract_dir / "good.json").write_text(
        json.dumps([{"original": "こんにちは", "translated": ""}], ensure_ascii=False), encoding="utf-8"
    )

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    stats = p.get_stats()
    assert stats["parse_errors"] == 0
    assert "parse_errors" in stats  # ключ должен существовать всегда, не только при ошибках


def test_stats_panel_shows_parse_errors_warning():
    """
    Поведенческая проверка GUI: StatsPanel должен реально показывать
    предупреждение, когда parse_errors > 0, и скрывать его, когда всё
    чисто. Вкладка выбирается явно — иначе QTabWidget скрывает содержимое
    неактивной вкладки и isVisible() всегда даёт False независимо от
    setVisible(), что легко принять за баг там, где его нет.
    """
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import rpgmtl_desktop as rd

    win = rd.MainWindow()
    win.show()
    win.tabs.setCurrentWidget(win.stats_panel)

    win.stats_panel.refresh_stats({
        "total_strings": 100, "unique_strings": 50, "translated_strings": 10,
        "files": 3, "parse_errors": 2,
    })
    assert win.stats_panel.lbl_parse_errors.isVisible() is True
    assert "2" in win.stats_panel.lbl_parse_errors.text()

    win.stats_panel.refresh_stats({
        "total_strings": 100, "unique_strings": 50, "translated_strings": 10,
        "files": 3, "parse_errors": 0,
    })
    assert win.stats_panel.lbl_parse_errors.isVisible() is False


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 12, продолжение): точечный проход по extract/inject путям —
# 4 категории немых except, где реально терялось содержимое или информация
# об ошибке, без единого следа в логах.
# ─────────────────────────────────────────────────────────────────────────────

def test_rpgmv_inject_logs_when_marker_write_fails(tmp_path, monkeypatch):
    """
    Маркер идемпотентности (.rpgmtl_injected) писался в try/except: pass —
    сбой записи (диск, права) проходил немо, хотя сам перевод уже успешно
    вставлен. На следующем запуске без маркера инструмент может не понять,
    что инъекция уже была. Паттерн идентичен во всех 7 инжекторах движков —
    RPG Maker MV/MZ здесь как представитель.
    """
    import engine.injector as injector_mod

    game_dir = tmp_path / "game"
    (game_dir / "data").mkdir(parents=True)
    (game_dir / "data" / "Actors.json").write_text(
        json.dumps([None, {"id": 1, "name": "こんにちは"}], ensure_ascii=False), encoding="utf-8"
    )
    out_dir = tmp_path / "out"
    (out_dir / "extracted").mkdir(parents=True)
    entries = [{"file": "Actors.json", "path": "[1].name", "original": "こんにちは",
                "translated": "Привет", "game_type": "rpgmv"}]
    (out_dir / "extracted" / "Actors_translated.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )

    real_atomic_write_text = injector_mod.atomic_write_text

    def flaky_write(path, content, *a, **kw):
        if str(path).endswith(".rpgmtl_injected"):
            raise OSError("симулированный сбой диска")
        return real_atomic_write_text(path, content, *a, **kw)

    monkeypatch.setattr(injector_mod, "atomic_write_text", flaky_write)

    logs: list[str] = []
    result = injector_mod.inject_translations(game_dir, out_dir, log_callback=logs.append)

    assert result["total_injected"] == 1, "Сама вставка перевода не должна была пострадать"
    assert any("маркер" in l.lower() for l in logs), (
        f"БАГ: сбой записи маркера прошёл немо, без единой строки в логе:\n{logs}"
    )


def test_tyrano_inject_logs_skipped_entries_with_malformed_path(tmp_path):
    """
    Запись с нераспознанным path ("line:XYZ" вместо "line:5") молча
    терялась — int() падал с ValueError внутри голого except: pass, перевод
    для этой строки никогда не инжектился без единого сообщения.
    """
    from engine.tyrano_injector import inject_tyrano

    game_dir = tmp_path / "game"
    scenario_dir = game_dir / "data" / "scenario"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scene1.ks").write_text("こんにちは\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    (out_dir / "extracted").mkdir(parents=True)

    entries = [{
        "file": "scene1.ks.json", "_source_ks": "scene1.ks", "path": "line:XYZ",  # невалидный номер строки
        "original": "こんにちは", "full_line": "こんにちは", "translated": "Привет",
        "game_type": "tyrano", "context": "narrator",
    }]
    (out_dir / "extracted" / "scene1_translated.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )

    logs: list[str] = []
    inject_tyrano(game_dir, out_dir, log_callback=logs.append)
    assert any("нераспознанным path" in l or "line:XYZ" in l for l in logs), (
        f"БАГ: запись с невалидным path пропущена немо, без объяснения в логе:\n{logs}"
    )


def test_glossary_import_csv_propagates_error_instead_of_silent_partial_result(tmp_path):
    """
    БАГ: import_glossary_csv() был обёрнут в try/except Exception: pass
    целиком — сбой при открытии/чтении файла (напр. это на самом деле
    директория, не файл) молча возвращал count=0, как будто это и есть
    результат для пустого файла, а не сигнал об ошибке.
    """
    from engine.glossary import import_glossary_csv, Glossary

    g = Glossary()
    not_really_a_csv = tmp_path / "fake.csv"
    not_really_a_csv.mkdir()  # это ДИРЕКТОРИЯ, не файл — open() на ней упадёт

    with pytest.raises(Exception):
        import_glossary_csv(g, not_really_a_csv)


def test_glossary_import_csv_still_works_normally(tmp_path):
    """Контроль: обычный корректный CSV должен по-прежнему импортироваться без исключений."""
    from engine.glossary import import_glossary_csv, Glossary

    csv_path = tmp_path / "terms.csv"
    csv_path.write_text("original,translation\nこんにちは,Привет\n勇者,Герой\n", encoding="utf-8")
    g = Glossary()
    count = import_glossary_csv(g, csv_path)
    assert count == 2
    assert g.terms["こんにちは"] == "Привет"


def test_vxace_extractor_logs_when_map_has_unexpected_structure(tmp_path):
    """
    БАГ: если у объекта карты нет .attributes (неожиданная структура),
    events_hash молча оставался None — файл вносил в перевод ТОЛЬКО
    display_name, все диалоги/события карты терялись без единого сообщения.
    """
    from engine.vxace_extractor import VXAceExtractor

    class FakeMapNoAttributes:
        """Объект без .attributes вообще — вызовет AttributeError при .attributes.get(...)."""
        pass

    ext = VXAceExtractor(tmp_path, log_cb=lambda m: None)
    logs: list[str] = []
    ext._log = logs.append

    # Подменяем _load, чтобы вернуть заведомо "сломанный" объект карты
    ext._load = lambda path: FakeMapNoAttributes()

    fake_map = tmp_path / "Map001.rvdata2"
    fake_map.write_bytes(b"")
    ext._extract_map(fake_map)

    assert any("неожиданная структура" in l for l in logs), (
        f"БАГ: неожиданная структура карты обработана немо, без сообщения в логе:\n{logs}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 13, "переводят ли движки оптимально"): renpy_extractor.py уже
# резолвит переменную персонажа в отображаемое имя через char_names lookup
# (define girl1 = Character("Юки")) и кладёт результат в entry["speaker"] —
# но context для диалоговых строк Ren'Py всегда просто "dialog" (без
# embedded-имени, в отличие от Kirikiri "dialog:Имя"/Tyrano "say:Имя").
# pipeline.py строил Speaker-подсказку для промпта ТОЛЬКО парсингом
# префикса "say:"/"dialog:" из context — entry["speaker"] нигде не
# читался. Персонажи с цифрой/подчёркиванием в имени переменной (girl1,
# npc_guard — обычное дело в VN с большим составом) получали НОЛЬ
# контекста о том, кто говорит, хотя экстрактор эту информацию уже
# вычислил и просто не передавал дальше.
# ─────────────────────────────────────────────────────────────────────────────

def test_renpy_speaker_field_reaches_translation_prompt_context(tmp_path, monkeypatch):
    """
    Сквозная проверка: define с переменной, содержащей цифру (girl1),
    должна давать полноценный Speaker-контекст в промпте — через
    entry["speaker"], а не только через парсинг context-префикса.
    """
    import ai_translator
    from ai_translator import BatchResult, TranslationResult
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    game_sub = game_dir / "game"
    game_sub.mkdir(parents=True)
    (game_sub / "script.rpy").write_text(
        'define girl1 = Character("Юки")\n'
        'label start:\n'
        '    girl1 "こんにちは、旅人よ。"\n',
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    ok, msg = p.extract()
    assert ok, msg

    captured_contexts: list[list[str]] = []

    def fake_translate_texts(texts, config, log_callback=None, progress_callback=None,
                              state=None, cancel_event=None, contexts=None):
        captured_contexts.append(contexts)
        results = [TranslationResult(original=t, translated=f"TR:{t}", success=True) for t in texts]
        return BatchResult(results=results, total=len(texts), succeeded=len(texts), failed=0)

    monkeypatch.setattr(ai_translator, "translate_texts", fake_translate_texts)
    p.translate(provider="openai", api_key="fake-key", model="fake-model")

    assert captured_contexts, "translate_texts не был вызван"
    all_contexts = captured_contexts[0]
    assert any("Юки" in c for c in all_contexts), (
        f"БАГ: speaker 'Юки' (переменная 'girl1' с цифрой) не попал в контекст промпта: {all_contexts}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug (round 14, "чтобы движки не мешали друг другу"): Pipeline.inject()
# заново детектировал game_type живьём, вместо того чтобы использовать тот,
# что был сохранён в extract_index.json при Extract. Если файлы игры
# менялись между Extract и Inject настолько, что живая детекция теперь
# видит ДРУГОЙ движок (законный сценарий — см. detect_game_type_verbose и
# multi-match репортинг), Inject мог применить инжектор ОДНОГО движка к
# данным, извлечённым ДРУГИМ — форматы path/source_path несовместимы
# между движками.
# ─────────────────────────────────────────────────────────────────────────────

def test_pipeline_inject_uses_saved_game_type_not_live_redetection(tmp_path):
    """
    Извлекаем как RPG Maker MV. Между Extract и Inject добавляем в ту же
    папку Unity-маркеры (ProjectSettings/ProjectVersion.txt) — живая
    детекция теперь скажет "unity". Inject должен всё равно использовать
    RPG Maker логику (сохранённую при Extract), а не переключаться на
    Unity-инжектор для данных, извлечённых RPG Maker экстрактором.
    """
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    (game_dir / "data").mkdir(parents=True)
    (game_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "T"}), encoding="utf-8")
    (game_dir / "data" / "Actors.json").write_text(
        json.dumps([None, {"id": 1, "name": "こんにちは"}], ensure_ascii=False), encoding="utf-8"
    )
    (game_dir / "js").mkdir()
    (game_dir / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    p = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=lambda m: None)
    ok, msg = p.extract()
    assert ok, msg

    # Вручную создаём переведённый файл — как будто Translate уже прошёл
    ext_dir = out_dir / "extracted"
    jf = next(f for f in ext_dir.glob("*.json") if "_translated" not in f.name)
    entries = json.loads(jf.read_text(encoding="utf-8"))
    for e in entries:
        e["translated"] = "ПЕРЕВЕДЕНО"
    (ext_dir / (jf.stem + "_translated.json")).write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )

    # Между Extract и Inject добавляем Unity-маркеры в ту же папку
    (game_dir / "ProjectSettings").mkdir()
    (game_dir / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2022.3.1f1", encoding="utf-8"
    )
    assert p.get_game_type() == "unity", "Проверка сценария: живая детекция должна была измениться"

    logs: list[str] = []
    p2 = Pipeline(game_dir=game_dir, output_dir=out_dir, log_callback=logs.append)
    ok2, msg2 = p2.inject()
    assert ok2, msg2

    assert any("сохранён при Extract" in l for l in logs), (
        f"БАГ: Inject не использовал сохранённый при Extract game_type:\n{logs}"
    )
    final = json.loads((game_dir / "data" / "Actors.json").read_text(encoding="utf-8"))
    assert final[1]["name"] == "ПЕРЕВЕДЕНО", (
        "БАГ: перевод не применился — Inject, вероятно, попытался использовать чужой инжектор"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1-41 (внешний аудит): Ren'Py decompile пишет .rpy рядом с оригинальной
# игрой во время Extract — то есть Extract уже изменяет исходную установку
# игры до этапа Inject. Это не редкий случай: игры обычно распространяются
# только с .rpyc, без .rpy, так что декомпиляция — типичный путь. Полный
# редирект вывода в staging потребовал бы переделки путей во всём
# извлечении/инъекции Ren'Py — вместо этого: явное предупреждение перед
# стартом + манифест реально созданных файлов в output_dir, чтобы
# пользователь знал и мог почистить папку игры при желании.
# ─────────────────────────────────────────────────────────────────────────────

def test_renpy_decompile_warns_and_tracks_newly_created_files_in_manifest(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import types
    import renpy_extractor as rx

    game_dir = tmp_path / "game"
    (game_dir / "game").mkdir(parents=True)
    rpyc_path = game_dir / "game" / "script.rpyc"
    rpyc_path.write_bytes(b"fake rpyc content")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # Мокаем unrpyc: "декомпиляция" просто создаёт .rpy рядом, как настоящий unrpyc.
    fake_unrpyc = types.ModuleType("unrpyc")

    class FakeContext:
        state = "ok"
        error = ""

    def fake_decompile_rpyc(path, ctx):
        Path(path).with_suffix(".rpy").write_text(
            'label start:\n    "Привет"\n', encoding="utf-8"
        )

    fake_unrpyc.Context = FakeContext
    fake_unrpyc.decompile_rpyc = fake_decompile_rpyc
    monkeypatch.setitem(sys.modules, "unrpyc", fake_unrpyc)

    logs: list[str] = []
    n = rx._decompile_rpyc_files(game_dir / "game", log_cb=logs.append, output_dir=output_dir)

    assert n == 1
    rpy_path = rpyc_path.with_suffix(".rpy")
    assert rpy_path.exists(), "декомпилированный .rpy должен появиться рядом с .rpyc"

    full_log = "\n".join(logs)
    assert "ПРЯМО В ПАПКЕ ИГРЫ" in full_log, (
        "БАГ: нет явного предупреждения о том, что Extract создаёт файлы в папке игры"
    )

    manifest_path = output_dir / "decompiled_rpy_manifest.json"
    assert manifest_path.exists(), "БАГ: манифест созданных файлов не сохранён"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "script.rpy" in manifest, f"БАГ: script.rpy не попал в манифест: {manifest}"

    # Повторный запуск: .rpy уже существует -> не пересоздаётся, не варнинг про
    # "создаст новых файлов" (to_create пуст), манифест не меняется.
    logs.clear()
    n2 = rx._decompile_rpyc_files(game_dir / "game", log_cb=logs.append, output_dir=output_dir)
    assert n2 == 0
    assert "создаст" not in "\n".join(logs)







# ─────────────────────────────────────────────────────────────────────────────
# P0-10/P0-11 (внешний аудит): partial numbered AI response can be silently
# accepted, leaving individual translated lines empty within an otherwise
# "successful" batch.
# ─────────────────────────────────────────────────────────────────────────────

def test_translate_texts_retries_individually_missing_numbered_entry(monkeypatch):
    """
    BUG (P0-10/P0-11, внешний аудит, подтверждено чтением кода):
    _parse_numbered() возвращает список ожидаемой длины даже если модель
    вернула только часть нумерованных записей (например есть [1] и [3], но
    нет [2]) — пропущенная позиция остаётся пустой строкой. Батч-уровневый
    retry в _do_translate_batch срабатывает только если ПУСТЫХ ответов
    больше половины (empty_count > len(texts) * 0.5) — то есть батч из 3
    строк с ОДНОЙ пропущенной проходит как "успешный" без единой попытки
    восстановить именно эту позицию.

    Уже существующий per-line retry (batch_size=1, изначально только для
    "потерянных кодов") расширен, чтобы захватывать и этот случай — то же
    самое исправление, тот же механизм, просто шире условие отбора.
    Проверено в ОБОИХ режимах (max_workers=1 последовательный и
    max_workers>1 параллельный — код дублирован в обоих местах).
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import ai_translator

    class _PartialThenCompleteProvider:
        def __init__(self):
            self.batch_calls = 0

        def translate_batch(self, texts, log_cb=None):
            if len(texts) == 1:
                # Individual per-line retry call -> succeeds.
                return [f"RETRY:{texts[0]}"]
            self.batch_calls += 1
            # Simulate a numbered AI response missing the middle index --
            # exactly what _parse_numbered() produces for a genuinely
            # incomplete/malformed model response (has [1] and [3], no [2]).
            out = [f"T:{t}" for t in texts]
            mid = len(texts) // 2
            out[mid] = ""
            return out

    for workers in (1, 2):
        provider = _PartialThenCompleteProvider()
        monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg, p=provider: p)
        texts = ["один", "два", "три"]
        cfg = ai_translator.TranslatorConfig(
            provider="openai", api_key="sk-fake", model="gpt-4o-mini",
            target_lang="Russian", batch_size=10, max_workers=workers,
            use_global_tm=False,
        )
        result = ai_translator.translate_texts(texts, cfg)
        translated = {r.original: r.translated for r in result.results}
        assert translated["один"] == "T:один"
        assert translated["три"] == "T:три"
        assert translated["два"] == "RETRY:два", (
            f"БАГ (max_workers={workers}): пропущенная в неполном batch-ответе "
            f"строка не была восстановлена per-line retry'ем: {translated!r}"
        )

def test_godot_injector_refuses_non_utf8_file_instead_of_corrupting_bytes():
    """
    BUG (P1-16, внешний аудит, подтверждено эмпирически): godot_injector
    читал файлы с errors="replace" — невалидные UTF-8 байты молча
    заменялись на "�" ДО любой обработки, а потом файл целиком
    перезаписывался в UTF-8 — то есть повреждённые байты уничтожались
    НАВСЕГДА, даже в местах, которые перевод не затрагивает. Теперь: строгое
    чтение, при невалидном UTF-8 файл пропускается целиком без изменений
    (Godot официально требует UTF-8 для скриптов/ресурсов, так что легитимные
    файлы почти всегда и так валидны).
    """
    import sys, json, tempfile
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.godot_injector import inject_godot

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        game_dir.mkdir(); out_dir.mkdir()
        (game_dir / "project.godot").write_text("config_version=5", encoding="utf-8")
        bad = game_dir / "broken.gd"
        bad.write_bytes(b'a = tr("Fire")\n\xff\xfe broken bytes here\n')
        orig_bytes = bad.read_bytes()

        (out_dir / "extracted").mkdir()
        (out_dir / "extracted" / "broken_translated.json").write_text(json.dumps([
            {"file": "broken.gd", "_source_path": "broken.gd", "game_type": "godot",
             "path": "regex_tr:4", "original": "Fire", "translated": "Огонь"},
        ], ensure_ascii=False), encoding="utf-8")

        result = inject_godot(game_dir, out_dir, log_callback=lambda m: None, backup=False)
        assert result["total_errors"] == 1
        assert result["total_injected"] == 0
        assert bad.read_bytes() == orig_bytes, (
            "БАГ: невалидные UTF-8 байты были уничтожены заменой на '�'"
        )

def test_unity_yaml_injector_quotes_values_that_would_change_type():
    """
    BUG (P1-15, внешний аудит, подтверждено сверкой с реальным PyYAML
    resolver'ом): needs_quotes раньше проверял только наличие "особых
    символов" где-то в строке. Перевод БЕЗ единого такого символа всё равно
    может быть опасен как plain scalar — например "5" без кавычек читается
    YAML-парсером как ЦЕЛОЕ ЧИСЛО, не строка; "yes"/"true"/"null"/"~" — как
    bool/null. Игра, ожидающая строку в этом поле, молча получает другой
    тип после Inject.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.unity_injector import _inject_yaml_value

    def run(original, translation):
        lines = [f"greeting: {original}\n"]
        ok = _inject_yaml_value(lines, "yaml[0]", original, translation)
        assert ok, f"инъекция должна была примениться для {original!r} -> {translation!r}"
        return lines[0]

    # Значения, которые ДОЛЖНЫ быть в кавычках, иначе YAML сменит тип.
    for translation in ("5", "5.5", "-3", "true", "false", "yes", "no", "null", "~", "100"):
        result = run("Hello", translation)
        try:
            import yaml
            parsed = yaml.safe_load(result)
            value = list(parsed.values())[0] if isinstance(parsed, dict) else None
            assert isinstance(value, str) and value == translation, (
                f"БАГ: {translation!r} записан так, что YAML читает его не как строку: {result!r} -> {parsed!r}"
            )
        except ImportError:
            # PyYAML недоступен в среде выполнения теста -- хотя бы проверим,
            # что значение обёрнуто в кавычки.
            assert result.strip().endswith(('"', "'")), (
                f"БАГ: {translation!r} не заквочен: {result!r}"
            )

    # Контроль: обычный текстовый перевод не должен заквочиваться зря.
    plain = run("Hello", "Привет, герой")
    assert plain.rstrip("\n") == "greeting: Привет, герой", (
        f"обычный текст не должен заквочиваться: {plain!r}"
    )

def test_unity_yaml_file_not_rewritten_when_nothing_applied():
    """
    BUG (P1-14, внешний аудит, подтверждено чтением кода): .csv/.xml ветки
    inject_unity корректно пишут файл только при applied > 0 — .po и
    .yaml/.yml писали БЕЗУСЛОВНО, даже если ни одна запись не применилась
    (drift, пустой translated, path не найден). read_text().splitlines() +
    "\\n".join() без единого реального изменения всё равно мог поменять line
    endings (CRLF -> LF) файла — несогласованность с соседними ветками, не
    осознанное решение.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.unity_extractor import extract_unity
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        out_dir = tmp / "out"
        (tmp / "ProjectSettings").mkdir()
        (tmp / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1")

        yaml_file = game_dir / "StreamingAssets" / "config.yaml"
        yaml_file.write_text("greeting: こんにちは\r\n", encoding="utf-8")
        original_bytes = yaml_file.read_bytes()

        extract_unity(tmp, out_dir, log_callback=lambda m: None)
        ext_dir = out_dir / "extracted"
        jf = next(f for f in ext_dir.glob("*.json") if "config" in f.name and "_translated" not in f.name)
        entries = json.loads(jf.read_text(encoding="utf-8"))
        # Строка "изменилась" в игре после Extract -> drift, ни одна запись
        # не должна примениться (translated есть, но исходное значение уже
        # не совпадает).
        yaml_file.write_text("greeting: さようなら\r\n", encoding="utf-8")
        changed_bytes = yaml_file.read_bytes()
        for e in entries:
            e["translated"] = "Привет"
        (ext_dir / (jf.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_unity(tmp, out_dir, backup=False)
        after_bytes = yaml_file.read_bytes()
        assert after_bytes == changed_bytes, (
            f"БАГ: файл переписан (line endings/форматирование могли смениться), "
            f"хотя ни одна запись не применилась: {after_bytes!r} != {changed_bytes!r}"
        )

def test_global_tm_isolates_by_source_language():
    """
    BUG (P1-22, внешний аудит): tm_namespace уже смешивал
    target_lang+game_type+style_profile в одну составную строку (де-факто
    composite key внутри колонки target_lang) — но НЕ source_lang. Один и
    тот же текст, переведённый как часть проекта с ДРУГИМ source_lang, мог
    получить чужой закэшированный перевод из Global TM. GPT ошибался в
    части утверждения (engine/style УЖЕ были изолированы этим же трюком) —
    реально не хватало только source_lang.
    """
    import sys, tempfile, os
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import ai_translator
    from ai_translator import TranslatorConfig, translate_texts
    import global_tm

    with tempfile.TemporaryDirectory() as tmp:
        global_tm.GlobalTM.DB_PATH = Path(tmp) / "tm.db"

        def fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event):
            return True, [f"{cfg.source_lang[:2].upper()}-ПЕРЕВОД: {t}" for t in batch]

        original = ai_translator._do_translate_batch
        ai_translator._do_translate_batch = fake_do_translate_batch
        try:
            cfg_ja = TranslatorConfig(provider="openai", api_key="fake", source_lang="Japanese",
                                       target_lang="Russian", use_global_tm=True,
                                       batch_size=10, max_workers=1)
            translate_texts(["Fire"], cfg_ja, log_callback=None)

            call_log = []
            def tracking_batch(provider, batch, cfg, log_cb, cancel_event):
                call_log.append(list(batch))
                return fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event)
            ai_translator._do_translate_batch = tracking_batch

            cfg_en = TranslatorConfig(provider="openai", api_key="fake", source_lang="English",
                                       target_lang="Russian", use_global_tm=True,
                                       batch_size=10, max_workers=1)
            translate_texts(["Fire"], cfg_en, log_callback=None)

            sent = [t for batch in call_log for t in batch]
            assert "Fire" in sent, (
                f"БАГ: English-source запрос переиспользовал Japanese-source перевод "
                f"из Global TM вместо повторного перевода: call_log={call_log}"
            )
        finally:
            ai_translator._do_translate_batch = original

def test_global_tm_warns_once_on_db_error_instead_of_silent_or_spam():
    """
    BUG (P1-23, внешний аудит): любая ошибка SQLite (corruption, lock,
    permission, disk full) молча превращалась в обычный cache miss —
    пользователь никогда не узнавал, что Global TM перестала работать.
    Не делаем ошибку фатальной (GlobalTM — необязательная оптимизация,
    переводы обязаны продолжать работать без неё), но предупреждаем ОДИН
    РАЗ за сессию — не спамим при persistent-проблеме, которая иначе
    повторялась бы на каждом вызове.
    """
    import sys, tempfile, os, sqlite3
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from global_tm import GlobalTM

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RPGMTL_HOME"] = tmp
        logs = []
        tm = GlobalTM(target_lang="Russian", log_cb=logs.append).open()

        class BrokenConn:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("database is locked")

        tm._conn = BrokenConn()
        assert tm.get("hello world") is None, "деградация должна остаться мягкой (None, не исключение)"
        assert tm.get("another one") is None
        tm.set("third one", "третий")

        assert len(logs) == 1, f"БАГ: ожидалось ровно 1 warning за сессию, получено {len(logs)}: {logs}"
        assert "Global TM" in logs[0]

# ─────────────────────────────────────────────────────────────────────────────
# Найдено самостоятельно при углублённом аудите Kirikiri (после Godot/VXAce/
# RPG Maker): errors="replace" при кодировании перевода в исходную кодировку
# файла (shift_jis/cp932) молча превращал непредставимые символы в "?".
# ─────────────────────────────────────────────────────────────────────────────

def test_kirikiri_falls_back_to_utf8_bom_when_translation_has_lossy_chars():
    """
    BUG (найдено самостоятельно, подтверждено эмпирически): русский перевод
    записывался обратно в ИСХОДНУЮ кодировку .ks файла (часто shift_jis/
    cp932 у настоящих Kirikiri-игр) через encode(write_enc, errors="replace").
    Базовые кириллические буквы в cp932/shift_jis кодируются (расширенный
    блок) — но обычная русская типографика (тире "—", кавычки-«ёлочки»),
    которую ИИ-перевод на русский ставит естественно, в них НЕ входит и
    молча превращалась в "?" при каждой такой инъекции. Теперь: если
    перевод не переживает round-trip через исходную кодировку без потерь,
    файл сохраняется как UTF-8 с BOM вместо потери символов — extractor
    уже распознаёт и пробует этот формат первым при чтении (комментарий
    "Kirikiri файлы часто в Shift-JIS или UTF-8 с BOM"), это не изобретённый
    формат для данной экосистемы.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.kirikiri_injector import inject_kirikiri

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        (game_dir / "data" / "scenario").mkdir(parents=True)
        ks = game_dir / "data" / "scenario" / "first.ks"
        ks.write_bytes("太郎「こんにちは」\n".encode("shift_jis"))
        out_dir.mkdir()
        (out_dir / "extracted").mkdir()

        translation = "Герой — это тот, кто сказал: «Здравствуй»"
        (out_dir / "extracted" / "first_translated.json").write_text(json.dumps([
            {"file": "first.json", "_source_ks": "data/scenario/first.ks",
             "game_type": "kirikiri", "path": "1",
             "original": "こんにちは", "translated": translation},
        ], ensure_ascii=False), encoding="utf-8")

        result = inject_kirikiri(game_dir, out_dir, log_callback=lambda m: None, backup=False)
        assert result["total_injected"] == 1
        assert result["total_errors"] == 0

        raw = ks.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), (
            "БАГ: файл должен был переключиться на UTF-8 с BOM"
        )
        final_text = raw.decode("utf-8-sig")
        assert translation in final_text, (
            f"БАГ: перевод потерял типографские символы: {final_text!r}"
        )

        # Контроль: перевод БЕЗ проблемных символов не должен без нужды
        # переключать кодировку файла.
        ks2 = game_dir / "data" / "scenario" / "second.ks"
        ks2.write_bytes("太郎「こんにちは」\n".encode("shift_jis"))
        (out_dir / "extracted" / "second_translated.json").write_text(json.dumps([
            {"file": "second.json", "_source_ks": "data/scenario/second.ks",
             "game_type": "kirikiri", "path": "1",
             "original": "こんにちは", "translated": "Привет мир"},
        ], ensure_ascii=False), encoding="utf-8")
        inject_kirikiri(game_dir, out_dir, log_callback=lambda m: None, backup=False)
        raw2 = ks2.read_bytes()
        assert not raw2.startswith(b"\xef\xbb\xbf"), (
            "не должно переключать кодировку, если перевод и так умещается"
        )

def test_tyrano_shift_jis_project_extracts_and_injects_without_loss():
    """
    BUG (тот же класс, что найден и исправлен в Kirikiri): extractor/injector
    для Tyrano были жёстко привязаны к UTF-8 (errors="replace" на чтении,
    encoding="utf-8" на записи безусловно) без попытки определить исходную
    кодировку файла. TyranoScript — веб-движок (JS/HTML5), UTF-8 реалистичный
    дефолт, но часть TyranoBuilder-проектов исторически конвертировались из
    Kirikiri (тот же .ks/KAG-синтаксис) и могли унаследовать shift_jis/cp932.
    Если так, старое чтение молча превращало ВЕСЬ японский текст в "�" —
    Extract работал бы заметно неправильно (не тихая порча отдельных
    символов, а полностью нечитаемая экстракция). Теперь: определение
    кодировки при чтении (тот же приём, что в Kirikiri), и при записи —
    сохранение в исходной кодировке с fallback на UTF-8-BOM, если перевод
    содержит непредставимые в ней символы (например русская типографика).
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.tyrano_extractor import TyranoExtractor
    from engine.tyrano_injector import inject_tyrano

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        (game_dir / "data" / "scenario").mkdir(parents=True)
        ks = game_dir / "data" / "scenario" / "first.ks"
        ks.write_bytes('[cm]\n太郎「こんにちは、旅人よ」\n'.encode("shift_jis"))
        out_dir.mkdir()

        ext = TyranoExtractor(str(game_dir), log_cb=lambda m: None)
        entries = ext.extract()
        assert any("こんにちは" in e["original"] for e in entries), (
            "БАГ: экстракция из shift_jis дала мусор вместо японского текста"
        )

        ext_dir = out_dir / "extracted"
        ext_dir.mkdir()
        for e in entries:
            e["game_type"] = "tyrano"
        translation = "Герой — путник, идущий по дороге: «Здравствуй»"
        (ext_dir / "first_translated.json").write_text(json.dumps([
            {**e, "translated": translation}
            for e in entries if "こんにちは" in e["original"]
        ], ensure_ascii=False), encoding="utf-8")

        result = inject_tyrano(game_dir, out_dir, log_callback=lambda m: None, backup=False)
        assert result["total_injected"] == 1
        assert result["total_errors"] == 0

        raw = ks.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "файл должен был переключиться на UTF-8 с BOM"
        final_text = raw.decode("utf-8-sig")
        assert translation in final_text, f"БАГ: перевод потерял типографские символы: {final_text!r}"
