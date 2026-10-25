"""Project identity and cross-game safety for RPGMTL.

A translation workspace is permanently bound to the game it was extracted from.
The lock is path-independent: moving/copying the same game is allowed, but using
its translation workspace with another game is refused before any file is changed.
"""
from __future__ import annotations
import hashlib, json, os
from pathlib import Path

MANIFEST = "project_manifest.json"
VERSION = 2


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _anchors(game_dir: Path, game_type: str) -> list[Path]:
    candidates: list[Path] = []
    if game_type in {"rpgmv", "rpgmz"}:
        candidates = [
            game_dir / "data" / "System.json", game_dir / "www" / "data" / "System.json",
            game_dir / "js" / "rmmz_core.js", game_dir / "js" / "rpg_core.js",
            game_dir / "index.html", game_dir / "www" / "index.html",
        ]
    elif game_type == "vxace":
        candidates = [game_dir / "Game.ini", game_dir / "Game.rvproj2", game_dir / "Data" / "System.rvdata2"]
    elif game_type == "unity":
        candidates = [
            game_dir / "ProjectSettings" / "ProjectVersion.txt",
            game_dir / "ProjectSettings" / "ProjectSettings.asset",
        ]
        try:
            candidates += [p for p in game_dir.iterdir() if p.is_file() and p.suffix.lower() in {".exe", ".x86_64", ".app"}]
        except Exception:
            pass
        # Built Unity games often have *_Data/globalgamemanagers as a strong identity anchor.
        try:
            candidates += [p / "globalgamemanagers" for p in game_dir.iterdir() if p.is_dir() and p.name.endswith("_Data")]
        except Exception:
            pass
    elif game_type == "godot":
        candidates = [game_dir / "project.godot"]
    elif game_type == "renpy":
        candidates = [game_dir / "game" / "script.rpy", game_dir / "game" / "options.rpy", game_dir / "game" / "gui.rpy"]
    elif game_type == "wolf":
        # BUG FIX (найдено систематической проверкой по-движковых списков,
        # тот же класс, что rpgmv/rpgmz в _ENGINE_TOKEN_PATTERNS): Wolf делил
        # anchor-список с kirikiri/tyrano ("Game.exe", "index.html",
        # "data/system.ks"), но ни index.html (веб-специфика Tyrano), ни
        # system.ks (KAG-скрипт, Wolf вообще не использует .ks) у настоящей
        # Wolf RPG игры не существуют — реально совпадает только Game.exe,
        # не очень отличительный маркер (общий для многих движков). У
        # собственного детектора Wolf (game_detector._check_wolf) уже есть
        # куда более точный маркер: Data/BasicData/*.wolf или *.dat.
        # GameBasic.dat — стандартное, конвенциональное имя этого файла в
        # каждом Wolf RPG Editor проекте.
        candidates = [
            game_dir / "Data" / "BasicData" / "GameBasic.dat",
            game_dir / "data" / "BasicData" / "GameBasic.dat",
            game_dir / "Game.exe",
        ]
    elif game_type in {"kirikiri", "tyrano"}:
        candidates = [game_dir / "Game.exe", game_dir / "index.html", game_dir / "data" / "system.ks"]
    existing = []
    seen = set()
    for p in candidates:
        if p.exists() and p.is_file():
            rp = str(p.resolve())
            if rp not in seen:
                seen.add(rp); existing.append(p)
    return existing


def fingerprint(game_dir: Path, game_type: str) -> dict:
    game_dir = Path(game_dir).resolve()
    anchors = _anchors(game_dir, game_type)
    anchor_data = []
    for p in anchors[:12]:
        try:
            anchor_data.append({"relative": p.relative_to(game_dir).as_posix(), "sha256": _sha256(p), "size": p.stat().st_size})
        except Exception:
            pass
    # If no strong anchor exists, use a bounded inventory of filenames/sizes. This is
    # deliberately not a full tree hash because translation must not make the identity stale.
    inventory = []
    try:
        for p in sorted(game_dir.rglob("*"), key=lambda x: str(x).lower()):
            if p.is_file() and len(inventory) < 3000:
                rel = p.relative_to(game_dir).as_posix()
                if any(part in {".git", "node_modules", "Temp", "Library"} for part in p.parts):
                    continue
                inventory.append((rel, p.stat().st_size))
    except Exception:
        pass
    payload = {"game_type": game_type, "anchors": anchor_data, "inventory": inventory}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["fingerprint"] = hashlib.sha256(raw).hexdigest()
    return payload


