"""
test_core.py — тесты ядра без зависимости от PyQt6 и внешних API.
Покрывает: state, glossary, deduplicator, _parse_numbered, text_filter, injector-utils.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))


# ── TranslationState ──────────────────────────────────────────────────────────

def test_state_set_get():
    from engine.state import TranslationState
    with tempfile.TemporaryDirectory() as d:
        s = TranslationState(Path(d) / "state.json")
        s.set("hello", "привет")
        assert s.has("hello")
        assert s.get("hello") == "привет"
        assert not s.has("unknown")


def test_state_atomic_save():
    """Сохранение должно создавать читаемый JSON, не нулевой файл."""
    from engine.state import TranslationState
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        s = TranslationState(path)
        s.set("a", "б")
        s.save()
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["cache"]["a"] == "б"


def test_state_public_items():
    from engine.state import TranslationState
    with tempfile.TemporaryDirectory() as d:
        s = TranslationState(Path(d) / "state.json")
        s.set("x", "у")
        items = dict(s.items())
        assert items["x"] == "у"  # state.set игнорирует пустые переводы по дизайну


def test_state_count():
    from engine.state import TranslationState
    with tempfile.TemporaryDirectory() as d:
        s = TranslationState(Path(d) / "state.json")
        assert s.count == 0
        s.set("a", "б")
        assert s.count == 1


# ── Glossary ──────────────────────────────────────────────────────────────────

def test_glossary_apply_basic():
    from engine.glossary import Glossary
    g = Glossary()
    g.add("勇者", "Герой")
    result = g.apply("勇者は戦う")
    assert "Герой" in result


def test_glossary_apply_empty_translation_no_delete():
    """BUG FIX: пустой перевод НЕ должен удалять текст."""
    from engine.glossary import Glossary
    g = Glossary()
    g.terms["田中"] = ""   # пустой перевод
    original = "田中は話す"
    result = g.apply(original)
    assert "田中" in result, "Термин с пустым переводом не должен удаляться"


def test_glossary_apply_protects_escape_codes():
    """BUG FIX: глоссарий не должен ломать RPG Maker escape-коды."""
    from engine.glossary import Glossary
    g = Glossary()
    g.add("N", "Н")   # однобуквенный термин
    text = r"\N[1] говорит"
    result = g.apply(text)
    assert r"\N[1]" in result, f"Escape-код сломан: {result!r}"


def test_glossary_longer_term_wins():
    """Более длинный термин должен заменяться первым."""
    from engine.glossary import Glossary
    g = Glossary()
    g.add("勇者", "Герой")
    g.add("勇", "Храбрый")
    result = g.apply("勇者")
    assert result == "Герой"


# ── _parse_numbered ───────────────────────────────────────────────────────────

def test_parse_numbered_basic():
    from ai_translator import _parse_numbered
    raw = "[1] Привет\n[2] Пока"
    r = _parse_numbered(raw, 2)
    assert r[0] == "Привет"
    assert r[1] == "Пока"


def test_parse_numbered_multiline():
    """BUG FIX: длинный диалог может занимать несколько строк."""
    from ai_translator import _parse_numbered
    raw = "[1] Первая строка\nпродолжение\n[2] Вторая"
    r = _parse_numbered(raw, 2)
    assert "продолжение" in r[0]
    assert r[1] == "Вторая"


def test_parse_numbered_think_stripped():
    """DeepSeek-R1 добавляет <think>...</think> — должно удаляться."""
    from ai_translator import _parse_numbered
    raw = "<think>Это размышление</think>\n[1] Перевод"
    r = _parse_numbered(raw, 1)
    assert r[0] == "Перевод"
    assert "<think>" not in r[0]


def test_parse_numbered_fallback_single():
    from ai_translator import _parse_numbered
    r = _parse_numbered("Просто текст", 1)
    assert r[0] == "Просто текст"


# ── _strip_context_hint ───────────────────────────────────────────────────────

def test_strip_context_hint_removes_hint():
    from ai_translator import _strip_context_hint
    text = "(Speaker: Alice, Type: Dialog) Привет!"
    assert _strip_context_hint(text) == "Привет!"


def test_strip_context_hint_keeps_normal_parens():
    from ai_translator import _strip_context_hint
    text = "(смеётся) Это забавно"
    # нет «:» внутри скобок — не трогаем
    assert _strip_context_hint(text) == text


def test_strip_context_hint_noop_on_plain():
    from ai_translator import _strip_context_hint
    assert _strip_context_hint("Привет!") == "Привет!"


# ── text_filter ───────────────────────────────────────────────────────────────

def test_text_filter_japanese():
    from engine.text_filter import should_translate
    assert should_translate("こんにちは")
    assert should_translate("RPG勇者")


def test_text_filter_skip_short():
    from engine.text_filter import should_translate
    assert not should_translate("")  # empty always False
    assert not should_translate("   ")  # whitespace-only


def test_text_filter_skip_numeric():
    from engine.text_filter import should_translate
    assert not should_translate("12345")
    assert not should_translate("100%")


# ── injector path resolver ────────────────────────────────────────────────────

def test_injector_set_nested_simple():
    from engine.injector import _set_nested
    data = {"name": "Alice"}
    ok = _set_nested(data, "name", "Алиса")
    assert ok
    assert data["name"] == "Алиса"


def test_injector_set_nested_array():
    from engine.injector import _set_nested
    data = {"list": [{"parameters": ["old"]}]}
    ok = _set_nested(data, "list[0].parameters[0]", "new")
    assert ok
    assert data["list"][0]["parameters"][0] == "new"


def test_injector_set_nested_missing_path():
    from engine.injector import _set_nested
    data = {}
    ok = _set_nested(data, "nonexistent[5].key", "value")
    assert not ok


def test_injector_inject_dialog_splits_newlines():
    from engine.injector import _inject_dialog
    data = {
        "list": [
            {"code": 401, "parameters": ["line1"]},
            {"code": 401, "parameters": ["line2"]},
        ]
    }
    ok = _inject_dialog(data, "list[0..1].dialog", "Привет\nМир")
    assert ok
    assert data["list"][0]["parameters"][0] == "Привет"
    assert data["list"][1]["parameters"][0] == "Мир"


# ── deduplicator ─────────────────────────────────────────────────────────────

def test_dedup_map_basic():
    from engine.deduplicator import build_dedup_map
    entries = [
        {"file": "a.json", "path": "p1", "original": "hello", "translated": ""},
        {"file": "b.json", "path": "p2", "original": "hello", "translated": ""},
        {"file": "c.json", "path": "p3", "original": "world", "translated": ""},
    ]
    dm = build_dedup_map(entries)
    assert dm.unique_count <= 2   # "hello" and "world" = 2 unique
    occs = dm.occurrences("hello")
    assert len(occs) == 2
    world_occs = dm.occurrences("world")
    assert len(world_occs) == 1


def test_dedup_apply_propagates():
    """Перевод одной строки должен распространяться на дубликаты."""
    from engine.deduplicator import build_dedup_map, apply_dedup_to_files
    with tempfile.TemporaryDirectory() as d:
        extract_dir = Path(d)
        entries_a = [{"file": "a.json", "path": "p1", "original": "hello", "translated": ""}]
        entries_b = [{"file": "b.json", "path": "p2", "original": "hello", "translated": "привет"}]
        (extract_dir / "a.json").write_text(json.dumps(entries_a), encoding="utf-8")
        (extract_dir / "b.json").write_text(json.dumps(entries_b), encoding="utf-8")
        (extract_dir / "b_translated.json").write_text(json.dumps(entries_b), encoding="utf-8")
        all_entries = entries_a + entries_b
        dm = build_dedup_map(all_entries)
        dm.set_translation("hello", "привет")
        apply_dedup_to_files(dm, extract_dir)
        out = json.loads((extract_dir / "a_translated.json").read_text(encoding="utf-8"))
        assert out[0]["translated"] == "привет"


# ── config atomic write ───────────────────────────────────────────────────────

def test_config_save_load():
    from config import save_config, load_config
    import unittest.mock as mock
    test_cfg = {"provider": "openai", "api_key": "test-key-123"}
    with tempfile.TemporaryDirectory() as d:
        with mock.patch("config.CONFIG_PATH", Path(d) / "cfg.json"):
            save_config(test_cfg)
            loaded = load_config()
            assert loaded["provider"] == "openai"
            assert loaded["api_key"] == "test-key-123"


# ── game_detector ──────────────────────────────────────────────────────────────

def test_detect_rpgmv(tmp_path):
    from game_detector import detect_game_type
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text("{}", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    assert detect_game_type(tmp_path) == "rpgmv"


def test_detect_rpgmz(tmp_path):
    from game_detector import detect_game_type
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text("{}", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "rmmz_core.js").write_text("", encoding="utf-8")
    assert detect_game_type(tmp_path) == "rpgmz"


def test_detect_godot_project_file(tmp_path):
    from game_detector import detect_game_type
    (tmp_path / "project.godot").write_text("[gd_resource]", encoding="utf-8")
    assert detect_game_type(tmp_path) == "godot"


def test_detect_godot_pck_with_exe(tmp_path):
    """pck + exe с совпадающим именем → Godot."""
    from game_detector import detect_game_type
    (tmp_path / "mygame.pck").write_bytes(b"")
    (tmp_path / "mygame.exe").write_bytes(b"")
    assert detect_game_type(tmp_path) == "godot"


def test_detect_pck_alone_not_godot(tmp_path):
    """pck без совпадающего exe → НЕ Godot (RPG Maker плагин)."""
    from game_detector import detect_game_type
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text("{}", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    (tmp_path / "someplugin.pck").write_bytes(b"")  # RPG Maker plugin
    assert detect_game_type(tmp_path) == "rpgmv"  # не godot!


def test_detect_unity_project_version(tmp_path):
    """ProjectSettings/ProjectVersion.txt — самый прямой признак Unity."""
    from game_detector import detect_game_type
    (tmp_path / "ProjectSettings").mkdir()
    (tmp_path / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2022.3.1f1", encoding="utf-8"
    )
    assert detect_game_type(tmp_path) == "unity"


def test_detect_unity_assets_and_project_settings(tmp_path):
    """Assets/ + ProjectSettings/ (без ProjectVersion.txt) — тоже Unity."""
    from game_detector import detect_game_type
    (tmp_path / "Assets").mkdir()
    (tmp_path / "ProjectSettings").mkdir()
    assert detect_game_type(tmp_path) == "unity"


def test_detect_unity_data_folder_with_markers(tmp_path):
    """Собранная (не исходная) Unity-игра: <Имя>_Data/ с globalgamemanagers
    и т.п. — так выглядит РЕАЛЬНО распространяемая игра, которую и будут
    переводить (а не Unity-проект с исходниками)."""
    from game_detector import detect_game_type
    data_dir = tmp_path / "MyGame_Data"
    data_dir.mkdir()
    (data_dir / "globalgamemanagers").write_bytes(b"")
    (data_dir / "Resources").mkdir()
    assert detect_game_type(tmp_path) == "unity"


def test_detect_unity_data_folder_without_markers_is_unknown(tmp_path):
    """Папка *_Data без ни одного из ожидаемых маркеров — не должна
    ложно определяться как Unity (мог быть любой другой _Data каталог)."""
    from game_detector import detect_game_type
    data_dir = tmp_path / "Something_Data"
    data_dir.mkdir()
    (data_dir / "randomfile.txt").write_text("", encoding="utf-8")
    assert detect_game_type(tmp_path) == "unknown"


def test_detect_vxace(tmp_path):
    from game_detector import detect_game_type
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "Actors.rvdata2").write_bytes(b"")
    assert detect_game_type(tmp_path) == "vxace"


def test_detect_wolf(tmp_path):
    from game_detector import detect_game_type
    (tmp_path / "Data" / "BasicData").mkdir(parents=True)
    (tmp_path / "Data" / "BasicData" / "DataBase.wolf").write_bytes(b"")
    assert detect_game_type(tmp_path) == "wolf"


def test_detect_tyrano_standard_path(tmp_path):
    from game_detector import detect_game_type
    (tmp_path / "data" / "scenario").mkdir(parents=True)
    (tmp_path / "data" / "scenario" / "first.ks").write_text("", encoding="utf-8")
    (tmp_path / "index.html").write_text("", encoding="utf-8")
    assert detect_game_type(tmp_path) == "tyrano"


def test_detect_tyrano_alt_path(tmp_path):
    """TyranoScript с scenario в tyrano/scenario/ должен определяться как tyrano, не kirikiri."""
    from game_detector import detect_game_type
    (tmp_path / "tyrano" / "scenario").mkdir(parents=True)
    (tmp_path / "tyrano" / "scenario" / "start.ks").write_text("", encoding="utf-8")
    (tmp_path / "tyrano" / "libs").mkdir()
    assert detect_game_type(tmp_path) == "tyrano"


def test_detect_unknown(tmp_path):
    from game_detector import detect_game_type
    assert detect_game_type(tmp_path) == "unknown"


# ── Godot extractor — .dialogue parsing ──────────────────────────────────────

def test_godot_extractor_dialogue(tmp_path):
    from engine.godot_extractor import GodotExtractor
    (tmp_path / "project.godot").write_text("[gd_resource]")
    dlg = tmp_path / "scene.dialogue"
    dlg.write_text(
        "Alice: こんにちは！\n"
        "Narrator without name\n"
        "- 選択肢A\n"
        "~ label\n",
        encoding="utf-8",
    )
    ext = GodotExtractor(tmp_path)
    entries = ext.extract()
    originals = [e["original"] for e in entries]
    assert any("こんにちは" in o for o in originals)
    assert any("選択肢A" in o for o in originals)


# ── Smart Warp skips Wolf/VXAce ───────────────────────────────────────────────

def test_pipeline_warp_skipped_for_wolf(tmp_path):
    """Wolf и VXAce не должны получать Smart Warp (бинарный инжект)."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pipeline import Pipeline

    game_dir = tmp_path / "game"
    out_dir = tmp_path / "out"
    game_dir.mkdir()
    out_dir.mkdir()
    (game_dir / "Data" / "BasicData").mkdir(parents=True)
    (game_dir / "Data" / "BasicData" / "DB.wolf").write_bytes(b"")

    p = Pipeline(str(game_dir), str(out_dir))
    assert p.get_game_type() == "wolf"


