"""
RPG Maker MV/MZ — встроенный движок.

Модули:
  extractor    — извлечение текста из data/*.json
  injector     — вставка переводов обратно
  unity_*      — поддержка Unity-текстов и локализаций
  text_filter  — фильтрация непереводимых строк
  deduplicator — дедупликация: один перевод на уникальную строку
  glossary     — глоссарий терминов и имён
  state        — кэш и resume переводов
"""

from .extractor import extract_game
from .injector import inject_translations
from .unity_extractor import extract_unity
from .unity_injector import inject_unity
from .deduplicator import build_dedup_map, apply_dedup_to_files
from .glossary import Glossary, load_or_create_glossary
from .state import TranslationState, load_state

__all__ = [
    "extract_game",
    "inject_translations",
    "extract_unity",
    "inject_unity",
    "build_dedup_map",
    "apply_dedup_to_files",
    "Glossary",
    "load_or_create_glossary",
    "TranslationState",
    "load_state",
]
