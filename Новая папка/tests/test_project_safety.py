from pathlib import Path
import json
from engine.project_lock import write_manifest, verify_manifest
from engine.translation_profiles import build_instructions


def make_rpg(root: Path, title: str):
    (root / "data").mkdir(parents=True)
    (root / "js").mkdir(parents=True)
    (root / "data" / "System.json").write_text(json.dumps({"gameTitle": title, "versionId": 1}), encoding="utf-8")
    (root / "js" / "rpg_core.js").write_text("RPGMV-CORE-" + title, encoding="utf-8")
    (root / "index.html").write_text(title, encoding="utf-8")


def test_workspace_rejects_different_game(tmp_path):
    a = tmp_path / "A"; b = tmp_path / "B"; out = tmp_path / "out"
    make_rpg(a, "A"); make_rpg(b, "B")
    write_manifest(out, a, "rpgmv")
    ok, _ = verify_manifest(out, b, "rpgmv")
    assert not ok


def test_workspace_allows_same_game_moved(tmp_path):
    a = tmp_path / "A"; out = tmp_path / "out"
    make_rpg(a, "A")
    write_manifest(out, a, "rpgmv")
    moved = tmp_path / "Moved"
    a.rename(moved)
    ok, _ = verify_manifest(out, moved, "rpgmv")
    assert ok


def test_engine_profiles_are_different():
    unity = build_instructions("unity")
    rpg = build_instructions("rpgmv")
    assert "Unity is a container" in unity
    assert "RPG Maker escape codes" in rpg
    assert unity != rpg


def test_pipeline_resolve_game_relative_rejects_path_traversal(tmp_path):
    """
    BUG (внешний аудит, тот же класс, что и manifest source collection):
    Pipeline._resolve_game_relative() делал только .lstrip("/") на rel-пути
    из entry["source_path"]/["file"] — "../evil.json" проходил невредимым.
    Эта функция питает _make_inject_rollback (копирует найденный файл в
    backup) и snapshot_json_targets (читает/хэширует его) — внешний файл мог
    попасть в бэкап Inject или в safety-сравнение.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pipeline import Pipeline

    game_dir = tmp_path / "game"; game_dir.mkdir()
    (tmp_path / "evil.json").write_text("{}", encoding="utf-8")
    (game_dir / "legit.json").write_text("{}", encoding="utf-8")

    assert Pipeline._resolve_game_relative(game_dir, "../evil.json") is None, (
        "БАГ: traversal-путь не был отклонён"
    )
    result = Pipeline._resolve_game_relative(game_dir, "legit.json")
    assert result == game_dir / "legit.json", "легитимный путь должен по-прежнему резолвиться"


def test_manifest_source_collection_rejects_path_traversal(tmp_path):
    """
    BUG (P1-3, внешний аудит, подтверждено эмпирически): rel-путь из
    extract_index.json проходил только rel.replace("\\\\","/").lstrip("/") —
    "../evil.json" не отфильтровывался. c.is_file() резолвит ".." на уровне
    ОС и находит файл ВНЕ game_dir; c.relative_to(game_dir) на неразрешённом
    Path тоже это не ловит (просто вернёт "../evil.json" как "относительный"
    путь). В манифест мог попасть sha256 файла снаружи игровой папки.
    """
    from engine.project_lock import _collect_extracted_sources

    game_dir = tmp_path / "game"; game_dir.mkdir()
    output_dir = tmp_path / "out"; output_dir.mkdir()
    (tmp_path / "evil.json").write_text('{"secret": "data"}', encoding="utf-8")
    (game_dir / "Actors.json").write_text("{}", encoding="utf-8")

    (output_dir / "extract_index.json").write_text(json.dumps({
        "game_type": "rpgmv",
        "sources": {"Actors.json": "Actors.json", "evil.json": "../evil.json"},
    }), encoding="utf-8")

    result = _collect_extracted_sources(output_dir, game_dir)
    rels = [r["relative"] for r in result]
    assert "Actors.json" in rels, "легитимный файл должен по-прежнему собираться"
    assert not any(".." in r for r in rels), (
        f"БАГ: путь с traversal попал в манифест: {rels}"
    )


def test_wolf_project_lock_uses_precise_own_anchor_not_shared_generic_one(tmp_path):
    """
    BUG (найдено систематической проверкой по-движковых списков — тот же
    класс, что rpgmv/rpgmz отсутствие в _ENGINE_TOKEN_PATTERNS): Wolf делил
    anchor-список Project Lock'а с kirikiri/tyrano ("Game.exe", "index.html",
    "data/system.ks"), но ни index.html (веб-специфика Tyrano), ни
    system.ks (KAG-скрипт — Wolf вообще не использует .ks), у настоящей
    Wolf RPG игры не существуют. Реально совпадал только Game.exe — не
    очень отличительный маркер (общий для многих движков), более слабая
    защита identity-проверки конкретно для Wolf. Собственный детектор Wolf
    (game_detector._check_wolf) уже использует куда более точный маркер:
    Data/BasicData/*.wolf или *.dat.
    """
    from engine.project_lock import _anchors

    (tmp_path / "Data" / "BasicData").mkdir(parents=True)
    (tmp_path / "Data" / "BasicData" / "GameBasic.dat").write_bytes(b"")
    (tmp_path / "Game.exe").write_bytes(b"")

    anchors = _anchors(tmp_path, "wolf")
    assert any("GameBasic.dat" in str(a) for a in anchors), (
        f"БАГ: точный якорь Wolf (GameBasic.dat) не используется: {anchors}"
    )

    # Контроль: kirikiri/tyrano по-прежнему используют свой (общий) список —
    # эта правка не должна была их затронуть.
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "system.ks").write_bytes(b"")
    kiri_anchors = _anchors(tmp_path, "kirikiri")
    assert any("system.ks" in str(a) for a in kiri_anchors)
