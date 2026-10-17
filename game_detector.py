"""
game_detector.py — определение типа игры по содержимому папки.
Поддерживает: RPG Maker MV, RPG Maker MZ, Ren'Py.
"""

from engine.file_utils import find_dir_ci
import json
from pathlib import Path


def detect_game_type(game_dir: Path) -> str:
    """
    Определить тип игры.
    Возвращает: 'rpgmv' | 'rpgmz' | 'renpy' | 'wolf' | 'vxace' |
                'kirikiri' | 'tyrano' | 'unity' | 'godot' | 'unknown'
    """
    return detect_game_type_verbose(game_dir)[0]


def _check_renpy(game_dir: Path) -> tuple[str, str] | None:
    game_sub = game_dir / "game"
    if game_sub.is_dir() and any(game_sub.rglob("*.rpy")):
        return "renpy", "Найдено: game/*.rpy"
    if any(game_dir.glob("*.rpy")):
        return "renpy", "Найдено: *.rpy в корне"
    return None


def _check_unity(game_dir: Path) -> tuple[str, str] | None:
    if (game_dir / "ProjectSettings" / "ProjectVersion.txt").exists():
        return "unity", "Найдено: ProjectSettings/ProjectVersion.txt"
    if (game_dir / "Assets").is_dir() and (game_dir / "ProjectSettings").is_dir():
        return "unity", "Найдено: Assets/ + ProjectSettings/"
    for child in game_dir.iterdir():
        if child.is_dir() and child.name.endswith("_Data"):
            markers = [
                child / "globalgamemanagers",
                child / "Resources",
                child / "Managed",
                child / "StreamingAssets",
            ]
            hit = next((m for m in markers if m.exists()), None)
            if hit:
                return "unity", f"Найдено: {child.name}/{hit.name}"
    return None


# Настоящий RPG Maker System.json — узнаваемая схема (gameTitle, versionId,
# windowTone, partyMembers, battlerName и т.д.). Используем это как
# ДОПОЛНИТЕЛЬНЫЙ сигнал уверенности в reason (видно в логах), но не как
# жёсткий фильтр — совсем пустой или урезанный System.json у реальной (пусть
# и нетипичной) RPG Maker игры не должен переставать определяться. Основная
# защита от конфликтов с другими движками — multi-match репортинг в
# detect_game_type_verbose ниже, а не блокировка по содержимому здесь.
_RPGM_SYSTEM_JSON_KEYS = {"gameTitle", "versionId", "windowTone", "partyMembers", "battlerName", "terms", "elements"}


