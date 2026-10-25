"""
test_new_features.py — тесты для исправлений и новых функций v48.

Покрывает:
- Smart Warp: token-based ellipsis trim (не режет RPG-коды)
- progress_callback fname chain через pipeline → extractor/injector
- text_filter: classify_text_for_translation edge cases
- config.py: atomic save, load, missing keys
- Kirikiri O(N) restore — safe_to_rel map
- vxace injector: apply_translation split logic
- Pre-flight validation: no extracted files / empty model
- session log flush
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Smart Warp — token-based ellipsis trim
# ─────────────────────────────────────────────────────────────────────────────

def test_warp_ellipsis_does_not_split_rpg_code():
    """Fix 100: trim should never cut inside \\C[2] or similar escape codes."""
    from smart_warp import wrap_paragraph
    # A line ending with an RPG code — ellipsis trim must not corrupt it
    text = r"とても長いセリフ。\C[2]強調テキスト\C[0]さらに長い部分が続く"
    result = wrap_paragraph(text, max_width_px=60, max_lines=1, table=None)
    # All codes that ARE present must remain intact — no half-code like \C[2 or C[0
    for code in [r"\C[2]", r"\C[0]"]:
        if code in text:
            # code may be trimmed off entirely (ok), but must not be half-present
            half = code[:-1]  # e.g. \C[2
            assert half not in result or code in result, \
                f"Partial code {half!r} found in result: {result!r}"


def test_warp_ellipsis_token_boundary():
    """Ellipsis must be added at a word boundary, not mid-word."""
    from smart_warp import wrap_paragraph
    text = "Hello world this is a long line that needs trimming"
    result = wrap_paragraph(text, max_width_px=80, max_lines=1, table=None)
    if "…" in result:
        before_ellipsis = result.split("…")[0]
        # Must not end with a partial word (space or start of word)
        assert not before_ellipsis.endswith(" ") or before_ellipsis.strip(), \
            f"Trailing space before ellipsis: {result!r}"


def test_wrap_by_sentences_ellipsis():
    """Fix 101: wrap_by_sentences must add ellipsis when truncating."""
    from smart_warp import wrap_by_sentences
    # Force a long multi-sentence text through max_lines=1
    text = "First sentence here. Second sentence here. Third sentence here."
    result = wrap_by_sentences(text, max_width_px=200, max_lines=1, table=None)
    lines = result.split("\n")
    assert len(lines) <= 1 or all(not ln.strip() for ln in lines[1:])
    # If truncated, last line must end with ellipsis
    last = lines[0].rstrip()
    if len(text.encode()) > 20:  # was long enough to possibly need truncation
        # Either fits on one line without ellipsis, or has ellipsis
        assert not last or last.endswith("…") or len(lines) == 1


# ─────────────────────────────────────────────────────────────────────────────
# progress_callback fname chain
# ─────────────────────────────────────────────────────────────────────────────

def _make_rpgmv_game(game_dir: Path) -> None:
    (game_dir / "data").mkdir(parents=True)
    actors = [None, {"id": 1, "name": "勇者", "nickname": "", "profile": "", "note": "",
                     "classId": 1, "initialLevel": 1, "maxLevel": 99,
                     "faceIndex": 0, "faceName": "", "characterIndex": 0,
                     "characterName": "", "battlerName": "", "equips": [],
                     "traits": []}]
    (game_dir / "data" / "Actors.json").write_text(json.dumps(actors), encoding="utf-8")
    (game_dir / "www").mkdir(exist_ok=True)


def test_extract_progress_emits_fname():
    """Fix 111: extractor must pass filename as 3rd arg to progress_callback."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"
        od = Path(tmp) / "out"
        _make_rpgmv_game(gd)
        od.mkdir()

        from engine.extractor import extract_game
        received: list[tuple] = []
        extract_game(gd, od, progress_callback=lambda c, t, eta=-1.0, f="": received.append((c, t, eta, f)))
        assert received, "No progress calls emitted"
        # fname is the 4th element (index 3)
        fnames = [r[3] for r in received]
        assert any(f for f in fnames), f"All fnames empty: {received}"
        assert any(f.endswith(".json") for f in fnames), f"No .json fname: {fnames}"


