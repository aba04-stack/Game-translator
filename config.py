"""config.py — persistent settings for RPG Maker AI Translator."""

import json
import os
import tempfile
from pathlib import Path

# BUG FIX: CONFIG_PATH используется Path.home() безусловно. Тестовый сьют
# гоняет MainWindow()/SettingsPanel десятки раз, и часть путей (например
# запуск _run_translate) по дизайну сохраняет настройки как "последняя
# сессия" — это нужное поведение для реального приложения, но без изоляции
# оно тихо перезаписывает НАСТОЯЩИЙ ~/.rpgmtl_config.json (включая реальный
# api_key, если тесты запускаются на машине, где RPGMTL уже настроен для
# работы) фейковыми тестовыми значениями и мёртвыми временными путями.
# Обнаружено эмпирически: после прогона сьюта в ~/.rpgmtl_config.json
# оказался "sk-test-fake-key" и несуществующие /tmp/... пути. RPGMTL_HOME
# выставляется один раз в tests/conftest.py на временную папку для всей
# тестовой сессии — не влияет на реальных пользователей (переменная не
# установлена вне тестов, поведение не меняется).
CONFIG_PATH = Path(os.environ.get("RPGMTL_HOME", str(Path.home()))) / ".rpgmtl_config.json"

DEFAULTS: dict = {
    "provider":           "openai",
    "api_key":            "",
    "model":              "",
    "base_url":           "",
    "source_lang":        "auto",
    "target_lang":        "Russian",
    "batch_size":         25,
    "max_workers":        3,
    "max_retries":        3,
    "request_delay":      0,
    "apply_warp":         True,
    "warp_width":         400,
    "recent_games":       [],
    "extra_instructions": "",
    "quality_mode":       "balanced",
    "auto_game_profile":  True,
    "use_context":        True,   # derived from quality_mode but can be overridden
    "timeout":            60,     # API request timeout in seconds
    "game_dir":           "",
    "output_dir":         "",
}


def load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = {**DEFAULTS, **data}
            # Валидируем типы — приводим к типам из DEFAULTS
            for key, default_val in DEFAULTS.items():
                if key not in merged:
                    continue
                val = merged[key]
                # BUG FIX: list(str) would corrupt recent_games → handle lists specially
                if isinstance(default_val, list):
                    if not isinstance(val, list):
                        merged[key] = []
                    else:
                        # Sanitize: keep only dicts (recent_games entries)
                        if key == "recent_games":
                            merged[key] = [e for e in val if isinstance(e, dict)]
                elif val is None:
                    merged[key] = default_val
                elif isinstance(default_val, bool) and not isinstance(val, bool):
                    # BUG FIX: bool(val) below would coerce ANY non-empty string
                    # to True — including "false", "0", "no" — since Python's
                    # bool(str) only checks emptiness, not content. A hand-edited
                    # config with a quoted boolean value (e.g. "apply_warp":
                    # "false" instead of unquoted JSON false — an easy mistake
                    # for someone used to other config formats) would silently
                    # INVERT the user's intent: they wrote "false" and got True.
                    # Recognize common true/false string spellings explicitly;
                    # fall back to the safe default for anything unrecognized
                    # rather than guessing.
                    if isinstance(val, str):
                        low = val.strip().lower()
                        if low in ("false", "0", "no", "off", ""):
                            merged[key] = False
                        elif low in ("true", "1", "yes", "on"):
                            merged[key] = True
                        else:
                            merged[key] = default_val
                    elif isinstance(val, (int, float)):
                        merged[key] = bool(val)
                    else:
                        merged[key] = default_val
                elif not isinstance(val, type(default_val)):
                    try:
                        merged[key] = type(default_val)(val)
                    except (ValueError, TypeError):
                        merged[key] = default_val

            # Жёсткие границы для числовых параметров, чтобы битый config
            # не ломал перевод или интерфейс.
            merged['batch_size'] = max(1, min(100, int(merged.get('batch_size', DEFAULTS['batch_size']))))
            merged['max_workers'] = max(1, min(10,  int(merged.get('max_workers', DEFAULTS['max_workers']))))
            merged['max_retries'] = max(1, min(10,  int(merged.get('max_retries', DEFAULTS['max_retries']))))
            merged['request_delay'] = max(0, min(60, int(merged.get('request_delay', DEFAULTS['request_delay']))))
            merged['warp_width'] = max(100, min(1200, int(merged.get('warp_width', DEFAULTS['warp_width']))))
            return merged
    except Exception:
        pass
    return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    """Атомарная запись конфига через временный файл.

    BUG FIX: ранее write_text() писал напрямую в CONFIG_PATH.
    При прерывании (диск переполнен, сбой питания) файл обнулялся или обрезался,
    и пользователь терял все настройки. Теперь пишем во временный файл рядом
    и переименовываем атомарно.
    """

    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(cfg, ensure_ascii=False, indent=2)
        # Пишем во временный файл в той же директории (для атомарного rename)
        fd, tmp_path = tempfile.mkstemp(
            dir=CONFIG_PATH.parent,
            prefix=".rpgmtl_cfg_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            # Атомарная замена (на Windows заменяет тоже атомарно с Python 3.3+)
            Path(tmp_path).replace(CONFIG_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception:
        pass
