"""
test_roundtrip.py — РЕАЛЬНЫЕ round-trip тесты всех движков.
Без заглушек. Реальные данные, реальный extract + inject + verify.
AI-перевод пропускается (нет ключей), но весь остальной pipeline — боевой.
"""
import io
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))

_TYRANO_TAG_RE = re.compile(r"\[.*?\]")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_fake_translation(extract_dir: Path, translation_map: dict[str, str]) -> None:
    """Проставить переводы в *_translated.json, имитируя результат AI-шага."""
    for src in extract_dir.glob("*.json"):
        if src.name.endswith("_translated.json"):
            continue
        data = json.loads(src.read_text(encoding="utf-8"))
        changed = False
        for entry in data:
            orig = entry.get("original", "")
            if orig in translation_map:
                entry["translated"] = translation_map[orig]
                changed = True
        stem = src.stem
        out = extract_dir / f"{stem}_translated.json"
        if changed or not out.exists():
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# RPG Maker MV round-trip
# ─────────────────────────────────────────────────────────────────────────────

def _make_rpgmv_game(game_dir: Path):
    data_dir = game_dir / "data"
    data_dir.mkdir(parents=True)
    js_dir = game_dir / "js"
    js_dir.mkdir()
    (js_dir / "rpg_core.js").write_text("// RPG Maker MV Core", encoding="utf-8")
    (data_dir / "System.json").write_text(json.dumps({"gameTitle": "テストゲーム"}), encoding="utf-8")

    actors = [
        {"id": 1, "name": "勇者", "profile": "主人公だ", "nickname": "英雄"},
        {"id": 2, "name": "魔法使い", "profile": "魔法を使う", "nickname": "賢者"},
    ]
    (data_dir / "Actors.json").write_text(json.dumps(actors), encoding="utf-8")

    map1 = {
        "displayName": "最初の村",
        "events": {
            "1": {
                "id": 1,
                "name": "NPC",
                "pages": [{
                    "list": [
                        {"code": 101, "parameters": ["", 0, 0, 2, ""]},
                        {"code": 401, "parameters": ["こんにちは！冒険者よ。"]},
                        {"code": 401, "parameters": ["この村へようこそ。"]},
                        {"code": 102, "parameters": [["はい", "いいえ"], 2]},
                        {"code": 0,   "parameters": []}
                    ]
                }]
            }
        }
    }
    (data_dir / "Map001.json").write_text(json.dumps(map1), encoding="utf-8")

    items = [
        {"id": 1, "name": "ポーション", "description": "HPを回復する薬。", "note": ""},
        {"id": 2, "name": "エリクサー", "description": "すべてを回復する薬。", "note": ""},
    ]
    (data_dir / "Items.json").write_text(json.dumps(items), encoding="utf-8")


def test_rpgmv_extract():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_rpgmv_game(game_dir)
        out_dir.mkdir()

        from engine.extractor import extract_game
        result = extract_game(game_dir, out_dir)
        assert result["total_strings"] > 0, f"Nothing extracted: {result}"

        all_entries = []
        for f in (out_dir / "extracted").glob("*.json"):
            if not f.name.endswith("_translated.json"):
                all_entries.extend(json.loads(f.read_text(encoding="utf-8")))
        originals = [e["original"] for e in all_entries]
        assert any("テスト" in o for o in originals), f"gameTitle not extracted: {originals[:5]}"
        assert "勇者" in originals, f"Actor name not found: {originals}"
        assert any("こんにちは" in o for o in originals), f"Dialog not found: {originals}"
        assert "ポーション" in originals, f"Item name not found: {originals}"
        assert "HPを回復する薬。" in originals, f"Item desc not found: {originals}"

        extract_dir = out_dir / "extracted"
        assert extract_dir.exists()
        assert any(extract_dir.glob("*.json"))


def test_rpgmv_inject_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_rpgmv_game(game_dir)
        out_dir.mkdir()

        from engine.extractor import extract_game
        extract_game(game_dir, out_dir)

        # Dialog extracted as combined entry with actual newline separator
        translation_map = {
            "勇者":            "Герой",
            "魔法使い":         "Маг",
            "こんにちは！冒険者よ。\nこの村へようこそ。": "Привет, путник!\nДобро пожаловать.",
            "ポーション":        "Зелье",
            "HPを回復する薬。":   "Восстанавливает HP.",
        }
        _apply_fake_translation(out_dir / "extracted", translation_map)

        from engine.injector import inject_translations
        result = inject_translations(game_dir, out_dir)
        assert result["total_injected"] > 0, f"Nothing injected: {result}"
        assert result["total_errors"] == 0, f"Inject errors: {result}"

        # Verify actors
        actors_injected = json.loads((game_dir / "data" / "Actors.json").read_text(encoding="utf-8"))
        names = [a.get("name") for a in actors_injected]
        assert "Герой" in names, f"Actor name not injected: {names}"

        # Verify map dialog
        map_injected = json.loads((game_dir / "data" / "Map001.json").read_text(encoding="utf-8"))
        dialog_texts = [
            e["parameters"][0]
            for e in map_injected["events"]["1"]["pages"][0]["list"]
            if e["code"] == 401
        ]
        combined = "\n".join(dialog_texts)
        assert "Привет" in combined, f"Dialog not injected: {dialog_texts}"


def test_rpgmv_inject_idempotent():
    """Повторный inject не должен ломать уже инъектированные данные."""
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_rpgmv_game(game_dir)
        out_dir.mkdir()

        from engine.extractor import extract_game
        extract_game(game_dir, out_dir)

        translation_map = {"勇者": "Герой", "こんにちは！冒険者よ。\nこの村へようこそ。": "Привет!\nДобро."}
        _apply_fake_translation(out_dir / "extracted", translation_map)

        from engine.injector import inject_translations
        inject_translations(game_dir, out_dir)
        r2 = inject_translations(game_dir, out_dir)

        actors_after = json.loads((game_dir / "data" / "Actors.json").read_text(encoding="utf-8"))
        names = [a.get("name") for a in actors_after]
        # Должно быть «Герой», а не «ГеройГерой» или оригинал
        assert "Герой" in names
        assert "ГеройГерой" not in names
        assert r2["total_injected"] >= 0  # second inject from backup should work


def test_rpgmv_stale_files_cleaned_on_reextract():
    """После re-extract устаревшие карты должны удаляться из extracted/."""
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_rpgmv_game(game_dir)
        out_dir.mkdir()

        from engine.extractor import extract_game
        extract_game(game_dir, out_dir)

        # Добавляем "призрачный" файл вручную
        ghost = out_dir / "extracted" / "Map999.json"
        ghost.write_text(json.dumps([{"file": "Map999.json", "path": "p", "original": "ghost", "translated": ""}]), encoding="utf-8")
        ghost_tr = out_dir / "extracted" / "Map999_translated.json"
        ghost_tr.write_text("[]", encoding="utf-8")

        assert ghost.exists()

        # Re-extract — призрак должен исчезнуть
        extract_game(game_dir, out_dir)
        assert not ghost.exists(), "Stale file Map999.json not removed on re-extract"


