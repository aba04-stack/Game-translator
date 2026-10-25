"""Animation/asset safety guard for RPGMTL.

The guard proves that Inject did not touch non-text game assets and, for JSON
text resources, that only paths present in the translation manifest changed.
It intentionally treats uncertainty as a failure: a suspicious change blocks
success instead of claiming the game is safe.
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any

IGNORED_DIRS = {"_backup_original", "_backup_unity_original", "_rpgmtl_safety"}
PROTECTED_EXTS = {
    ".anim", ".controller", ".overridecontroller", ".mask", ".avatar",
    ".fbx", ".blend", ".dae", ".obj", ".mtl", ".gltf", ".glb",
    ".png", ".jpg", ".jpeg", ".webp", ".tga", ".dds", ".psd",
    ".wav", ".ogg", ".mp3", ".flac", ".mp4", ".mov",
    ".bundle", ".unity3d", ".resS", ".resource", ".assets",
    ".assetbundle", ".shader", ".shadergraph", ".mat", ".prefab",
    ".unity", ".ttf", ".otf",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ignored(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(p in IGNORED_DIRS or p.startswith("_backup_") for p in parts)


def snapshot_protected_assets(game_dir: Path) -> dict[str, str]:
    """Hash animation/media/binary asset files that Inject must never touch."""
    root = Path(game_dir)
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for p in root.rglob("*"):
        if not p.is_file() or _ignored(p, root):
            continue
        if p.suffix.lower() in {x.lower() for x in PROTECTED_EXTS}:
            try:
                result[p.relative_to(root).as_posix()] = sha256(p)
            except OSError:
                pass
    return result


def compare_protected_assets(game_dir: Path, before: dict[str, str]) -> list[str]:
    """Return protected assets that disappeared, changed, or appeared."""
    root = Path(game_dir)
    after = snapshot_protected_assets(root)
    changed = [k for k, v in before.items() if after.get(k) != v]
    # A new protected asset is suspicious too: an injector should not create
    # animation/audio/image/binary assets as a side effect.
    changed += [k for k in after if k not in before]
    return sorted(set(changed))


def _tokenize(path: str) -> list[tuple[str, str]]:
    import re
    out = []
    for m in re.finditer(r"\[(\d+)\]|([^\.\[\]]+)", path.strip(".")):
        out.append(("index", m.group(1)) if m.group(1) is not None else ("key", m.group(2)))
    return out


def _mask_json_path(root: Any, path: str) -> bool:
    """Replace a translated JSON leaf/range with a sentinel in-place."""
    import re
    # RPG Maker event range: list[10..13].dialog / list[10..13].scroll
    # BUG FIX (найдено при аудите покрытия P0/P1 фиксов): регэксп проверял
    # только ".dialog", хотя extractor.py производит ДВА типа range-путей —
    # ".dialog" (Show Text, code 401) и ".scroll" (Show Scrolling Text, code
    # 405) — оба одинаково устроены, оба пишутся через parameters[0]
    # (_inject_dialog/_inject_scroll в injector.py). Для ".scroll" маска
    # молча падала (generic-ветка ниже пыталась резолвить "10"/"13" как
    # словарные ключи внутри list — TypeError, return False), запись
    # оставалась НЕМАСКИРОВАННОЙ в снапшоте. Результат: 100% легитимный
    # перевод Scroll Text распознавался как "изменение вне разрешённых
    # путей" и Safety Guard откатывал ЛЮБОЙ Inject для игры, использующей
    # Scroll Text — без возможности когда-либо успешно вставить перевод.
    m = re.search(r"\[(\d+)\.\.(\d+)\]\.(?:dialog|scroll)$", path)
    if m:
        prefix = path[:m.start()]
        parent = root
        for kind, key in _tokenize(prefix):
            try:
                parent = parent[int(key)] if kind == "index" else parent[key]
            except Exception:
                return False
        if not isinstance(parent, list):
            return False
        # BUG FIX (P1-1, внешний аудит, подтверждено чтением injector.py):
        # раньше маскировался ВЕСЬ parameters (parent[i]["parameters"] = [...]),
        # хотя _inject_dialog() в injector.py пишет РОВНО в parameters[0]
        # (params[0] = line) и больше никуда. Маскируя весь массив, guard не
        # видел бы изменение parameters[1]/[2]/... (для 401/405 у Show Text
        # обычно один элемент, но маска была шире, чем реально разрешённая
        # запись) — сужаем маску до того самого единственного индекса, куда
        # инжектор имеет право писать. Всё остальное в объекте команды
        # (включая другие элементы parameters, code, indent) остаётся
        # немаскированным и по-прежнему детектируется как подозрительное.
        for i in range(int(m.group(1)), int(m.group(2)) + 1):
            if 0 <= i < len(parent) and isinstance(parent[i], dict):
                params = parent[i].get("parameters")
                if isinstance(params, list) and params:
                    params[0] = "__RPGMTL_ALLOWED_CHANGE__"
        return True

    parts = _tokenize(path)
    if not parts:
        return False
    obj = root
    for kind, key in parts[:-1]:
        try:
            obj = obj[int(key)] if kind == "index" else obj[key]
        except Exception:
            return False
    kind, key = parts[-1]
    try:
        if kind == "index":
            obj[int(key)] = "__RPGMTL_ALLOWED_CHANGE__"
        else:
            obj[key] = "__RPGMTL_ALLOWED_CHANGE__"
        return True
    except Exception:
        return False


def _canonical_json_without_allowed(path: Path, entries: list[dict]) -> bytes | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for e in entries:
        p = str(e.get("path", ""))
        _mask_json_path(data, p)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def snapshot_json_targets(game_dir: Path, entries_by_source: dict[str, list[dict]]) -> dict[str, str]:
    """Snapshot JSON resources while masking exactly the translated paths."""
    out: dict[str, str] = {}
    root = Path(game_dir)
    for rel, entries in entries_by_source.items():
        p = root / rel
        if not p.is_file() or p.suffix.lower() != ".json":
            continue
        data = _canonical_json_without_allowed(p, entries)
        if data is not None:
            out[rel] = hashlib.sha256(data).hexdigest()
    return out


def compare_json_targets(game_dir: Path, entries_by_source: dict[str, list[dict]], before: dict[str, str]) -> list[str]:
    out = []
    after = snapshot_json_targets(game_dir, entries_by_source)
    for rel, digest in before.items():
        if after.get(rel) != digest:
            out.append(rel)
    return sorted(out)