def _check_rpgmaker(game_dir: Path) -> tuple[str, str] | None:
    for candidate in ["data", "www/data", "Data"]:
        data_dir = game_dir / candidate
        if not data_dir.is_dir():
            continue
        sys_json = data_dir / "System.json"
        if not sys_json.exists():
            continue

        matched_keys: list[str] = []
        try:
            data = json.loads(sys_json.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                matched_keys = sorted(_RPGM_SYSTEM_JSON_KEYS & data.keys())
        except Exception:
            pass  # не парсится/пустой — не страшно, ниже определяем всё равно по факту наличия файла
        confidence = f" (ключи: {matched_keys})" if matched_keys else ""

        if (game_dir / "js" / "rmmz_core.js").exists():
            return "rpgmz", f"Найдено: {candidate}/System.json{confidence} + js/rmmz_core.js"

        pkg = game_dir / "package.json"
        if pkg.exists():
            try:
                info = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
                if "mz" in info.get("name", "").lower():
                    return "rpgmz", f"Найдено: {candidate}/System.json{confidence} + package.json(name содержит 'mz')"
            except Exception:
                pass

        if (game_dir / "js" / "rpg_core.js").exists():
            return "rpgmv", f"Найдено: {candidate}/System.json{confidence} + js/rpg_core.js"

        return "rpgmv", f"Найдено: {candidate}/System.json{confidence} (без версионных маркеров, fallback → MV)"
    return None


def _check_godot(game_dir: Path) -> tuple[str, str] | None:
    if (game_dir / "project.godot").exists():
        return "godot", "Найдено: project.godot"
    # BUG FIX: .pck alone is unreliable — also used by RPG Maker plugins.
    # Godot packages an .exe + .pck with matching stems.
    if any(game_dir.glob("*.pck")):
        pck_stems = {p.stem for p in game_dir.glob("*.pck")}
        exe_stems = {e.stem for e in list(game_dir.glob("*.exe")) + list(game_dir.glob("*.x86_64"))}
        if pck_stems & exe_stems:
            return "godot", "Найдено: *.pck с совпадающим именем *.exe/*.x86_64"
    return None


def _check_tyrano(game_dir: Path) -> tuple[str, str] | None:
    # BUG FIX: removed the third condition `(game_dir/"data"/"scenario").is_dir()`
    # which was ALWAYS True when _tyrano_scenario == game_dir/"data"/"scenario",
    # causing every Kirikiri/KAG game with data/scenario/*.ks to be mis-detected as Tyrano.
    # Tyrano requires either its own runtime folder ("tyrano/") or an HTML launcher.
    scenario_dirs = [
        game_dir / "data" / "scenario",
        game_dir / "tyrano" / "scenario",
        game_dir / "scenario",
    ]
    scenario = next((p for p in scenario_dirs if p.is_dir() and list(p.glob("*.ks"))), None)
    if scenario and ((game_dir / "tyrano").is_dir() or (game_dir / "index.html").exists()):
        return "tyrano", f"Найдено: {scenario}/*.ks + tyrano/ или index.html"
    return None


def _check_wolf(game_dir: Path) -> tuple[str, str] | None:
    # Wolf RPG Editor: Game.exe + Data/BasicData/*.wolf
    wolf_data = find_dir_ci(game_dir, "Data")
    for wolf_dir in [
        (wolf_data / "BasicData" if wolf_data else None),
        game_dir / "data" / "BasicData",
        (wolf_data / "basicdata" if wolf_data else None),
    ]:
        if wolf_dir and wolf_dir.is_dir() and (
            list(wolf_dir.glob("*.wolf")) or list(wolf_dir.glob("*.dat"))
        ):
            return "wolf", f"Найдено: {wolf_dir}/*.wolf или *.dat"
    # Wolf RPG без подпапки
    for candidate in filter(None, [wolf_data, game_dir / "data"]):
        if candidate.is_dir() and list(candidate.glob("*.wolf")):
            return "wolf", f"Найдено: {candidate}/*.wolf"
    return None


def _check_vxace(game_dir: Path) -> tuple[str, str] | None:
    for candidate in ["Data", "data"]:
        d = game_dir / candidate
        if d.is_dir() and list(d.glob("*.rvdata2")):
            return "vxace", f"Найдено: {candidate}/*.rvdata2"
    return None


def _check_kirikiri(game_dir: Path) -> tuple[str, str] | None:
    # Kirikiri/KAG: .ks файлы (после TyranoBuilder — оба движка используют .ks)
    for candidate in [
        game_dir / "data" / "scenario",
        game_dir / "data" / "Scenario",
        game_dir / "scenario",
    ]:
        if candidate.exists() and list(candidate.glob("*.ks")):
            return "kirikiri", f"Найдено: {candidate}/*.ks"
    if list(game_dir.rglob("*.ks")):
        return "kirikiri", "Найдено: *.ks где-то в дереве папок"
    return None


# Порядок задаёт приоритет ПОБЕДИТЕЛЯ при конфликте (см. BUG FIX в
# detect_game_type_verbose ниже) — самые специфичные/трудноподделываемые
# маркеры первыми, самый общий (RPG Maker, теперь content-валидированный,
# но исторически самый общий по имени файла) — сразу после Unity, т.к. это
# основной целевой движок инструмента.
_CHECKS = [
    _check_renpy,
    _check_unity,
    _check_rpgmaker,
    _check_godot,
    _check_tyrano,
    _check_wolf,
    _check_vxace,
    _check_kirikiri,
]


def detect_game_type_verbose(game_dir: Path) -> tuple[str, str]:
    """
    Определить тип игры + причину (для диагностики в логах).
    Возвращает (type_key, reason), где reason — человекочитаемое объяснение,
    какой маркер сработал.

    BUG FIX: раньше функция возвращалась при ПЕРВОМ совпадении — если папка
    случайно подходила под маркеры двух движков одновременно (см. историю:
    Unity-проект со своим data/System.json ловился как RPG Maker раньше,
    чем вообще проверялись Unity-маркеры), об этом не было ни строки в
    логах — молча побеждал более ранний по порядку. Теперь собираем
    совпадения СО ВСЕХ проверок (не останавливаясь на первой) и явно
    предупреждаем в reason, если подошло больше одного движка — вместо
    того, чтобы полагаться только на правильный порядок проверок навсегда.
    """
    game_dir = Path(game_dir)

    if not game_dir.is_dir():
        return "unknown", f"Папка не существует: {game_dir}"

    matches: list[tuple[str, str]] = []
    for check in _CHECKS:
        try:
            result = check(game_dir)
        except Exception:
            continue
        if result:
            matches.append(result)

    if not matches:
        return "unknown", "Ни один известный маркер движка не найден"

    winner_type, winner_reason = matches[0]
    if len(matches) > 1:
        others = "; ".join(f"{t} ({r})" for t, r in matches[1:])
        winner_reason += f" [⚠ также похоже на: {others} — если тип определён неверно, сообщите]"
    return winner_type, winner_reason


_LABELS: dict[str, str] = {
    "rpgmv":    "RPG Maker MV",
    "rpgmz":    "RPG Maker MZ",
    "unity":    "Unity",
    "godot":    "Godot Engine",
    "renpy":    "Ren'Py",
    "kirikiri": "Kirikiri/KAG",
    "tyrano":   "TyranoBuilder",
    "vxace":    "RPG Maker VX/Ace",
    "wolf":     "Wolf RPG Editor",
    "unknown":  "Неизвестный тип",
}

_ICONS: dict[str, str] = {
    "rpgmv":    "🎮",
    "rpgmz":    "🎮",
    "unity":    "🎲",
    "godot":    "🤖",
    "renpy":    "📖",
    "kirikiri": "🌸",
    "tyrano":   "🦖",
    "vxace":    "🎮",
    "wolf":     "🐺",
    "unknown":  "❓",
}

_COLORS: dict[str, str] = {
    "rpgmv":    "#66BB6A",
    "rpgmz":    "#4FC3F7",
    "unity":    "#9575CD",
    "godot":    "#478CBF",
    "renpy":    "#FFB74D",
    "kirikiri": "#CE93D8",
    "tyrano":   "#E57373",
    "vxace":    "#81C784",
    "wolf":     "#FFCC02",
    "unknown":  "#EF5350",
}


def detect_game_type_label(game_dir: Path) -> str:
    """Человекочитаемое название типа игры."""
    return _LABELS.get(detect_game_type(game_dir), "Неизвестно")


def detect_game_display(game_dir: Path) -> tuple[str, str, str]:
    """
    Возвращает (type_key, label_with_icon, css_color).
    """
    t = detect_game_type(game_dir)
    icon = _ICONS.get(t, "")
    label = _LABELS.get(t, t)
    color = _COLORS.get(t, "#888")
    return t, f"{icon}  {label}", color