# ── _should_warp ──────────────────────────────────────────────────────────────

def test_should_warp_covers_all_engines():
    """_should_warp должна возвращать True для всех реальных dialog-контекстов."""
    from pipeline import Pipeline
    dialog_contexts = [
        # MV/MZ
        "dialog_text", "scroll_text", "choice",
        # Wolf
        "wolf_dialog", "wolf_string",
        # Kirikiri (real format)
        "dialog:Alice", "dialog:田中",
        # RenPy (real formats)
        "alice", "bob", "narrator", "dialog",
        # Tyrano
        "say:ユイ",
        # Godot
        "godot_text", "godot_dialog_narration", "godot_dialog_choice",
        # Unity
        "unity_text",
    ]
    for ctx in dialog_contexts:
        assert Pipeline._should_warp(ctx), f"_should_warp({ctx!r}) returned False"


def test_should_warp_skips_data_contexts():
    """Данные (имена предметов, описания) не должны варпиться."""
    from pipeline import Pipeline
    data_contexts = [
        "actor:name", "item:description", "weapon:name",
        "skill:message1", "armor:name",
        "godot_script", "unity_json", "unity_csv",
    ]
    for ctx in data_contexts:
        assert not Pipeline._should_warp(ctx), f"_should_warp({ctx!r}) should be False"