def _collect_extracted_sources(output_dir: Path, game_dir: Path) -> list[dict]:
    """Build an exact source-file manifest from extracted entries/index metadata."""
    extract_dir = Path(output_dir) / "extracted"
    index_path = Path(output_dir) / "extract_index.json"
    rels: set[str] = set()
    if index_path.exists():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            sources = idx.get("sources", {})
            if isinstance(sources, dict):
                for rel in sources.values():
                    if isinstance(rel, str) and rel.strip():
                        rels.add(rel.replace("\\", "/").lstrip("/"))
            # Some engines expose only file names in `files`.
            for rel in (idx.get("files") or {}).keys():
                if isinstance(rel, str) and rel.strip():
                    rels.add(rel.replace("\\", "/").lstrip("/"))
        except Exception:
            pass
    if extract_dir.exists():
        for jf in extract_dir.glob("*.json"):
            if jf.name.endswith("_translated.json"):
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                for key in ("source_path", "_source_path", "file", "_source_rvdata2"):
                    rel = entry.get(key)
                    if isinstance(rel, str) and rel.strip():
                        rels.add(rel.replace("\\", "/").lstrip("/"))
                        break

    def resolve(rel: str) -> Path | None:
        raw = Path(rel)
        game_root = Path(game_dir).resolve()
        candidates = [Path(game_dir) / raw]
        for root_name in ("data", "Data", "www/data", "Assets", "StreamingAssets", "Resources", "game"):
            candidates.append(Path(game_dir) / root_name / raw)
        found=[]
        for c in candidates:
            try:
                # BUG FIX (P1-3, внешний аудит, подтверждено эмпирически):
                # rel приходит из extract_index.json/extracted-записей и
                # только lstrip("/") — "../../evil.json" проходит невредимым.
                # c.is_file() резолвит ".." на уровне ОС и вернёт True для
                # файла ВНЕ game_dir, а c.relative_to(game_dir) на
                # неразрешённом Path тоже не ловит это (просто вернёт
                # "../../evil.json" как "относительный" путь). Явно проверяем
                # разрешённый (resolve()) путь на принадлежность game_root —
                # никакой traversal наружу, даже если такая запись как-то
                # попала в extract_index.json.
                if not c.resolve().is_relative_to(game_root):
                    continue
                if c.is_file(): found.append(c)
            except OSError: pass
        # If the extractor stored only a basename, accept it only when unique.
        if not found:
            matches=[]
            try:
                for c in Path(game_dir).rglob(raw.name):
                    if c.is_file(): matches.append(c)
                    if len(matches)>1: break
            except OSError: pass
            if len(matches)==1:
                return matches[0]
            return None
        # Prefer the shortest conventional engine path.
        return sorted(found, key=lambda x: len(x.as_posix()))[0]

    result=[]
    seen=set()
    for rel in sorted(rels):
        src=resolve(rel)
        if src is None:
            continue
        try:
            actual_rel=src.relative_to(Path(game_dir)).as_posix()
            if actual_rel in seen: continue
            seen.add(actual_rel)
            result.append({"relative": actual_rel, "sha256": _sha256(src), "size": src.stat().st_size})
        except (OSError, ValueError):
            pass
    return result


