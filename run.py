"""
run.py — точка запуска. Проверяет зависимости и запускает GUI.
"""

import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).parent
sys.path.insert(0, str(DESKTOP_DIR))

REQUIRED = {"PyQt6": "PyQt6>=6.5.0"}

# Каждая запись OPTIONAL — кортеж имён модулей: достаточно, чтобы ЛЮБОЙ из них
# импортировался. Для Gemini это google.genai (актуальный SDK, он в requirements.txt)
# или старый google.generativeai (снят с поддержки, но код умеет работать и с ним,
# если он уже установлен) — см. провайдер Gemini в ai_translator.py. Проверяется
# полное точечное имя, а не только пространство имён «google».
OPTIONAL = {
    "openai":    ("openai",),
    "gemini":    ("google.genai", "google.generativeai"),
    "anthropic": ("anthropic",),
}
PKG_HINT = {
    "openai":    "openai>=1.0.0",
    "gemini":    "google-genai>=1.0.0",
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
