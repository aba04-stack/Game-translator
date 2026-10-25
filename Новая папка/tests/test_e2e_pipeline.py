"""
test_e2e_pipeline.py — полный end-to-end тест pipeline (fix: правильные ключи API).
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_rpgmv_game(game_dir: Path) -> None:
    data = game_dir / "data"
    data.mkdir(parents=True)
    (game_dir / "www").mkdir()
    (data / "System.json").write_text(
        json.dumps({"gameTitle": "テストゲーム"}), encoding="utf-8"
    )
    # RPG Maker MV/MZ: code 401 lines get merged into one entry by extractor
    map1 = {
        "events": [None, {
            "pages": [{"list": [
                {"code": 101, "parameters": ["Actor1", 0, 0, 2, "勇者"]},
                {"code": 401, "parameters": ["\\C[2]勇者\\C[0]よ、旅に出るのだ！"]},
                {"code": 401, "parameters": ["\\N[1]の運命は君にかかっている。"]},
                {"code": 0,   "parameters": []},
            ]}]
        }]
    }
    (data / "Map001.json").write_text(
        json.dumps(map1, ensure_ascii=False), encoding="utf-8"
    )
    actors = [None, {
        "id": 1, "name": "勇者", "nickname": "英雄", "profile": "王国の勇者だ。",
        "note": "", "classId": 1, "initialLevel": 1, "maxLevel": 99,
        "faceIndex": 0, "faceName": "", "characterIndex": 0,
        "characterName": "", "battlerName": "", "equips": [], "traits": [],
    }]
    (data / "Actors.json").write_text(
        json.dumps(actors, ensure_ascii=False), encoding="utf-8"
    )


def get_all_originals(extract_dir: Path) -> list[dict]:
    entries = []
    for jf in extract_dir.glob("*.json"):
        if not jf.name.endswith("_translated.json"):
            entries.extend(json.loads(jf.read_text(encoding="utf-8")))
    return entries


def write_fake_translations(extract_dir: Path, translation_map: dict) -> None:
    """Write *_translated.json using the EXACT original strings from extracted files."""
    for src in extract_dir.glob("*.json"):
        if src.name.endswith("_translated.json"):
            continue
        entries = json.loads(src.read_text(encoding="utf-8"))
        changed = False
        for e in entries:
            orig = e.get("original", "")
            if orig in translation_map:
                e["translated"] = translation_map[orig]
                changed = True
        if changed:
            out = extract_dir / (src.stem + "_translated.json")
            out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────

def test_extract_finds_japanese_text():
    """Extract must find Japanese text in RPG Maker MV/MZ files."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        ok, msg = Pipeline(str(gd), str(od)).extract()
        assert ok, f"Extract failed: {msg}"
        entries = get_all_originals(od / "extracted")
        originals = [e["original"] for e in entries if e.get("original")]
        assert any("勇者" in o for o in originals), f"勇者 not found: {originals}"


def test_extract_preserves_rpg_codes():
    """Extracted entries must keep RPG escape codes verbatim."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        Pipeline(str(gd), str(od)).extract()
        entries = get_all_originals(od / "extracted")
        dialog = next((e for e in entries if "\\C[2]" in e.get("original", "")), None)
        assert dialog is not None, "Dialog with \\C[2] not extracted"
        assert "\\C[2]" in dialog["original"]


def test_inject_after_mock_translate():
    """Full flow: extract → fake translate → inject → verify game files."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        p = Pipeline(str(gd), str(od))
        ok, _ = p.extract(); assert ok
        # Build translation map from actual extracted originals
        entries = get_all_originals(od / "extracted")
        tr_map = {}
        for e in entries:
            orig = e.get("original", "")
            if "勇者" in orig and "旅" in orig:
                # multi-line dialog joined with \n
                tr_map[orig] = orig.replace("勇者", "Герой").replace(
                    "旅に出るのだ！", "в путь!").replace(
                    "運命は君にかかっている。", "Судьба в твоих руках.")
            elif orig == "勇者":
                tr_map[orig] = "Герой"
            elif orig == "英雄":
                tr_map[orig] = "Богатырь"
            elif "勇者" in orig:
                tr_map[orig] = orig.replace("勇者", "Герой")
        write_fake_translations(od / "extracted", tr_map)
        ok2, msg2 = p.inject(apply_warp=False)
        assert ok2, f"Inject failed: {msg2}"
        map_data = json.loads((gd / "data" / "Map001.json").read_text(encoding="utf-8"))
        dialog_lines = [
            e["parameters"][0]
            for e in map_data["events"][1]["pages"][0]["list"]
            if e["code"] == 401
        ]
        combined = " ".join(dialog_lines)
        assert "Герой" in combined, f"No translation in: {dialog_lines}"