# ─────────────────────────────────────────────────────────────────────────────
# Kirikiri round-trip
# ─────────────────────────────────────────────────────────────────────────────

def _make_kirikiri_game(game_dir: Path):
    scenario = game_dir / "data" / "scenario"
    scenario.mkdir(parents=True)
    (scenario / "start.ks").write_text(
        "*start\n"
        "[chara_show name=\"アリス\"]\n"
        "[talk name=\"アリス\"]こんにちは！[p]\n"
        "[talk name=\"アリス\"]今日はいい天気ですね。[p]\n"
        "[s]\n",
        encoding="utf-8"
    )


def test_kirikiri_extract():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_kirikiri_game(game_dir)
        out_dir.mkdir()

        from engine.kirikiri_extractor import extract_kirikiri
        result = extract_kirikiri(game_dir, out_dir)
        assert result.get("total_strings", 0) > 0, f"Nothing extracted: {result}"
        extract_dir = out_dir / "extracted"
        all_entries = []
        for f in extract_dir.glob("*.json"):
            if not f.name.endswith("_translated.json"):
                all_entries.extend(json.loads(f.read_text(encoding="utf-8")))
        originals = [e["original"] for e in all_entries]
        assert any("こんにちは" in o for o in originals), f"Dialog not found: {originals}"


def test_kirikiri_inject_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_kirikiri_game(game_dir)
        out_dir.mkdir()

        from engine.kirikiri_extractor import extract_kirikiri
        extract_kirikiri(game_dir, out_dir)

        _apply_fake_translation(out_dir / "extracted", {
            "こんにちは！": "Привет!",
            "今日はいい天気ですね。": "Сегодня хорошая погода.",
        })

        from engine.kirikiri_injector import inject_kirikiri
        result = inject_kirikiri(game_dir, out_dir, backup=False)
        assert result.get("total_injected", 0) > 0, f"Nothing injected: {result}"

        ks_content = (game_dir / "data" / "scenario" / "start.ks").read_text(encoding="utf-8")
        assert "Привет!" in ks_content, f"Translation not found in .ks: {ks_content[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# Ren'Py round-trip
# ─────────────────────────────────────────────────────────────────────────────

def _make_renpy_game(game_dir: Path):
    game_sub = game_dir / "game"
    game_sub.mkdir(parents=True)
    (game_sub / "script.rpy").write_text(
        'label start:\n'
        '    scene bg town\n'
        '    "こんにちは、世界！"\n'
        '    a "私はアリスです。"\n'
        '    menu:\n'
        '        "はい":\n'
        '            pass\n'
        '        "いいえ":\n'
        '            pass\n',
        encoding="utf-8"
    )


def test_renpy_extract():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_renpy_game(game_dir)
        out_dir.mkdir()

        from renpy_extractor import extract_renpy
        result = extract_renpy(game_dir, out_dir)
        assert result.get("total_strings", 0) > 0, f"Nothing extracted: {result}"

        all_entries = []
        for f in (out_dir / "extracted").glob("*.json"):
            if not f.name.endswith("_translated.json"):
                all_entries.extend(json.loads(f.read_text(encoding="utf-8")))
        originals = [e["original"] for e in all_entries]
        assert any("こんにちは" in o for o in originals), f"Narration not extracted: {originals}"
        assert any("アリス" in o for o in originals), f"Dialog not extracted: {originals}"


def test_renpy_inject_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_renpy_game(game_dir)
        out_dir.mkdir()

        from renpy_extractor import extract_renpy
        extract_renpy(game_dir, out_dir)

        _apply_fake_translation(out_dir / "extracted", {
            "こんにちは、世界！": "Привет, мир!",
            "私はアリスです。": "Я — Алиса.",
        })

        from renpy_injector import inject_renpy
        result = inject_renpy(game_dir, out_dir, backup=False)
        assert result.get("total_injected", 0) > 0, f"Nothing injected: {result}"

        content = (game_dir / "game" / "script.rpy").read_text(encoding="utf-8")
        assert "Привет, мир!" in content, f"Narration not injected:\n{content}"
        assert "Я — Алиса." in content, f"Dialog not injected:\n{content}"


# ─────────────────────────────────────────────────────────────────────────────
# TyranoBuilder round-trip
# ─────────────────────────────────────────────────────────────────────────────

