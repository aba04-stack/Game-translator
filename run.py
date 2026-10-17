"""
run.py — точка запуска. Проверяет зависимости и запускает GUI.
"""

import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).parent
sys.path.insert(0, str(DESKTOP_DIR))

REQUIRED = {"PyQt6": "PyQt6>=6.5.0"}

# BUG FIX: OPTIONAL used to map "google.genai" -> "google-genai>=1.0.0" and
# check ONLY that one dotted name. requirements.txt installs
# google-generativeai (the legacy SDK) by default — google-genai is commented
# out as an optional upgrade — and ai_translator.py's Gemini provider already
# tries google.genai first, falling back to google.generativeai if that's
# not installed (see ai_translator.py around the Gemini provider init). So a
# completely standard `pip install -r requirements.txt`, followed by
# `python run.py`, reported Gemini as NOT installed even though it works
# fine via the fallback — misleading the person into thinking they needed
# yet another package. Each OPTIONAL entry is now a tuple of import names;
# any one of them being importable counts as "available".
OPTIONAL = {
    "openai":    ("openai",),
    "gemini":    ("google.genai", "google.generativeai"),
    "anthropic": ("anthropic",),
}
PKG_HINT = {
    "openai":    "openai>=1.0.0",
    "gemini":    "google-genai>=1.0.0  (или google-generativeai>=0.8.0 — см. requirements.txt)",
    "anthropic": "anthropic>=0.25.0",
}


def check(name: str) -> bool:
    """Check if a module is importable using its full dotted path."""
    try:
        # BUG FIX: was __import__(name.split(".")[0]) which only checked the top-level
        # namespace package. "google.genai" → __import__("google") succeeds even without
        # google-genai installed because "google" is a namespace package.
        # Use importlib for full dotted-path check.
        import importlib
        importlib.import_module(name)
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def main():
    print("═" * 56)
    print("  RPG Maker MV/MZ — AI Переводчик")
    print("  Встроенный движок · Dedup · Glossary · Resume")
    print("═" * 56)
    print()

    print("[Check] Обязательные зависимости:")
    all_ok = True
    for name, pkg in REQUIRED.items():
        if check(name):
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name} — НЕ УСТАНОВЛЕН  →  pip install {pkg}")
            all_ok = False

    print()
    print("[Check] AI провайдеры (нужен хотя бы один):")
    any_ai = False
    for name, import_names in OPTIONAL.items():
        if any(check(n) for n in import_names):
            print(f"  ✓ {name}")
            any_ai = True
        else:
            print(f"  - {name}  (pip install {PKG_HINT[name]})")

    print()
    if not all_ok:
        print("ОШИБКА: установите обязательные пакеты:")
        print("  pip install -r desktop/requirements.txt")
        sys.exit(1)

    if not any_ai:
        print("ПРЕДУПРЕЖДЕНИЕ: ни один AI-провайдер не установлен.")
        print("  Шаг Translate работать не будет.")
        print("  Установите: pip install openai")
        print()

    print("[Start] Запуск GUI...")
    print()

    from rpgmtl_desktop import main as gui
    gui()


if __name__ == "__main__":
    main()