def test_inject_preserves_rpg_codes():
    """Injected text must preserve \\C[2] and \\N[1] codes."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        p = Pipeline(str(gd), str(od))
        p.extract()
        entries = get_all_originals(od / "extracted")
        tr_map = {}
        for e in entries:
            orig = e.get("original", "")
            if "\\C[2]" in orig:
                tr_map[orig] = orig.replace("勇者", "Герой").replace(
                    "旅に出るのだ！", "в путь!").replace(
                    "運命は君にかかっている。", "Судьба.")
            elif orig == "勇者":
                tr_map[orig] = "Герой"
            elif orig == "英雄":
                tr_map[orig] = "Богатырь"
            elif "勇者" in orig:
                tr_map[orig] = orig.replace("勇者", "Герой")
        write_fake_translations(od / "extracted", tr_map)
        p.inject(apply_warp=False)
        map_data = json.loads((gd / "data" / "Map001.json").read_text(encoding="utf-8"))
        all_text = " ".join(
            e["parameters"][0] for e in map_data["events"][1]["pages"][0]["list"]
            if e["code"] == 401
        )
        assert "\\C[2]" in all_text, f"\\C[2] lost after inject: {all_text!r}"


def test_inject_idempotency():
    """Double inject must not duplicate or corrupt text."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        p = Pipeline(str(gd), str(od))
        p.extract()
        entries = get_all_originals(od / "extracted")
        tr_map = {e["original"]: e["original"].replace("勇者", "Герой").replace(
            "英雄", "Богатырь").replace("旅に出るのだ！", "в путь!").replace(
            "運命は君にかかっている。", "Судьба.").replace("王国の勇者だ。", "Герой.")
            for e in entries if e.get("original")}
        write_fake_translations(od / "extracted", tr_map)
        assert p.inject(apply_warp=False)[0]
        assert p.inject(apply_warp=False)[0]
        data = json.loads((gd / "data" / "Map001.json").read_text(encoding="utf-8"))
        lines = [e["parameters"][0] for e in data["events"][1]["pages"][0]["list"] if e["code"] == 401]
        for l in lines:
            assert l.count("Герой") <= 1, f"Text doubled: {l!r}"


def test_backup_created_on_inject():
    """inject(backup=True) must create backup files."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        p = Pipeline(str(gd), str(od))
        p.extract()
        entries = get_all_originals(od / "extracted")
        tr_map = {e["original"]: "Перевод" for e in entries if e.get("original")}
        write_fake_translations(od / "extracted", tr_map)
        p.inject(apply_warp=False, backup=True)
        backup_dir = gd / "_backup_original"
        assert backup_dir.exists(), "Backup dir not created"
        assert list(backup_dir.glob("*.json")), "No backup files"


def test_backup_restore_via_injector():
    """Backup exists → injector restores originals before re-injecting."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        p = Pipeline(str(gd), str(od))
        p.extract()
        entries = get_all_originals(od / "extracted")
        tr_map1 = {e["original"]: "[v1] " + e["original"][:10] for e in entries if e.get("original")}
        tr_map2 = {e["original"]: "[v2] " + e["original"][:10] for e in entries if e.get("original")}
        write_fake_translations(od / "extracted", tr_map1)
        p.inject(apply_warp=False, backup=True)
        # Now update to v2
        write_fake_translations(od / "extracted", tr_map2)
        p.inject(apply_warp=False, backup=True)
        # Game files should have v2, not v1
        actors = json.loads((gd / "data" / "Actors.json").read_text(encoding="utf-8"))
        actor_name = actors[1]["name"]
        assert "[v1]" not in actor_name, f"Old translation still present: {actor_name!r}"
        assert "[v2]" in actor_name or actor_name == "勇者", f"Unexpected: {actor_name!r}"