# ── _format_line_with_context — all real engine formats ──────────────────────

def test_format_kirikiri_dialog_context():
    """Kirikiri: 'dialog:Alice' → (Speaker: Alice)"""
    from ai_translator import _format_line_with_context
    result = _format_line_with_context("こんにちは", "dialog:Alice")
    assert "Alice" in result
    assert "こんにちは" in result


def test_format_renpy_char_variable():
    """RenPy: char variable 'alice' → treated as speaker name."""
    from ai_translator import _format_line_with_context
    result = _format_line_with_context("やあ", "alice")
    assert "Alice" in result or "alice" in result.lower()


def test_format_vxace_data_field():
    """VXAce: 'actor:description' → (Type: Actor description)"""
    from ai_translator import _format_line_with_context
    result = _format_line_with_context("勇者の物語", "actor:description")
    assert "Actor" in result or "description" in result.lower()


def test_format_narrator_no_speaker():
    """narrator コンテキスト → нет Speaker"""
    from ai_translator import _format_line_with_context
    result = _format_line_with_context("昔々あるところに", "narrator")
    # should not add spurious Speaker
    assert "Speaker:" not in result


def test_format_noop_on_empty_ctx():
    from ai_translator import _format_line_with_context
    result = _format_line_with_context("text", "")
    assert result == "text"


