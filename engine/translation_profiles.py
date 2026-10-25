"""Engine-specific translation rules. Technical preservation and literary style are separate."""
from __future__ import annotations

PROFILES = {
    "rpgmv": {
        "name": "RPG Maker MV", "style": "JRPG / visual-novel game dialogue",
        "rules": "Preserve RPG Maker escape codes such as \\N[n], \\V[n], \\C[n], \\I[n], \\G, \\{, \\}, \\., \\!, \\^, \\|, \\>, \\< exactly. Preserve message choices, actor/item/skill names and database terminology consistently."
    },
    "rpgmz": {
        "name": "RPG Maker MZ", "style": "JRPG / visual-novel game dialogue",
        "rules": "Preserve RPG Maker MZ escape codes, plugin tags and control sequences exactly. Never translate plugin parameters, IDs, filenames or script expressions."
    },
    "vxace": {
        "name": "RPG Maker VX/Ace", "style": "classic JRPG",
        "rules": "Preserve Ruby event/control-code data and binary structure. Translate only extracted human-readable fields. Never change serialized keys, IDs or byte-level structure."
    },
    "unity": {
        "name": "Unity", "style": "game-specific; neutral unless context identifies a genre",
        "rules": "Unity is a container, not a literary genre. Preserve JSON/XML/YAML keys, GUIDs, paths, asset names, localization IDs, placeholders, rich-text tags, printf/specifier tokens, and serialized structure exactly. Translate only human-readable values. Do not translate code, shader text, script identifiers, enum values or resource paths. Keep multiline strings and whitespace semantics."
    },
    "godot": {
        "name": "Godot", "style": "game-specific; neutral unless context identifies a genre",
        "rules": "Preserve Godot resource/property names, node paths, IDs, BBCode tags, format specifiers and script/code syntax. Translate only human-readable string values. Never rewrite resource structure."
    },
    "renpy": {
        "name": "Ren'Py", "style": "visual novel / character-driven dialogue",
        "rules": "Preserve Ren'Py syntax, character variables, interpolation {name}, tags, transforms, labels, menus and Python expressions. Translate dialogue/narration only. Preserve speaker identity and voice."
    },
    "kirikiri": {
        "name": "Kirikiri/KAG", "style": "visual novel",
        "rules": "Preserve KAG tags, macros, parameters, ruby markup and script commands exactly. Translate dialogue/narration and explicitly extracted human-readable text only."
    },
    "tyrano": {
        "name": "TyranoBuilder", "style": "visual novel",
        "rules": "Preserve Tyrano/KAG tags, parameters, macros and script syntax exactly. Translate dialogue/narration only; never alter tag names or attributes."
    },
    "wolf": {
        "name": "Wolf RPG Editor", "style": "JRPG / RPG Maker-like",
        "rules": "Preserve binary table structure, IDs, control bytes and event commands. Translate only fields explicitly classified as human-readable text."
    },
}

def get_profile(game_type: str) -> dict:
    return PROFILES.get(game_type, {"name": game_type or "Unknown", "style": "neutral", "rules": "Preserve all structure and translate only human-readable text."})

def build_instructions(game_type: str, style: str = "") -> str:
    p = get_profile(game_type)
    lines = [f"ENGINE PROFILE: {p['name']}", f"LITERARY STYLE: {style or p['style']}", "ENGINE-SPECIFIC SAFETY RULES:", p["rules"]]
    return "\n".join(lines)