def test_progress_callbacks_receive_fname():
    """Extract and inject progress_callback must pass filename as 4th arg."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        extract_calls, inject_calls = [], []
        def on_prog_extract(c, t, eta=-1.0, fname=""):
            extract_calls.append((c, t, eta, fname))
        def on_prog_inject(c, t, eta=-1.0, fname=""):
            inject_calls.append((c, t, eta, fname))
        p = Pipeline(str(gd), str(od))
        p.extract(progress_callback=on_prog_extract)
        entries = get_all_originals(od / "extracted")
        # BUG FIX: translating every original to a bare "X" drops the
        # \C[2]/\N[1]-style RPG Maker codes present in this fixture's dialogue
        # lines. That's a genuine critical QA finding (code loss can break the
        # game), not a pipeline bug — pre-Inject QA is correctly refusing to
        # inject it. This test only cares about the progress_callback contract,
        # so keep each original's codes in order (same rule QA/_validate_codes
        # enforces) instead of stripping them.
        from ai_translator import _extract_codes
        tr_map = {
            e["original"]: "".join(_extract_codes(e["original"])) + "X"
            for e in entries if e.get("original")
        }
        write_fake_translations(od / "extracted", tr_map)
        p.inject(apply_warp=False, progress_callback=on_prog_inject)
        assert extract_calls, "No extract progress calls"
        assert inject_calls, "No inject progress calls"
        # eta must be float, fname must be str
        for call in extract_calls:
            assert isinstance(call[2], float), f"eta not float: {call}"
            assert isinstance(call[3], str),  f"fname not str: {call}"
        for call in inject_calls:
            assert isinstance(call[2], float), f"eta not float: {call}"
            assert isinstance(call[3], str),  f"fname not str: {call}"
        assert any(c[3].endswith(".json") for c in extract_calls), "No .json fname"


def test_get_stats_keys_exist():
    """get_stats() must return known keys with correct types."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        p = Pipeline(str(gd), str(od))
        stats_before = p.get_stats()
        assert "extract_done" in stats_before
        assert "total_strings" in stats_before
        assert "unique_strings" in stats_before
        assert "translated_strings" in stats_before
        assert stats_before["extract_done"] is False
        p.extract()
        stats_after = p.get_stats()
        assert stats_after["extract_done"] is True
        assert stats_after["total_strings"] > 0
        assert stats_after["unique_strings"] > 0
        assert stats_after["translated_strings"] == 0


def test_get_stats_after_fake_translate():
    """get_stats() translated_strings must increase after writing _translated.json."""
    with tempfile.TemporaryDirectory() as tmp:
        gd = Path(tmp) / "game"; od = Path(tmp) / "out"
        make_rpgmv_game(gd); od.mkdir()
        from pipeline import Pipeline
        p = Pipeline(str(gd), str(od))
        p.extract()
        entries = get_all_originals(od / "extracted")
        write_fake_translations(od / "extracted",
            {e["original"]: "Перевод" for e in entries if e.get("original")})
        Pipeline._stats_class_cache.clear()  # force re-read
        stats = p.get_stats()
        # All UNIQUE strings should now be marked translated
        assert stats["translated_strings"] == stats["unique_strings"], \
            f"Expected translated={stats['unique_strings']}, got {stats['translated_strings']}"
        assert stats["remaining_strings"] == 0, f"remaining_strings: {stats}"


def test_deepseek_clean_translation_in_pipeline():
    """_parse_numbered strips DeepSeek markdown and preserves RPG codes."""
    from ai_translator import _parse_numbered
    raw = (
        "[1] \\C[2]**Герой**\\C[0], отправляйся в путь!\n"
        "[2] Судьба **великого** \\N[1] зависит от тебя.\n"
        "[3] Герой королевства.\n"
        "Note: kept RPG codes intact as instructed."
    )
    results = _parse_numbered(raw, 3)
    assert "**" not in results[0], f"Bold in [0]: {results[0]!r}"
    assert "**" not in results[1], f"Bold in [1]: {results[1]!r}"
    assert "Note" not in results[2], f"Note leaked: {results[2]!r}"
    assert "\\C[2]" in results[0], f"\\C[2] lost: {results[0]!r}"
    assert "\\N[1]" in results[1], f"\\N[1] lost: {results[1]!r}"