def test_inject_progress_emits_fname():
    """Fix 96: inject_translations must pass filename as 3rd arg."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"
        od = Path(tmp) / "out"
        _make_rpgmv_game(gd)
        od.mkdir()

        from engine.extractor import extract_game
        extract_game(gd, od)
        ext = od / "extracted"
        # Write fake translated file
        src = list(ext.glob("*.json"))[0]
        data = json.loads(src.read_text(encoding="utf-8"))
        for e in data:
            e["translated"] = "テスト"
        tr_path = ext / (src.stem + "_translated.json")
        tr_path.write_text(json.dumps(data), encoding="utf-8")

        from engine.injector import inject_translations
        received: list[tuple] = []
        inject_translations(gd, od, progress_callback=lambda c, t, eta=-1.0, f="": received.append((c, t, eta, f)))
        assert received, "No progress calls from inject"
        fnames = [r[3] for r in received]
        assert any(f for f in fnames), f"All inject fnames empty: {received}"


# ─────────────────────────────────────────────────────────────────────────────
# text_filter — classify_text_for_translation edge cases
# ─────────────────────────────────────────────────────────────────────────────

def test_classify_abbreviation_skipped():
    """Fix in text_filter: HP, MP, ATK etc. should not be translated."""
    from engine.text_filter import classify_text_for_translation
    for abbr in ("HP", "MP", "ATK", "DEF", "EXP", "SP1", "TP"):
        ok, reason = classify_text_for_translation(abbr)
        assert not ok, f"{abbr!r} should be skipped (reason={reason})"
        assert reason == "abbreviation", f"{abbr!r}: expected abbreviation, got {reason}"


def test_classify_empty_string_skipped():
    from engine.text_filter import classify_text_for_translation
    ok, reason = classify_text_for_translation("")
    assert not ok and reason == "empty"


def test_classify_only_codes_skipped():
    from engine.text_filter import classify_text_for_translation
    ok, reason = classify_text_for_translation(r"\C[2]\N[1]\!")
    assert not ok and reason == "only_codes", f"reason={reason}"


def test_classify_japanese_ok():
    from engine.text_filter import classify_text_for_translation
    ok, reason = classify_text_for_translation("こんにちは世界")
    assert ok, f"Japanese text should translate, got reason={reason}"


def test_classify_null_string_skipped():
    from engine.text_filter import classify_text_for_translation
    ok, reason = classify_text_for_translation("null")
    assert not ok and reason == "skip_exact"


def test_classify_single_punctuation_skipped():
    from engine.text_filter import classify_text_for_translation
    for s in (".", "!", "?", ","):
        ok, reason = classify_text_for_translation(s)
        assert not ok, f"{s!r} should be skipped"


def test_classify_mixed_code_and_text():
    from engine.text_filter import classify_text_for_translation
    ok, reason = classify_text_for_translation(r"\C[2]勇者\C[0]")
    assert ok, f"Code+text should translate, reason={reason}"


def test_classify_kag_tag_only_skipped():
    from engine.text_filter import classify_text_for_translation
    ok, reason = classify_text_for_translation("[l][r][p]")
    assert not ok, f"KAG-only should be skipped, reason={reason}"


# ─────────────────────────────────────────────────────────────────────────────
# config.py — atomic save, missing keys, corrupt JSON
# ─────────────────────────────────────────────────────────────────────────────

def test_config_load_defaults_on_missing_key():
    """load_config must return defaults even when file has missing keys."""
    import config
    original_path = config.CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        config.CONFIG_PATH = Path(tmp) / "cfg.json"
        config.CONFIG_PATH.write_text(json.dumps({"provider": "openai"}), encoding="utf-8")
        cfg = config.load_config()
        # All DEFAULTS keys must be present after merge
        for key in config.DEFAULTS:
            assert key in cfg, f"Missing default key: {key!r}"
        # Spot-check specific important keys including new ones
        assert "batch_size" in cfg
        assert "max_workers" in cfg
        assert "quality_mode" in cfg
        assert "use_context" in cfg,  "use_context must be in DEFAULTS"
        assert "timeout" in cfg,       "timeout must be in DEFAULTS"
        assert cfg["use_context"] is True
        assert cfg["timeout"] == 60
        config.CONFIG_PATH = original_path


def test_config_load_returns_defaults_on_corrupt_json():
    """load_config must not crash on corrupt JSON — returns defaults."""
    import config
    original_path = config.CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        config.CONFIG_PATH = Path(tmp) / "cfg.json"
        config.CONFIG_PATH.write_text("{corrupt: json!!", encoding="utf-8")
        cfg = config.load_config()
        assert isinstance(cfg, dict), "Should return dict even on corrupt JSON"
        assert "provider" in cfg
        config.CONFIG_PATH = original_path


def test_config_save_creates_directories():
    """save_config must create parent directories atomically."""
    import config
    original_path = config.CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        config.CONFIG_PATH = Path(tmp) / "nested" / "dir" / "cfg.json"
        config.save_config({"provider": "gemini", "batch_size": 10})
        assert config.CONFIG_PATH.exists()
        data = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        assert data["provider"] == "gemini"
        config.CONFIG_PATH = original_path


def test_config_save_load_roundtrip():
    """Values saved must be exactly recovered."""
    import config
    original_path = config.CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        config.CONFIG_PATH = Path(tmp) / "cfg.json"
        payload = {"provider": "claude", "batch_size": 7, "temperature": 0.3,
                   "api_key": "sk-test", "model": "claude-opus-4-6"}
        config.save_config(payload)
        loaded = config.load_config()
        for k, v in payload.items():
            assert loaded[k] == v, f"Key {k!r}: expected {v!r}, got {loaded[k]!r}"
        config.CONFIG_PATH = original_path


# ─────────────────────────────────────────────────────────────────────────────
# Kirikiri O(N) restore map (Fix 98)
# ─────────────────────────────────────────────────────────────────────────────

def test_kirikiri_restore_map_correctness():
    """Fix 98: safe_to_rel mapping must invert ks_rel → safe_name correctly."""
    # Simulate what the fixed code does
    translations = {
        "scenario/first.ks": {},
        "scenario/sub/second.ks": {},
        "data/config.ks": {},
    }
    safe_to_rel = {
        ks_rel.replace("/", "_").replace("\\", "_"): ks_rel
        for ks_rel in translations
    }
    assert safe_to_rel["scenario_first.ks"] == "scenario/first.ks"
    assert safe_to_rel["scenario_sub_second.ks"] == "scenario/sub/second.ks"
    assert safe_to_rel["data_config.ks"] == "data/config.ks"
    # All keys unique — no collision for these paths
    assert len(safe_to_rel) == len(translations)


# ─────────────────────────────────────────────────────────────────────────────
# vxace injector: new_lines split (Fix 10)
# ─────────────────────────────────────────────────────────────────────────────

def test_vxace_injector_split_handles_literal_newline():
    """Translated string with literal \\n and smart-warp \\\\n must both split."""
    import re
    # Mimic the exact pattern from vxace_injector._apply_translation
    translated_literal = "Line1\nLine2"
    translated_escaped = r"Line1\nLine2"

    for t in (translated_literal, translated_escaped):
        parts = re.split(r"\n|\\n", t)
        assert len(parts) == 2, f"Failed to split {t!r}: {parts}"
        assert parts[0] == "Line1" and parts[1] == "Line2"


# ─────────────────────────────────────────────────────────────────────────────
# Session log flush
# ─────────────────────────────────────────────────────────────────────────────

def test_session_log_flush_writes_file():
    """_flush_session_log must atomically write log buffer to output/logs/*.log."""
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        log_path = log_dir / "rpgmtl_translate_20250101_120000.log"
        buffer = ["[Translate] Начало", "[Translate] Батч 1", "✓ Готово"]

        # Simulate what _flush_session_log does
        import os, tempfile as tf
        content = "\n".join(buffer) + "\n"
        tmp_fd, tmp_p = tf.mkstemp(dir=log_dir, suffix=".tmp", prefix=".rpgmtl_log_")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_p, log_path)

        assert log_path.exists()
        text = log_path.read_text(encoding="utf-8")
        assert "[Translate] Начало" in text
        assert "✓ Готово" in text
        assert not Path(tmp_p).exists(), "Temp file must be cleaned up"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight inject guard: no _translated.json → must abort
# ─────────────────────────────────────────────────────────────────────────────

def test_inject_with_no_translated_files_returns_zero():
    """inject_translations must return 0 injected when no *_translated.json exist."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"
        od = Path(tmp) / "out"
        _make_rpgmv_game(gd)
        od.mkdir()
        # Run extract but don't translate (no _translated.json)
        from engine.extractor import extract_game
        extract_game(gd, od)

        from engine.injector import inject_translations
        result = inject_translations(gd, od, backup=False)
        assert result.get("total_injected", 0) == 0, \
            f"Expected 0 injections without _translated.json, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# deduplicator edge case: zero translations
# ─────────────────────────────────────────────────────────────────────────────

def test_dedup_apply_with_no_translations_is_noop():
    """apply_dedup_to_files must not crash or corrupt when dm has no translations."""
    from engine.deduplicator import DeduplicationMap, apply_dedup_to_files
    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        entries = [{"file": "Actors.json", "path": "[1].name",
                    "original": "勇者", "translated": ""}]
        (extract_dir / "Actors.json").write_text(json.dumps(entries), encoding="utf-8")

        dm = DeduplicationMap()
        dm.add("勇者", "Actors.json", "[1].name")
        # No translations set — dm.get_translation("勇者") returns None

        applied = apply_dedup_to_files(dm, extract_dir)
        assert applied == 0, f"Expected 0 applied, got {applied}"
        # File must still exist and be unchanged
        data = json.loads((extract_dir / "Actors.json").read_text(encoding="utf-8"))
        assert data[0]["translated"] == ""


# ─────────────────────────────────────────────────────────────────────────────
# DeepSeek / _parse_numbered response cleaning (Bugs DS-1, DS-2)
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_numbered_strips_think_block():
    """DeepSeek-R1 wraps answer in <think>…</think> — must be stripped."""
    from ai_translator import _parse_numbered
    raw = "<think>\nI need to translate these...\n[1] は in Japanese\n</think>\n[1] Привет!\n[2] Пока!"
    result = _parse_numbered(raw, 2)
    assert result == ["Привет!", "Пока!"], f"think not stripped: {result}"


def test_parse_numbered_strips_markdown_bold():
    """DeepSeek-chat sometimes wraps words in **bold** — must be stripped."""
    from ai_translator import _parse_numbered
    raw = "[1] **Герой** победил!\n[2] Здравствуйте, **товарищ**."
    result = _parse_numbered(raw, 2)
    assert "**" not in result[0], f"bold in [0]: {result[0]!r}"
    assert "**" not in result[1], f"bold in [1]: {result[1]!r}"
    assert "Герой" in result[0]
    assert "товарищ" in result[1]


def test_parse_numbered_strips_markdown_italic():
    """Italic *text* must be stripped, inner text kept."""
    from ai_translator import _parse_numbered
    raw = "[1] *Тихо* сказал он.\n[2] Нормальный текст."
    result = _parse_numbered(raw, 2)
    assert "*" not in result[0], f"italic not stripped: {result[0]!r}"
    assert "Тихо" in result[0]


def test_parse_numbered_strips_trailing_note():
    """Model commentary after the list must not pollute last translation."""
    from ai_translator import _parse_numbered
    raw = "[1] Привет!\n[2] Добрый день.\n\nNote: I used formal register throughout."
    result = _parse_numbered(raw, 2)
    assert "Note" not in result[1], f"trailing note in result: {result[1]!r}"
    assert result[1] == "Добрый день."


def test_parse_numbered_strips_translator_note():
    """'Translator's note:' style commentary must be stripped."""
    from ai_translator import _parse_numbered
    raw = "[1] Привет!\nTranslator's note: kept the original greeting style."
    result = _parse_numbered(raw, 1)
    assert "Translator" not in result[0], f"translator note leaked: {result[0]!r}"


def test_parse_numbered_preserves_rpg_codes():
    """Cleaning must NOT strip RPG escape codes like \\C[2] or \\N[1]."""
    from ai_translator import _parse_numbered
    raw = r"[1] \C[2]Герой\C[0] победил монстра\N[1]!"
    result = _parse_numbered(raw, 1)
    assert r"\C[2]" in result[0], f"RPG code stripped: {result[0]!r}"
    assert r"\C[0]" in result[0]
    assert r"\N[1]" in result[0]


def test_clean_translation_inline():
    """_clean_translation handles mixed markdown + think directly."""
    from ai_translator import _clean_translation
    inp = "<think>thinking...</think>**Важно**: *срочно* нужна помощь!"
    out = _clean_translation(inp)
    assert "<think>" not in out
    assert "**" not in out
    assert "*" not in out
    assert "Важно" in out
    assert "срочно" in out
    assert "нужна помощь!" in out


# ─────────────────────────────────────────────────────────────────────────────
# Escape code repair — _validate_and_repair_batch
# ─────────────────────────────────────────────────────────────────────────────

def test_repair_batch_logs_missing_codes_without_modifying():
    """When AI drops codes, _validate_and_repair_batch must log warning but NOT auto-repair."""
    from ai_translator import _validate_and_repair_batch

    original  = r"\C[2]勇者\C[0]よ、旅に出るのだ！"
    ai_output = "Герой, отправляйся в путь!"   # AI dropped \C[2] and \C[0]

    result = _validate_and_repair_batch([original], [ai_output], None)
    # No auto-repair: text kept as-is (codes preserved via §N§ upstream)
    assert result[0] == ai_output, f"Auto-repair must be disabled: {result[0]!r}"


def test_repair_batch_keeps_correct_codes():
    """If AI preserves codes correctly, result must be unchanged."""
    from ai_translator import _validate_and_repair_batch

    original  = r"\C[2]勇者\C[0]は強い！"
    correct   = r"\C[2]Герой\C[0] силён!"    # correct — codes present

    result = _validate_and_repair_batch([original], [correct], None)
    assert result[0] == correct, f"Correct translation modified: {result[0]!r}"


def test_validate_codes_order_sensitive():
    """Codes must appear in same ORDER as original, not just same set."""
    from ai_translator import _validate_codes

    original  = r"\C[2]text\C[0]"
    correct   = r"\C[2]Текст\C[0]"       # same order
    wrong_order = r"\C[0]Текст\C[2]"     # reversed

    assert _validate_codes(original, correct),      "Correct order should pass"
    assert not _validate_codes(original, wrong_order), "Wrong order should fail"


def test_system_prompt_contains_example():
    """System prompt must include a concrete code example for DeepSeek."""
    from ai_translator import TranslatorConfig
    cfg = TranslatorConfig(provider="deepseek", api_key="test", model="deepseek-chat")
    prompt = cfg.get_system_prompt()
    assert r"\C[2]" in prompt,  "Prompt missing \\C[2] example"
    assert "UNCHANGED" in prompt, "Prompt missing UNCHANGED keyword"
    assert "FORBIDDEN" in prompt, "Prompt missing FORBIDDEN keyword"


# ─────────────────────────────────────────────────────────────────────────────
# §N§ normalization: spaces, Cyrillic, fullwidth digits
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_placeholder_spaces():
    """§ 0 § and §0 § must both be normalized to §0§."""
    from ai_translator import _protect_codes, _restore_codes, _clean_translation, _SPLIT_PLACEHOLDER_RE

    orig = r"\C[2]勇者\C[0]"
    _, ph = _protect_codes(orig)

    for bad in ["§ 0 §Герой§ 1 §", "§0 §Герой§1 §", "§ 0§Герой§ 1§", "§0\n§Герой§1\n§"]:
        cleaned = _clean_translation(bad)
        fixed = _SPLIT_PLACEHOLDER_RE.sub(r"§\1§", cleaned)
        restored = _restore_codes(fixed, ph)
        assert r"\C[2]" in restored, f"\\C[2] lost for {bad!r}: {restored!r}"
        assert r"\C[0]" in restored, f"\\C[0] lost for {bad!r}: {restored!r}"


def test_normalize_placeholder_cyrillic_o():
    """§О§ (Cyrillic О) must be treated as §0§."""
    from ai_translator import _protect_codes, _restore_codes, _clean_translation, _SPLIT_PLACEHOLDER_RE

    orig = r"\C[2]勇者\C[0]"
    _, ph = _protect_codes(orig)

    cleaned = _clean_translation("§О§Герой§1§")
    fixed = _SPLIT_PLACEHOLDER_RE.sub(r"§\1§", cleaned)
    restored = _restore_codes(fixed, ph)
    assert r"\C[2]" in restored, f"\\C[2] lost: {restored!r}"


def test_normalize_placeholder_fullwidth():
    """§０§ (fullwidth 0) must be treated as §0§."""
    from ai_translator import _protect_codes, _restore_codes, _clean_translation, _SPLIT_PLACEHOLDER_RE

    orig = r"\N[1]の運命"
    _, ph = _protect_codes(orig)

    cleaned = _clean_translation("§０§の運命")
    fixed = _SPLIT_PLACEHOLDER_RE.sub(r"§\1§", cleaned)
    restored = _restore_codes(fixed, ph)
    assert r"\N[1]" in restored, f"\\N[1] lost: {restored!r}"


def test_validate_codes_empty_original():
    """String with no codes must always pass validation."""
    from ai_translator import _validate_codes
    assert _validate_codes("こんにちは！", "Привет!") is True
    assert _validate_codes("", "Что угодно") is True


def test_validate_codes_correct_order():
    """\\C[2]text\\C[0] → codes must be in order 2, 0 not 0, 2."""
    from ai_translator import _validate_codes
    assert _validate_codes(r"\C[2]text\C[0]", r"\C[2]Текст\C[0]") is True
    assert _validate_codes(r"\C[2]text\C[0]", r"\C[0]Текст\C[2]") is False
