from pathlib import Path
import json
from engine.asset_safety import snapshot_protected_assets, compare_protected_assets, snapshot_json_targets, compare_json_targets


def test_protected_animation_asset_change_is_detected(tmp_path):
    p = tmp_path / "Assets" / "hero.anim"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"animation-v1")
    before = snapshot_protected_assets(tmp_path)
    p.write_bytes(b"animation-v2")
    assert "Assets/hero.anim" in compare_protected_assets(tmp_path, before)


def test_unrelated_animation_is_unchanged(tmp_path):
    p = tmp_path / "Assets" / "hero.anim"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"animation")
    before = snapshot_protected_assets(tmp_path)
    assert compare_protected_assets(tmp_path, before) == []


def test_json_allows_only_translated_leaf(tmp_path):
    p = tmp_path / "data.json"
    original = {"dialog": "Hello", "animation": {"clip": "walk", "speed": 1.0}}
    p.write_text(json.dumps(original), encoding="utf-8")
    entries = {"data.json": [{"path": "dialog", "original": "Hello", "translated": "Привет"}]}
    before = snapshot_json_targets(tmp_path, entries)
    p.write_text(json.dumps({"dialog": "Привет", "animation": {"clip": "walk", "speed": 1.0}}), encoding="utf-8")
    assert compare_json_targets(tmp_path, entries, before) == []


def test_json_animation_change_is_detected(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"dialog": "Hello", "animation": {"clip": "walk", "speed": 1.0}}), encoding="utf-8")
    entries = {"data.json": [{"path": "dialog", "original": "Hello", "translated": "Привет"}]}
    before = snapshot_json_targets(tmp_path, entries)
    p.write_text(json.dumps({"dialog": "Привет", "animation": {"clip": "run", "speed": 2.0}}), encoding="utf-8")
    assert "data.json" in compare_json_targets(tmp_path, entries, before)


def test_scroll_range_mask_not_falsely_blocking_legit_translation(tmp_path):
    """
    BUG (найдено при аудите покрытия): маска для range-путей проверяла
    регэкспом только ".dialog", хотя extractor.py производит ещё ".scroll"
    (RPG Maker "Show Scrolling Text", code 405) — та же структура, тот же
    _inject_scroll() пишет ровно в parameters[0]. Для ".scroll" маска молча
    не срабатывала (generic-ветка падала на TypeError, пытаясь резолвить
    "10"/"13" как словарные ключи внутри списка), запись оставалась
    немаскированной в снапшоте. Результат: 100% легитимный перевод Scroll
    Text распознавался Safety Guard как "изменение вне разрешённых путей", и
    Inject откатывался для ЛЮБОЙ игры с этой командой — без шанса когда-либо
    успешно вставить перевод.
    """
    p = tmp_path / "data" / "Map001.json"
    p.parent.mkdir(parents=True)
    events = {"events": [{"pages": [{"list": [
        {"code": 405, "parameters": ["長いスクロールテキスト行1"]},
        {"code": 405, "parameters": ["長いスクロールテキスト行2"]},
    ]}]}]}
    p.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    entries = {"data/Map001.json": [
        {"path": "events[0].pages[0].list[0..1].scroll",
         "original": "長いスクロールテキスト行1\n長いスクロールテキスト行2"},
    ]}
    before = snapshot_json_targets(tmp_path, entries)

    legit = json.loads(p.read_text(encoding="utf-8"))
    legit["events"][0]["pages"][0]["list"][0]["parameters"][0] = "Строка прокрутки 1"
    legit["events"][0]["pages"][0]["list"][1]["parameters"][0] = "Строка прокрутки 2"
    p.write_text(json.dumps(legit, ensure_ascii=False), encoding="utf-8")
    assert compare_json_targets(tmp_path, entries, before) == [], (
        "БАГ: легитимный перевод Scroll Text ложно заблокирован Safety Guard'ом"
    )

    # Защита при этом не должна ослабнуть: подмена соседнего параметра на
    # той же scroll-команде по-прежнему обязана детектироваться.
    sneaky = json.loads(p.read_text(encoding="utf-8"))
    sneaky["events"][0]["pages"][0]["list"][0]["parameters"] = ["Строка прокрутки 1", "SNEAKY"]
    p.write_text(json.dumps(sneaky, ensure_ascii=False), encoding="utf-8")
    assert "data/Map001.json" in compare_json_targets(tmp_path, entries, before), (
        "Подмена parameters[1] на scroll-команде должна детектироваться"
    )

def test_dialog_range_mask_covers_only_parameters_zero(tmp_path):
    """
    "[N..M].dialog" раньше прятала ВЕСЬ parameters, хотя _inject_dialog() в
    injector.py пишет ровно в parameters[0] и больше никуда. Guard не увидел
    бы, если бы что-то (баг в пайплайне, повреждение файла) одновременно с
    легитимным изменением текста подменило parameters[1] (например ID
    анимации/звука на той же команде).
    """
    p = tmp_path / "data" / "Map001.json"
    p.parent.mkdir(parents=True)
    events = {"events": [{"pages": [{"list": [
        {"code": 401, "indent": 0, "parameters": ["こんにちは"]},
        {"code": 401, "indent": 0, "parameters": ["元気ですか"]},
    ]}]}]}
    p.write_text(json.dumps(events), encoding="utf-8")
    entries = {"data/Map001.json": [
        {"path": "events[0].pages[0].list[0..1].dialog", "original": "こんにちは\n元気ですか"},
    ]}
    before = snapshot_json_targets(tmp_path, entries)

    # Легитимное изменение: только parameters[0], ровно как делает инжектор.
    legit = json.loads(p.read_text(encoding="utf-8"))
    legit["events"][0]["pages"][0]["list"][0]["parameters"][0] = "Привет"
    legit["events"][0]["pages"][0]["list"][1]["parameters"][0] = "Как дела"
    p.write_text(json.dumps(legit), encoding="utf-8")
    assert compare_json_targets(tmp_path, entries, before) == [], (
        "Легитимное изменение только текста не должно флаговаться"
    )

    # Подозрительное: текст поменялся, но заодно появился НЕОЖИДАННЫЙ второй
    # элемент parameters на той же команде — это инжектор никогда не пишет.
    sneaky = json.loads(p.read_text(encoding="utf-8"))
    sneaky["events"][0]["pages"][0]["list"][0]["parameters"] = ["Привет", "SNEAKY"]
    p.write_text(json.dumps(sneaky), encoding="utf-8")
    assert "data/Map001.json" in compare_json_targets(tmp_path, entries, before), (
        "БАГ: изменение parameters[1] на той же команде должно быть замечено"
    )