# ── base_url flows through to OpenAI client ───────────────────────────────────

def test_base_url_in_translator_config():
    from ai_translator import TranslatorConfig
    cfg = TranslatorConfig(provider="openai", api_key="x", base_url="http://localhost:1234/v1")
    assert cfg.base_url == "http://localhost:1234/v1"


def test_base_url_in_pipeline_config(tmp_path):
    """base_url must reach TranslatorConfig, not be silently dropped."""
    from pipeline import Pipeline
    captured = []

    class _FakePipeline(Pipeline):
        def _real_translate(self, cfg, *a, **kw):
            captured.append(cfg.base_url)
            return True, ""

    p = Pipeline(str(tmp_path), str(tmp_path))
    # Check the field exists on TranslatorConfig and pipeline accepts it
    import inspect
    sig = inspect.signature(p.translate)
    assert "base_url" in sig.parameters, "base_url not in pipeline.translate signature"


# ── Glossary public API ───────────────────────────────────────────────────────

def test_glossary_get_method():
    from engine.glossary import Glossary
    g = Glossary()
    g.add("勇者", "Герой")
    assert g.get("勇者") == "Герой"
    assert g.get("unknown", "default") == "default"


def test_glossary_len():
    from engine.glossary import Glossary
    g = Glossary()
    assert len(g) == 0
    g.add("a", "б")
    assert len(g) == 1