def _verify_source_manifest(saved: dict, game_dir: Path) -> tuple[bool, str]:
    sources = saved.get("source_files") or []
    if not sources:
        return True, "OK"
    backup_roots = [Path(game_dir) / "_backup_original", Path(game_dir) / "_backup_unity_original"]
    mismatches=[]
    for item in sources:
        rel=str(item.get("relative", ""))
        expected=str(item.get("sha256", ""))
        if not rel or not expected:
            continue
        live=Path(game_dir) / rel
        candidate=live
        try:
            if live.is_file() and _sha256(live)==expected:
                continue
        except OSError:
            pass
        # Inject legitimately changes live files. Compare the immutable original backup.
        for root in backup_roots:
            bp=root / rel
            if bp.is_file():
                candidate=bp
                break
            # RPG Maker's legacy injector stores Data/*.json backups flat.
            flat=root / Path(rel).name
            if flat.is_file():
                candidate=flat
                break
        try:
            if candidate.is_file() and _sha256(candidate)==expected:
                continue
        except OSError:
            pass
        mismatches.append(rel)
        if len(mismatches)>=5:
            break
    if mismatches:
        return False, "Исходные файлы игры не совпадают с Extract: " + ", ".join(mismatches)
    return True, "OK"

def read_manifest(output_dir: Path) -> dict | None:
    p = Path(output_dir) / MANIFEST
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except Exception:
        return None


def write_manifest(output_dir: Path, game_dir: Path, game_type: str) -> dict:
    fp = fingerprint(game_dir, game_type)
    data = {"version": VERSION, "game_type": game_type, "fingerprint": fp["fingerprint"],
            "anchors": fp["anchors"], "inventory": fp["inventory"],
            "source_files": _collect_extracted_sources(output_dir, game_dir)}
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    tmp = Path(output_dir) / ".project_manifest.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, Path(output_dir) / MANIFEST)
    return data


def verify_manifest(output_dir: Path, game_dir: Path, game_type: str) -> tuple[bool, str]:
    saved = read_manifest(output_dir)
    if not saved:
        return False, "Рабочая папка не привязана к игре: отсутствует project_manifest.json. Выполните Extract заново."
    if saved.get("game_type") != game_type:
        return False, f"Рабочая папка принадлежит движку {saved.get('game_type')}, а текущая игра определена как {game_type}."
    source_ok, source_msg = _verify_source_manifest(saved, game_dir)
    if not source_ok:
        return False, source_msg
    # Version 2+ workspaces have exact hashes for every source file actually
    # translated. Those hashes are stronger than the bounded inventory fallback
    # and remain valid after a legitimate Inject because immutable backups are
    # accepted by _verify_source_manifest().
    if saved.get("source_files"):
        return True, "OK"
    current = fingerprint(game_dir, game_type)
    old_anchors = {x.get("relative"): x.get("sha256") for x in saved.get("anchors", [])}
    cur_anchors = {x.get("relative"): x.get("sha256") for x in current.get("anchors", [])}
    if old_anchors:
        missing = [k for k in old_anchors if k not in cur_anchors]
        changed = [k for k in old_anchors if k in cur_anchors and old_anchors[k] != cur_anchors[k]]
        # A legitimate Inject changes some source files. The injector keeps the
        # pre-translation copy in _backup_original; accept a changed live anchor
        # only when that backup is byte-for-byte the original anchor recorded at Extract.
        backup_dir = Path(game_dir) / "_backup_original"
        still_changed = []
        for rel in changed:
            bp = backup_dir / rel
            if not bp.exists():
                # Legacy RPG Maker injector stores Data/*.json backups flat.
                candidate = backup_dir / Path(rel).name
                if candidate.exists():
                    bp = candidate
            if not bp.exists() or _sha256(bp) != old_anchors[rel]:
                still_changed.append(rel)
        if missing or still_changed:
            details = []
            if missing: details.append("пропали: " + ", ".join(missing[:3]))
            if still_changed: details.append("изменены/не совпадают с оригиналом: " + ", ".join(still_changed[:3]))
            return False, "Игра не совпадает с той, из которой был сделан Extract (" + "; ".join(details) + ")."
        return True, "OK"
    # Weak legacy manifest: require a meaningful inventory overlap.
    old_inv = dict(saved.get("inventory", [])); cur_inv = dict(current.get("inventory", []))
    common = sum(1 for k, v in old_inv.items() if cur_inv.get(k) == v)
    if old_inv and common < max(3, int(len(old_inv) * 0.15)):
        return False, "Игра не совпадает с исходной: слишком мало совпавших файлов."
    return True, "OK"