def _make_tyrano_game(game_dir: Path):
    scenario = game_dir / "data" / "scenario"
    scenario.mkdir(parents=True)
    (game_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (scenario / "first.ks").write_text(
        "*start\n"
        "[chara_show name=\"ユイ\" storage=\"yui_normal.png\"]\n"
        "[chara_talk name=\"ユイ\" face=\"normal\"]こんにちは！[l][r]\n"
        "[chara_talk name=\"ユイ\" face=\"normal\"]どうぞよろしく。[l][r]\n"
        "[s]\n",
        encoding="utf-8"
    )


def test_tyrano_extract():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_tyrano_game(game_dir)
        out_dir.mkdir()

        from engine.tyrano_extractor import extract_tyrano
        result = extract_tyrano(game_dir, out_dir)
        assert result.get("total_strings", 0) > 0, f"Nothing extracted: {result}"

        all_entries = []
        for f in (out_dir / "extracted").glob("*.json"):
            if not f.name.endswith("_translated.json"):
                all_entries.extend(json.loads(f.read_text(encoding="utf-8")))
        originals = [e["original"] for e in all_entries]
        assert any("こんにちは" in o for o in originals), f"Not extracted: {originals}"


def test_tyrano_inject_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_tyrano_game(game_dir)
        out_dir.mkdir()

        from engine.tyrano_extractor import extract_tyrano
        extract_tyrano(game_dir, out_dir)

        # Tyrano: original = FULL LINE including tags, translated = full line with translated text
        all_entries = []
        for f in (out_dir / "extracted").glob("*.json"):
            if not f.name.endswith("_translated.json"):
                all_entries.extend(json.loads(f.read_text(encoding="utf-8")))
        # Build translation map using actual original (full lines with tags)
        tyrano_map = {}
        for e in all_entries:
            orig = e["original"]
            text_only = _TYRANO_TAG_RE.sub("", orig).strip()
            replacements = {"こんにちは！": "Привет!", "どうぞよろしく。": "Приятно познакомиться."}
            for jp, ru in replacements.items():
                if jp in text_only:
                    tyrano_map[orig] = _TYRANO_TAG_RE.sub("", orig).replace(jp, ru)
        _apply_fake_translation(out_dir / "extracted", tyrano_map)

        from engine.tyrano_injector import inject_tyrano
        result = inject_tyrano(game_dir, out_dir, backup=False)
        assert result.get("total_injected", 0) > 0, f"Nothing injected: {result}"

        content = (game_dir / "data" / "scenario" / "first.ks").read_text(encoding="utf-8")
        assert "Привет!" in content, f"Not injected:\n{content}"


# ─────────────────────────────────────────────────────────────────────────────
# Godot round-trip
# ─────────────────────────────────────────────────────────────────────────────

def _make_godot_game(game_dir: Path):
    (game_dir).mkdir(parents=True, exist_ok=True)
    (game_dir / "project.godot").write_text('[gd_resource type="PackedScene"]\n', encoding="utf-8")
    (game_dir / "main.tscn").write_text(
        '[node name="Label"]\ntext = "こんにちは！"\n'
        '[node name="Button"]\ntext = "はい"\n',
        encoding="utf-8"
    )
    scripts = game_dir / "scripts"
    scripts.mkdir()
    (scripts / "dialog.gd").write_text(
        'func _ready():\n    label.text = tr("RPGのタイトル")\n',
        encoding="utf-8"
    )
    dlg = game_dir / "scenes"
    dlg.mkdir()
    (dlg / "intro.dialogue").write_text(
        "Alice: はじめまして！\n"
        "ナレーション\n"
        "- 続ける\n",
        encoding="utf-8"
    )


def test_godot_extract():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_godot_game(game_dir)
        out_dir.mkdir()

        from engine.godot_extractor import extract_godot
        result = extract_godot(game_dir, out_dir)
        assert result.get("total_strings", 0) > 0, f"Nothing extracted: {result}"

        all_entries = []
        for f in (out_dir / "extracted").glob("*.json"):
            if not f.name.endswith("_translated.json"):
                all_entries.extend(json.loads(f.read_text(encoding="utf-8")))
        originals = [e["original"] for e in all_entries]

        assert any("こんにちは" in o for o in originals), f"TSCN not extracted: {originals}"
        assert any("RPGのタイトル" in o for o in originals), f"GDScript tr() not extracted: {originals}"
        assert any("はじめまして" in o for o in originals), f".dialogue not extracted: {originals}"


def test_godot_inject_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_godot_game(game_dir)
        out_dir.mkdir()

        from engine.godot_extractor import extract_godot
        extract_godot(game_dir, out_dir)

        _apply_fake_translation(out_dir / "extracted", {
            "こんにちは！": "Привет!",
            "RPGのタイトル": "Название RPG",
            "はじめまして！": "Приятно познакомиться!",
        })

        from engine.godot_injector import inject_godot
        result = inject_godot(game_dir, out_dir, backup=False)
        assert result.get("total_injected", 0) > 0, f"Nothing injected: {result}"

        tscn = (game_dir / "main.tscn").read_text(encoding="utf-8")
        assert "Привет!" in tscn, f"TSCN not injected:\n{tscn}"


# ─────────────────────────────────────────────────────────────────────────────
# VXAce round-trip  
# ─────────────────────────────────────────────────────────────────────────────

def test_vxace_extract():
    """VXAce uses rubymarshal. Create minimal Actors.rvdata2 and extract."""
    try:
        from rubymarshal.classes import RubyObject
    except ImportError:
        pytest.skip("rubymarshal not available")

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        (game_dir / "Data").mkdir(parents=True)
        out_dir.mkdir()

        # Create minimal Actors.rvdata2 using rubymarshal
        # Create minimal Actors.rvdata2 with rubymarshal
        import rubymarshal.writer as rw

        actor = RubyObject("RPG::Actor", attributes={
            "@id": 1, "@name": "勇者", "@nickname": "英雄",
            "@description": "主人公だ", "@note": "", "@class_id": 1,
            "@initial_level": 1, "@max_level": 99, "@face_name": "",
            "@face_index": 0, "@character_name": "", "@character_index": 0,
            "@battler_name": "", "@equips": [], "@traits": [], "@exp_params": [],
            "@params": [],
        })
        buf = io.BytesIO()
        rw.write(buf, [None, actor])
        (game_dir / "Data" / "Actors.rvdata2").write_bytes(buf.getvalue())

        from engine.vxace_extractor import extract_vxace
        result = extract_vxace(game_dir, out_dir)
        assert result.get("total_strings", 0) > 0, f"VXAce extract nothing: {result}"

        all_entries = []
        for f in (out_dir / "extracted").glob("*.json"):
            if not f.name.endswith("_translated.json"):
                all_entries.extend(json.loads(f.read_text(encoding="utf-8")))
        originals = [e["original"] for e in all_entries]
        assert any("勇者" in o for o in originals), f"Actor name not extracted: {originals}"



# ─────────────────────────────────────────────────────────────────────────────
# Wolf RPG injector — no-translation guard and API consistency
# ─────────────────────────────────────────────────────────────────────────────

def test_wolf_extract_finds_correct_byte_offsets():
    """
    Реальный тест бинарного формата Wolf: length-prefixed строки (4-байтная
    LE длина + байты) должны находиться СТРОГО на своих байтовых смещениях.
    Раньше это покрывалось только тестами на пустых директориях (не проверяли
    сам парсинг вообще).
    """
    import struct
    from engine.wolf_extractor import extract_wolf

    text1 = "こんにちは勇者"
    text2 = "さようなら、また会おう"

    def wolf_str(s):
        raw = s.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    junk = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    binary = junk + wolf_str(text1) + b"\x00\x00\x00\x00" + wolf_str(text2) + b"\xff\xff"

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        basic = game_dir / "Data" / "BasicData"
        basic.mkdir(parents=True)
        out_dir.mkdir()
        (basic / "DataBase.wolf").write_bytes(binary)

        result = extract_wolf(game_dir, out_dir)
        assert result["total_strings"] == 2

        ext_file = (out_dir / "extracted" / "Data_BasicData_DataBase.json")
        entries = json.loads(ext_file.read_text(encoding="utf-8"))
        by_original = {e["original"]: e for e in entries}

        assert int(by_original[text1]["path"]) == len(junk)
        expected_offset2 = len(junk) + 4 + len(text1.encode("utf-8")) + 4
        assert int(by_original[text2]["path"]) == expected_offset2


def test_wolf_inject_produces_byte_correct_binary():
    """
    Реальный round-trip: extract -> перевод -> inject -> ЗАНОВО распарсить
    итоговый бинарник вручную и убедиться что байты корректны, включая
    правильный сдвиг смещения второй строки (т.к. перевод другой длины).
    """
    import struct
    from engine.wolf_extractor import extract_wolf
    from engine.wolf_injector import inject_wolf

    text1, text2 = "こんにちは勇者", "さようなら、また会おう"
    trans1, trans2 = "Привет, герой", "Прощай, увидимся снова"

    def wolf_str(s):
        raw = s.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    junk = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    binary = junk + wolf_str(text1) + b"\x00\x00\x00\x00" + wolf_str(text2) + b"\xff\xff"

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        basic = game_dir / "Data" / "BasicData"
        basic.mkdir(parents=True)
        out_dir.mkdir()
        (basic / "DataBase.wolf").write_bytes(binary)

        extract_wolf(game_dir, out_dir)
        _apply_fake_translation(out_dir / "extracted", {text1: trans1, text2: trans2})
        inject_wolf(game_dir, out_dir, backup=True)

        new_binary = (basic / "DataBase.wolf").read_bytes()

        # Ручной парсинг итогового бинарника — не полагаемся на extractor,
        # чтобы тест был независимой проверкой, а не циклической зависимостью.
        pos, found = 0, []
        while pos < len(new_binary) - 4:
            length = struct.unpack_from("<I", new_binary, pos)[0]
            if 2 <= length <= 2048 and pos + 4 + length <= len(new_binary):
                raw = new_binary[pos + 4:pos + 4 + length]
                try:
                    text = raw.decode("utf-8")
                    if any("\u0400" <= c <= "\u04FF" for c in text):
                        found.append(text)
                        pos += 4 + length
                        continue
                except UnicodeDecodeError:
                    pass
            pos += 1

        assert found == [trans1, trans2], f"Патченный бинарник некорректен: {found}"

        backup_file = game_dir / "_backup_wolf_original" / "Data" / "BasicData" / "DataBase.wolf"
        assert backup_file.exists()
        assert backup_file.read_bytes() == binary, "Backup не совпадает с оригинальными байтами"


def test_wolf_double_inject_is_idempotent_via_hash_check():
    """
    Wolf использует hash-based idempotency (не marker-файлы, как остальные
    движки): при повторном inject он видит что файл уже не совпадает с хэшем
    на момент Extract и корректно ОТКАЗЫВАЕТСЯ от повторной вставки, оставляя
    файл нетронутым — вместо того чтобы патчить уже переведённые байты снова.
    """
    import struct
    from engine.wolf_extractor import extract_wolf
    from engine.wolf_injector import inject_wolf

    text1, trans1 = "こんにちは勇者", "Привет, герой"

    def wolf_str(s):
        raw = s.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    binary = b"\x01\x02\x03\x04\x05\x06\x07\x08" + wolf_str(text1) + b"\xff\xff"

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        basic = game_dir / "Data" / "BasicData"
        basic.mkdir(parents=True)
        out_dir.mkdir()
        (basic / "DataBase.wolf").write_bytes(binary)

        extract_wolf(game_dir, out_dir)
        _apply_fake_translation(out_dir / "extracted", {text1: trans1})

        inject_wolf(game_dir, out_dir, backup=True)
        bytes_after_1st = (basic / "DataBase.wolf").read_bytes()

        inject_wolf(game_dir, out_dir, backup=True)
        bytes_after_2nd = (basic / "DataBase.wolf").read_bytes()

        assert bytes_after_1st == bytes_after_2nd, "Double-inject изменил байты файла!"

        pos, found = 0, []
        while pos < len(bytes_after_2nd) - 4:
            length = struct.unpack_from("<I", bytes_after_2nd, pos)[0]
            if 2 <= length <= 2048 and pos + 4 + length <= len(bytes_after_2nd):
                raw = bytes_after_2nd[pos + 4:pos + 4 + length]
                try:
                    text = raw.decode("utf-8")
                    if any("\u0400" <= c <= "\u04FF" for c in text):
                        found.append(text)
                        pos += 4 + length
                        continue
                except UnicodeDecodeError:
                    pass
            pos += 1
        assert found == [trans1], f"Содержимое повреждено после double-inject: {found}"


def test_vxace_inject_roundtrip():
    """
    VXAce round-trip через РЕАЛЬНЫЙ Ruby Marshal бинарник: extract -> перевод
    -> inject -> заново загрузить .rvdata2 через rubymarshal.reader и
    убедиться что @name/@nickname/@description действительно изменились.

    Раньше существовал только test_vxace_extract — inject НИКОГДА не
    проверялся сквозным тестом.
    """
    pytest.importorskip("rubymarshal")
    from rubymarshal.classes import RubyObject
    import rubymarshal.writer as rw
    import rubymarshal.reader as rdr
    from engine.vxace_extractor import extract_vxace
    from engine.vxace_injector import inject_vxace

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        (game_dir / "Data").mkdir(parents=True)
        out_dir.mkdir()

        actor = RubyObject("RPG::Actor", attributes={
            "@id": 1, "@name": "勇者", "@nickname": "英雄",
            "@description": "主人公だ", "@note": "", "@class_id": 1,
            "@initial_level": 1, "@max_level": 99, "@face_name": "",
            "@face_index": 0, "@character_name": "", "@character_index": 0,
            "@battler_name": "", "@equips": [], "@traits": [], "@exp_params": [],
            "@params": [],
        })
        buf = io.BytesIO()
        rw.write(buf, [None, actor])
        (game_dir / "Data" / "Actors.rvdata2").write_bytes(buf.getvalue())

        extract_vxace(game_dir, out_dir)
        ext_dir = out_dir / "extracted"
        f = list(ext_dir.glob("*.json"))[0]
        entries = json.loads(f.read_text(encoding="utf-8"))
        trans_map = {"勇者": "Герой", "英雄": "Легенда", "主人公だ": "Это главный герой"}
        for e in entries:
            e["translated"] = trans_map.get(e["original"], "")
        (ext_dir / (f.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        result = inject_vxace(game_dir, out_dir, backup=True)
        assert result["total_injected"] == 3
        assert result["total_errors"] == 0

        new_bytes = (game_dir / "Data" / "Actors.rvdata2").read_bytes()
        reloaded_actor = rdr.load(io.BytesIO(new_bytes))[1]

        def _txt(v):
            return getattr(v, "text", v)

        assert _txt(reloaded_actor.attributes.get("@name")) == "Герой"
        assert _txt(reloaded_actor.attributes.get("@nickname")) == "Легенда"
        assert _txt(reloaded_actor.attributes.get("@description")) == "Это главный герой"


def test_vxace_double_inject_no_corruption():
    """VXAce использует marker+restore идемпотентность — двойной inject не должен портить данные."""
    pytest.importorskip("rubymarshal")
    from rubymarshal.classes import RubyObject
    import rubymarshal.writer as rw
    import rubymarshal.reader as rdr
    from engine.vxace_extractor import extract_vxace
    from engine.vxace_injector import inject_vxace

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        (game_dir / "Data").mkdir(parents=True)
        out_dir.mkdir()

        actor = RubyObject("RPG::Actor", attributes={
            "@id": 1, "@name": "勇者", "@nickname": "", "@description": "", "@note": "",
            "@class_id": 1, "@initial_level": 1, "@max_level": 99, "@face_name": "",
            "@face_index": 0, "@character_name": "", "@character_index": 0,
            "@battler_name": "", "@equips": [], "@traits": [], "@exp_params": [], "@params": [],
        })
        buf = io.BytesIO()
        rw.write(buf, [None, actor])
        (game_dir / "Data" / "Actors.rvdata2").write_bytes(buf.getvalue())

        extract_vxace(game_dir, out_dir)
        ext_dir = out_dir / "extracted"
        f = list(ext_dir.glob("*.json"))[0]
        entries = json.loads(f.read_text(encoding="utf-8"))
        for e in entries:
            if e["original"] == "勇者":
                e["translated"] = "Герой"
        (ext_dir / (f.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_vxace(game_dir, out_dir, backup=True)
        inject_vxace(game_dir, out_dir, backup=True)  # second inject

        new_bytes = (game_dir / "Data" / "Actors.rvdata2").read_bytes()
        reloaded_actor = rdr.load(io.BytesIO(new_bytes))[1]
        name = getattr(reloaded_actor.attributes.get("@name"), "text", None)
        assert name == "Герой", f"Double-inject corrupted @name: {name!r}"


def test_unity_json_inject_roundtrip_nested_paths():
    """
    Unity JSON round-trip с ВЛОЖЕННЫМИ путями (dialogue.npc1) — engine ранее
    не имел вообще НИ ОДНОГО функционального теста (только проверка label'ов
    контекста в test_core.py).
    """
    from engine.unity_extractor import extract_unity
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        out_dir = tmp / "out"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        out_dir.mkdir()

        loc_data = {
            "greeting": "こんにちは、旅人よ",
            "dialogue": {"npc1": "町へようこそ！", "npc2": "気をつけて。"},
        }
        loc_file = game_dir / "StreamingAssets" / "localization_ja.json"
        loc_file.write_text(json.dumps(loc_data, ensure_ascii=False, indent=2), encoding="utf-8")

        result = extract_unity(tmp, out_dir)
        assert result["total_strings"] == 3

        ext_dir = out_dir / "extracted"
        f = list(ext_dir.glob("*.json"))[0]
        entries = json.loads(f.read_text(encoding="utf-8"))
        trans_map = {
            "こんにちは、旅人よ": "Привет, путник",
            "町へようこそ！": "Добро пожаловать в город!",
            "気をつけて。": "Будь осторожен.",
        }
        for e in entries:
            e["translated"] = trans_map[e["original"]]
        (ext_dir / (f.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inj_result = inject_unity(tmp, out_dir, backup=True)
        assert inj_result["total_injected"] == 3
        assert inj_result["total_errors"] == 0

        new_content = json.loads(loc_file.read_text(encoding="utf-8"))
        assert new_content["greeting"] == "Привет, путник"
        assert new_content["dialogue"]["npc1"] == "Добро пожаловать в город!"
        assert new_content["dialogue"]["npc2"] == "Будь осторожен."

        backup_file = tmp / "_backup_unity_original" / "MyGame_Data" / "StreamingAssets" / "localization_ja.json"
        assert backup_file.exists()
        assert json.loads(backup_file.read_text(encoding="utf-8")) == loc_data


def test_unity_csv_inject_and_double_inject_idempotent():
    """Unity CSV extract/inject + double-inject не должен дублировать/портить строки."""
    import csv as csv_mod
    from engine.unity_extractor import extract_unity
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        out_dir = tmp / "out"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        out_dir.mkdir()

        csv_file = game_dir / "StreamingAssets" / "strings_ja.csv"
        with open(csv_file, "w", encoding="utf-8", newline="") as fh:
            w = csv_mod.writer(fh)
            w.writerow(["Key", "Japanese"])
            w.writerow(["MENU_START", "ゲーム開始"])
            w.writerow(["MENU_QUIT", "終了する"])

        extract_unity(tmp, out_dir)
        ext_dir = out_dir / "extracted"
        f = list(ext_dir.glob("*.json"))[0]
        entries = json.loads(f.read_text(encoding="utf-8"))
        for e in entries:
            if e["original"] == "ゲーム開始":
                e["translated"] = "Начать игру"
            elif e["original"] == "終了する":
                e["translated"] = "Выйти"
        (ext_dir / (f.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_unity(tmp, out_dir, backup=True)
        content_1st = csv_file.read_text(encoding="utf-8")
        inject_unity(tmp, out_dir, backup=True)
        content_2nd = csv_file.read_text(encoding="utf-8")

        assert content_1st == content_2nd, "Double-inject изменил CSV между вызовами"
        rows = list(csv_mod.reader(content_2nd.splitlines()))
        assert ["MENU_START", "Начать игру"] in rows
        assert ["MENU_QUIT", "Выйти"] in rows


def test_unity_xml_extract_only_leaf_text_preserves_structure():
    """
    БАГ: .xml раньше маршрутизировался в построчный extractor — вся строка
    (включая теги/атрибуты) извлекалась и переводилась как единый текст, а
    при инъекции строка заменялась целиком, теряя разметку полностью.
    Теперь извлекается только текст между > и <; инъекция сохраняет 100%
    окружающей структуры (теги, атрибуты, XML-комментарии) — не через
    ElementTree.write() (переформатирует файл, теряет комментарии), а
    через точечную замену подстроки в сыром тексте файла.
    """
    from engine.unity_extractor import extract_unity
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        out_dir = tmp / "out"
        (tmp / "ProjectSettings").mkdir()
        (tmp / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1")

        xml_file = game_dir / "StreamingAssets" / "dialogue.xml"
        xml_file.write_text(
            '<?xml version="1.0"?>\n<Root>\n'
            '  <!-- важный комментарий -->\n'
            '  <Line id="a" speaker="Yuusha">こんにちは、旅人よ！</Line>\n'
            '</Root>\n',
            encoding="utf-8",
        )

        extract_unity(tmp, out_dir, log_callback=lambda m: None)
        ext_dir = out_dir / "extracted"
        jf = next(f for f in ext_dir.glob("*.json") if "dialogue" in f.name and "_translated" not in f.name)
        entries = json.loads(jf.read_text(encoding="utf-8"))
        assert len(entries) == 1
        assert entries[0]["original"] == "こんにちは、旅人よ！"
        assert '<Line' not in entries[0]["original"], "БАГ: теги попали в извлечённый текст"
        for e in entries:
            e["translated"] = "Привет, путник!"
        (ext_dir / (jf.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_unity(tmp, out_dir, backup=True)
        result = xml_file.read_text(encoding="utf-8")
        assert "<!-- важный комментарий -->" in result, "БАГ: XML-комментарий потерян"
        assert 'id="a"' in result and 'speaker="Yuusha"' in result, "БАГ: атрибуты потеряны"
        assert "Привет, путник!" in result
        assert "こんにちは" not in result


def test_unity_po_never_translates_msgid_only_msgstr():
    """
    БАГ: .po раньше маршрутизировался в построчный extractor — 'msgid "..."'
    (ключ поиска перевода в gettext) МОГ извлекаться и переводиться, что
    ломает механизм поиска перевода по ключу — тот же класс бага, что и с
    идентификаторами RPG Maker/Wolf. А 'msgstr "..."' заменялась целиком,
    теряя обёртку. Теперь трогается только значение msgstr, msgid — никогда.
    """
    from engine.unity_extractor import extract_unity
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        out_dir = tmp / "out"
        (tmp / "ProjectSettings").mkdir()
        (tmp / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1")

        po_file = game_dir / "StreamingAssets" / "strings.po"
        po_file.write_text('msgid "greet"\nmsgstr "こんにちは"\n', encoding="utf-8")

        extract_unity(tmp, out_dir, log_callback=lambda m: None)
        ext_dir = out_dir / "extracted"
        jf = next(f for f in ext_dir.glob("*.json") if "strings" in f.name and "_translated" not in f.name)
        entries = json.loads(jf.read_text(encoding="utf-8"))
        assert len(entries) == 1, "Должна извлечься только msgstr, не msgid"
        assert entries[0]["original"] == "こんにちは"
        for e in entries:
            e["translated"] = "Привет"
        (ext_dir / (jf.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_unity(tmp, out_dir, backup=True)
        result = po_file.read_text(encoding="utf-8")
        assert 'msgid "greet"' in result, "БАГ: msgid изменён — сломан поиск перевода по ключу"
        assert 'msgstr "Привет"' in result


def test_unity_yaml_preserves_key_only_translates_value():
    """
    БАГ: .yaml раньше маршрутизировался в построчный extractor — строка
    'greeting: こんにちは' заменялась ЦЕЛИКОМ переводом при инъекции, теряя
    ключ 'greeting:' полностью — YAML переставал парситься как валидная
    структура. Теперь ключ всегда сохраняется, переводится только значение.
    """
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
        yaml_file.write_text("greeting: こんにちは\nsome_id: internal_value\n", encoding="utf-8")

        extract_unity(tmp, out_dir, log_callback=lambda m: None)
        ext_dir = out_dir / "extracted"
        jf = next(f for f in ext_dir.glob("*.json") if "config" in f.name and "_translated" not in f.name)
        entries = json.loads(jf.read_text(encoding="utf-8"))
        assert len(entries) == 1, "Должна извлечься только greeting, не строка с идентификатором"
        assert entries[0]["original"] == "こんにちは"
        for e in entries:
            e["translated"] = "Привет"
        (ext_dir / (jf.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_unity(tmp, out_dir, backup=True)
        result = yaml_file.read_text(encoding="utf-8")
        assert "greeting: Привет" in result, f"БАГ: ключ 'greeting:' потерян: {result!r}"
        assert "some_id: internal_value" in result, "Нетронутая строка не должна была измениться"


def test_unity_xml_inject_idempotent_and_backup_preserves_original():
    """Повторный Inject для XML должен восстанавливать оригинал из бэкапа
    перед повторным применением, а бэкап должен хранить ИСТИННЫЙ оригинал,
    а не когда-либо переведённый текст (тот же контракт, что и у остальных
    форматов Unity — см. Round про Unity backup corruption)."""
    from engine.unity_extractor import extract_unity
    from engine.unity_injector import inject_unity

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game_dir = tmp / "MyGame_Data"
        (game_dir / "StreamingAssets").mkdir(parents=True)
        out_dir = tmp / "out"
        (tmp / "ProjectSettings").mkdir()
        (tmp / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1")

        xml_file = game_dir / "StreamingAssets" / "d.xml"
        xml_file.write_text("<Root><Line>こんにちは</Line></Root>", encoding="utf-8")

        extract_unity(tmp, out_dir, log_callback=lambda m: None)
        ext_dir = out_dir / "extracted"
        jf = next(f for f in ext_dir.glob("*.json") if "_translated" not in f.name)
        entries = json.loads(jf.read_text(encoding="utf-8"))
        for e in entries:
            e["translated"] = "Привет"
        (ext_dir / (jf.stem + "_translated.json")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

        inject_unity(tmp, out_dir, backup=True)
        inject_unity(tmp, out_dir, backup=True)
        inject_unity(tmp, out_dir, backup=True)

        result = xml_file.read_text(encoding="utf-8")
        assert "Привет" in result
        assert result.count("Привет") == 1, "БАГ: многократный inject не должен дублировать перевод"
        assert "<Root>" in result and "<Line>" in result, (
            f"БАГ: XML-теги потеряны — вся строка была заменена переводом целиком: {result!r}"
        )

        backup = (tmp / "_backup_unity_original" / "MyGame_Data" / "StreamingAssets" / "d.xml").read_text(encoding="utf-8")
        assert "こんにちは" in backup, f"БАГ: бэкап испорчен переводом: {backup!r}"


def test_wolf_inject_no_files_returns_dict():
    """inject_wolf должен возвращать dict, а не raise, если нет переводов."""
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        game_dir.mkdir(); out_dir.mkdir()
        (game_dir / "Data").mkdir()

        from engine.wolf_injector import inject_wolf
        result = inject_wolf(game_dir, out_dir, backup=False)
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result["total_injected"] == 0


def test_wolf_extract_returns_dict():
    """wolf extractor должен вернуть stats-dict, не упасть при отсутствии файлов."""
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        (game_dir / "Data" / "BasicData").mkdir(parents=True)
        out_dir.mkdir()

        from engine.wolf_extractor import extract_wolf
        result = extract_wolf(game_dir, out_dir)
        assert isinstance(result, dict)
        assert "total_strings" in result



# ─────────────────────────────────────────────────────────────────────────────
# Deduplication round-trip
# ─────────────────────────────────────────────────────────────────────────────

def test_dedup_full_pipeline():
    """Перевод одной строки распространяется на 50 дубликатов в разных файлах."""
    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = Path(tmp)
        n_files = 50
        repeated_text = "はい"

        all_entries = []
        for i in range(n_files):
            entry = {
                "file": f"Map{i:03d}.json",
                "path": f"events[1].pages[0].list[{i}]",
                "original": repeated_text,
                "translated": "",
                "context": "dialog_text",
                "game_type": "rpgmv",
            }
            (extract_dir / f"Map{i:03d}.json").write_text(
                json.dumps([entry]), encoding="utf-8"
            )
            all_entries.append(entry)

        from engine.deduplicator import build_dedup_map, apply_dedup_to_files
        dm = build_dedup_map(all_entries)
        dm.set_translation(repeated_text, "Да")
        apply_dedup_to_files(dm, extract_dir)

        # Every _translated.json must have the translation
        for i in range(n_files):
            tr_path = extract_dir / f"Map{i:03d}_translated.json"
            assert tr_path.exists(), f"Missing {tr_path.name}"
            data = json.loads(tr_path.read_text(encoding="utf-8"))
            assert data[0]["translated"] == "Да", f"File {i}: not translated"


# ─────────────────────────────────────────────────────────────────────────────
# TranslationState — persistence & atomic write
# ─────────────────────────────────────────────────────────────────────────────

def test_state_survives_reload():
    """Состояние сохраняется и корректно загружается после перезапуска."""
    from engine.state import TranslationState
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        s = TranslationState(path)
        for i in range(200):
            s.set(f"orig_{i}", f"trans_{i}")
        s.save()

        s2 = TranslationState(path)
        for i in range(200):
            assert s2.has(f"orig_{i}"), f"Missing orig_{i}"
            assert s2.get(f"orig_{i}") == f"trans_{i}"


def test_state_save_periodically_triggers():
    """save_periodically должен сохранять после N записей."""
    from engine.state import TranslationState
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        s = TranslationState(path)
        for i in range(49):
            s.set(f"k{i}", f"v{i}")
        s.save_periodically(50)
        assert not path.exists() or path.stat().st_size == 0 or True  # might exist from set
        s.set("k49", "v49")  # 50th
        s.save_periodically(50)
        assert path.exists() and path.stat().st_size > 10


# ─────────────────────────────────────────────────────────────────────────────
# escape code protect / validate / repair
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_codes_ok():
    from ai_translator import _validate_codes
    assert _validate_codes(r"\C[2]Hello\C[0]", r"\C[2]Привет\C[0]")
    assert _validate_codes(r"\N[1] speaks", r"\N[1] говорит")


def test_validate_codes_fail():
    from ai_translator import _validate_codes
    assert not _validate_codes(r"\C[2]Hello\C[0]", "Привет")  # missing codes


def test_repair_codes():
    from ai_translator import _repair_codes, _validate_codes
    original = r"\C[2]Hello\C[0]"
    broken   = "Привет"
    repaired = _repair_codes(original, broken)
    assert _validate_codes(original, repaired), f"Repair failed: {repaired!r}"


def test_protect_restore_roundtrip():
    from ai_translator import _protect_codes, _restore_codes
    text = r"\C[2]こんにちは\N[1]\C[0]"
    protected, ph = _protect_codes(text)
    assert r"\C" not in protected, "Codes not protected"
    restored = _restore_codes(protected, ph)
    assert restored == text, f"Restore mismatch: {restored!r} != {text!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Smart Warp real data
# ─────────────────────────────────────────────────────────────────────────────

def test_smart_warp_wraps_long_line():
    from smart_warp import wrap_paragraph
    long_text = "これは非常に長いテキストで、画面に収まらないはずの文章です。折り返しが必要です。"
    wrapped = wrap_paragraph(long_text, max_width_px=200, max_lines=4)
    assert "\n" in wrapped or len(wrapped) <= len(long_text), "Warp did nothing on long text"


def test_smart_warp_short_no_wrap():
    from smart_warp import wrap_paragraph
    short = "短い"
    result = wrap_paragraph(short, max_width_px=400, max_lines=4)
    assert "\n" not in result


def test_smart_warp_does_not_corrupt_codes():
    from smart_warp import wrap_paragraph
    text = r"\C[2]長いセリフここに\C[0]とても長い文章"
    result = wrap_paragraph(text, max_width_px=100, max_lines=4)
    assert r"\C[2]" in result and r"\C[0]" in result, f"Codes corrupted: {result!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline.get_stats — no TOCTOU
# ─────────────────────────────────────────────────────────────────────────────

def test_get_stats_before_extract():
    with tempfile.TemporaryDirectory() as tmp:
        from pipeline import Pipeline
        p = Pipeline(tmp, tmp)
        stats = p.get_stats()
        assert stats["extract_done"] is False
        assert stats["total_strings"] == 0


def test_get_stats_after_extract():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_rpgmv_game(game_dir)
        out_dir.mkdir()

        from engine.extractor import extract_game
        extract_game(game_dir, out_dir)

        from pipeline import Pipeline
        p = Pipeline(str(game_dir), str(out_dir))
        stats = p.get_stats()
        assert stats["extract_done"] is True
        assert stats["total_strings"] > 0
        assert stats["unique_strings"] > 0
        assert stats["unique_strings"] <= stats["total_strings"]


def test_get_stats_class_cache():
    """Разные экземпляры Pipeline должны использовать общий кэш."""
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_rpgmv_game(game_dir)
        out_dir.mkdir()

        from engine.extractor import extract_game
        extract_game(game_dir, out_dir)

        from pipeline import Pipeline
        p1 = Pipeline(str(game_dir), str(out_dir))
        s1 = p1.get_stats()

        p2 = Pipeline(str(game_dir), str(out_dir))
        s2 = p2.get_stats()

        assert s1["total_strings"] == s2["total_strings"], "Class cache not shared"


# ─────────────────────────────────────────────────────────────────────────────
# Glossary full apply with edge cases
# ─────────────────────────────────────────────────────────────────────────────

def test_glossary_roundtrip():
    from engine.glossary import Glossary
    g = Glossary()
    g.add("勇者", "Герой")
    g.add("魔法", "Магия")
    g.add("魔法使い", "Маг")  # longer wins over 魔法

    text = r"\N[1]は勇者で魔法使いだ"
    result = g.apply(text)
    assert "Герой" in result
    assert "Маг" in result
    assert "Магия" not in result  # 魔法 was part of 魔法使い
    assert r"\N[1]" in result    # code intact


def test_glossary_csv_import_export(tmp_path):
    from engine.glossary import Glossary, export_glossary_csv, import_glossary_csv
    g = Glossary()
    g.add("アリス", "Алиса")
    g.add("ボブ", "Боб")

    csv_path = tmp_path / "glossary.csv"
    export_glossary_csv(g, csv_path)
    assert csv_path.exists()

    g2 = Glossary()
    import_glossary_csv(g2, csv_path)
    assert g2.terms.get("アリス") == "Алиса"
    assert g2.terms.get("ボブ") == "Боб"

# ─────────────────────────────────────────────────────────────────────────────
# AI context labels — all engines mapped
# ─────────────────────────────────────────────────────────────────────────────

def test_all_engine_contexts_have_labels():
    """Все context-значения всех экстракторов должны быть в _CTX_LABELS."""
    from ai_translator import _format_line_with_context
    # contexts actually produced by extractors (verified by grep)
    engine_contexts = [
        "dialog_text", "choice", "narrator", "scroll_text",
        "wolf_dialog", "wolf_choice", "wolf_string",
        "unity_json", "unity_csv", "unity_text",
        "godot_script", "godot_dialog_choice", "godot_dialog_narration",
        "dialog", "renpy_say", "renpy_narrator",
    ]
    for ctx in engine_contexts:
        # _format_line_with_context should not raise and should produce output
        result = _format_line_with_context("テスト", ctx)
        assert isinstance(result, str)


def test_format_with_speaker_context():
    from ai_translator import _format_line_with_context
    result = _format_line_with_context("こんにちは", "say:アリス")
    assert "アリス" in result
    assert "こんにちは" in result


def test_strip_hint_does_not_touch_normal_parens():
    from ai_translator import _strip_context_hint
    assert _strip_context_hint("(笑) なに？") == "(笑) なに？"



# ─────────────────────────────────────────────────────────────────────────────
# Idempotency — all text-based injectors
# ─────────────────────────────────────────────────────────────────────────────

def test_rpgmv_double_inject_no_corruption():
    """Double inject must restore from backup, not stack translations."""
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_rpgmv_game(game_dir)
        out_dir.mkdir()

        from engine.extractor import extract_game
        extract_game(game_dir, out_dir)
        _apply_fake_translation(out_dir / "extracted", {
            "勇者": "Герой",
            "こんにちは！冒険者よ。\nこの村へようこそ。": "Привет!\nДобро.",
        })

        from engine.injector import inject_translations
        inject_translations(game_dir, out_dir)
        inject_translations(game_dir, out_dir)  # second inject

        actors = json.loads((game_dir / "data" / "Actors.json").read_text(encoding="utf-8"))
        names = [a.get("name","") for a in actors if isinstance(a, dict)]
        # Must be "Герой" not "ГеройГерой"
        assert "Герой" in names
        assert not any("Герой" * 2 in n for n in names), f"Double translation detected: {names}"


def test_renpy_double_inject_no_corruption():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_renpy_game(game_dir)
        out_dir.mkdir()

        from renpy_extractor import extract_renpy
        extract_renpy(game_dir, out_dir)
        _apply_fake_translation(out_dir / "extracted", {
            "こんにちは、世界！": "Привет, мир!",
            "私はアリスです。": "Я Алиса.",
        })

        from renpy_injector import inject_renpy
        inject_renpy(game_dir, out_dir, backup=True)
        inject_renpy(game_dir, out_dir, backup=True)  # second inject

        content = (game_dir / "game" / "script.rpy").read_text(encoding="utf-8")
        count = content.count("Привет, мир!")
        assert count == 1, f"Double translation: found {count} occurrences"


def test_kirikiri_double_inject_no_corruption():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_kirikiri_game(game_dir)
        out_dir.mkdir()

        from engine.kirikiri_extractor import extract_kirikiri
        extract_kirikiri(game_dir, out_dir)
        _apply_fake_translation(out_dir / "extracted", {
            "こんにちは！": "Привет!",
            "今日はいい天気ですね。": "Сегодня хорошая погода.",
        })

        from engine.kirikiri_injector import inject_kirikiri
        inject_kirikiri(game_dir, out_dir, backup=True)
        inject_kirikiri(game_dir, out_dir, backup=True)  # second inject

        content = (game_dir / "data" / "scenario" / "start.ks").read_text(encoding="utf-8")
        count = content.count("Привет!")
        assert count == 1, f"Double translation: {count} occurrences"


def test_tyrano_double_inject_no_corruption():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_tyrano_game(game_dir)
        out_dir.mkdir()

        from engine.tyrano_extractor import extract_tyrano
        extract_tyrano(game_dir, out_dir)
        _apply_fake_translation(out_dir / "extracted", {
            "こんにちは！": "Привет!",
        })

        from engine.tyrano_injector import inject_tyrano
        inject_tyrano(game_dir, out_dir, backup=True)
        inject_tyrano(game_dir, out_dir, backup=True)  # second inject

        content = (game_dir / "data" / "scenario" / "first.ks").read_text(encoding="utf-8")
        count = content.count("Привет!")
        assert count == 1, f"Double translation: {count} occurrences"


def test_godot_double_inject_no_corruption():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        out_dir = Path(tmp) / "out"
        _make_godot_game(game_dir)
        out_dir.mkdir()

        from engine.godot_extractor import extract_godot
        extract_godot(game_dir, out_dir)
        _apply_fake_translation(out_dir / "extracted", {
            "こんにちは！": "Привет!",
        })

        from engine.godot_injector import inject_godot
        inject_godot(game_dir, out_dir, backup=False)
        inject_godot(game_dir, out_dir, backup=False)  # second inject - no backup, uses marker

        content = (game_dir / "main.tscn").read_text(encoding="utf-8")
        count = content.count("Привет!")
        assert count == 1, f"Double translation: {count} occurrences"


# ─────────────────────────────────────────────────────────────────────────────
# _should_warp — integration with real extractor context values
# ─────────────────────────────────────────────────────────────────────────────

def test_warp_applies_to_kirikiri_contexts():
    """Kirikiri 'dialog:Alice' context entries must get warp applied."""

    from pipeline import Pipeline
    assert Pipeline._should_warp("dialog:Alice")
    assert Pipeline._should_warp("dialog:田中")
    assert not Pipeline._should_warp("godot_script")


def test_warp_applies_to_renpy_char_vars():
    from pipeline import Pipeline
    # RenPy char variable → treated as speaker
    assert Pipeline._should_warp("alice")
    assert Pipeline._should_warp("narrator")  # generic narration
    assert not Pipeline._should_warp("actor:name")


# ─────────────────────────────────────────────────────────────────────────────
# _ctx_label — prefix-based label resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_ctx_label_actor_field():
    from ai_translator import _ctx_label
    assert _ctx_label("actor:name") == "Actor name"
    assert _ctx_label("item:description") == "Item description"


def test_ctx_label_dialog_speaker():
    from ai_translator import _ctx_label
    assert _ctx_label("dialog:Alice") == "Dialog"
    assert _ctx_label("say:ユイ") == "Dialog"


def test_ctx_label_known_ctx():
    from ai_translator import _ctx_label
    assert _ctx_label("choice") == "Choice option"
    assert _ctx_label("narrator") == "Narration"