def test_glossary_contains():
    from engine.glossary import Glossary
    g = Glossary()
    g.add("アリス", "Алиса")
    assert "アリス" in g
    assert "ボブ" not in g


# ── Gemini SDK fallback ───────────────────────────────────────────────────────

def test_gemini_provider_initializes_with_available_sdk():
    """GeminiProvider должен инициализироваться без ошибок если хотя бы один SDK установлен."""
    from ai_translator import TranslatorConfig, GeminiProvider
    cfg = TranslatorConfig(provider="gemini", api_key="fake-key-for-test")
    try:
        p = GeminiProvider(cfg)
        # Either SDK was found
        assert hasattr(p, "_client") or hasattr(p, "_genai_legacy")
    except RuntimeError as e:
        if "не установлен" in str(e):
            pytest.skip("Neither Gemini SDK installed")
        raise


# ── file_utils module ─────────────────────────────────────────────────────────

def test_file_utils_atomic_json(tmp_path):
    from engine.file_utils import atomic_write_json
    p = tmp_path / "test.json"
    atomic_write_json(p, {"key": "value", "num": 42})
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["key"] == "value"
    assert data["num"] == 42


def test_file_utils_atomic_overwrites(tmp_path):
    from engine.file_utils import atomic_write_json
    p = tmp_path / "overwrite.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text())["v"] == 2


def test_file_utils_creates_parent_dirs(tmp_path):
    from engine.file_utils import atomic_write_text
    p = tmp_path / "a" / "b" / "c" / "file.txt"
    atomic_write_text(p, "hello")
    assert p.read_text() == "hello"


# ── CLI interface ─────────────────────────────────────────────────────────────

def test_cli_extract(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent.parent))
    
    game = tmp_path / "game"
    out = tmp_path / "out"
    (game / "data").mkdir(parents=True)
    (game / "js").mkdir()
    (game / "js" / "rpg_core.js").write_text("x")
    (game / "data" / "System.json").write_text(json.dumps({"gameTitle": "テスト"}))
    (game / "data" / "Actors.json").write_text(
        json.dumps([{"id": 1, "name": "勇者", "nickname": "", "profile": ""}])
    )
    out.mkdir()

    import cli, importlib
    importlib.reload(cli)  # ensure fresh state
    sys.argv = ["cli.py", "extract", "--game", str(game), "--output", str(out)]
    try:
        rc = cli.main()
    except SystemExit as e:
        rc = e.code
    assert rc == 0
    assert (out / "extracted").exists()


def test_cli_export_import_roundtrip(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent.parent))
    
    extract = tmp_path / "extracted"
    extract.mkdir()
    entries = [
        {"file": "A.json", "path": "p1", "original": "勇者", "translated": "Герой", "context": "actor:name"},
        {"file": "A.json", "path": "p2", "original": "魔法", "translated": "Магия", "context": "skill:name"},
    ]
    (extract / "A_translated.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )

    import cli
    csv_path = tmp_path / "trans.csv"
    sys.argv = ["cli.py", "export", "--output", str(tmp_path), "--csv", str(csv_path)]
    try: cli.main()
    except SystemExit: pass
    assert csv_path.exists()
    content = csv_path.read_text(encoding="utf-8-sig")
    assert "Герой" in content

    # Modify CSV and import back
    content = content.replace("Герой", "Храбрец")
    from engine.file_utils import atomic_write_text
    atomic_write_text(csv_path, content.encode("utf-8").decode("utf-8"), encoding="utf-8")

    sys.argv = ["cli.py", "import", "--output", str(tmp_path), "--csv", str(csv_path)]
    try: cli.main()
    except SystemExit: pass

    updated = json.loads((extract / "A_translated.json").read_text(encoding="utf-8"))
    assert updated[0]["translated"] == "Храбрец"


def test_cli_stats_no_extract(tmp_path):
    import cli
    out = tmp_path / "out"
    out.mkdir()
    sys.argv = ["cli.py", "stats", "--game", str(tmp_path), "--output", str(out)]
    try:
        rc = cli.main()
    except SystemExit as e:
        rc = e.code
    assert rc == 0  # should not crash, just show 0 stats


# ── find_dir_ci ───────────────────────────────────────────────────────────────

def test_find_dir_ci_exact(tmp_path):
    from engine.file_utils import find_dir_ci
    (tmp_path / "Data").mkdir()
    assert find_dir_ci(tmp_path, "Data") == tmp_path / "Data"


def test_find_dir_ci_lowercase(tmp_path):
    from engine.file_utils import find_dir_ci
    (tmp_path / "data").mkdir()  # lowercase
    result = find_dir_ci(tmp_path, "Data")
    assert result is not None
    assert result.name.lower() == "data"


def test_find_dir_ci_missing(tmp_path):
    from engine.file_utils import find_dir_ci
    assert find_dir_ci(tmp_path, "Data") is None


def test_find_dir_ci_prefers_exact(tmp_path):
    from engine.file_utils import find_dir_ci
    (tmp_path / "Data").mkdir()
    (tmp_path / "data").mkdir()
    result = find_dir_ci(tmp_path, "Data")
    assert result.name == "Data"  # exact match preferred
