"""
P0.3 — QA не должен ругаться на ПРАВИЛЬНЫЕ переводы и не должен пропускать
сломанные, для каждого движка.

Таблицы ниже — реальные для ja→ru конструкции: цветовые/именные/переменные
коды, паузы, плейсхолдеры, теги, интерполяция. Найдено эмпирически: шаблон
технических токенов для VX Ace «съедал» соседний текст и давал ложный critical
на любой строке с кодом рядом с японским текстом.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
from ai_translator import _extract_engine_tokens, _validate_engine_tokens
from conftest import make_mini_project
from engine.state import STATE_FILE, TranslationState
from pipeline import Pipeline
from qa_report import check_entry

# (движок, оригинал, ПРАВИЛЬНЫЙ перевод)
VALID = [
    *[(e, o, t) for e in ("rpgmv", "rpgmz", "vxace") for o, t in [
        ("\\C[2]勇者\\C[0]が来た", "\\C[2]Герой\\C[0] пришёл"),
        ("\\N[1]、\\V[5]個のアイテムを手に入れた！", "\\N[1], вы получили предметов: \\V[5]!"),
        ("\\I[12]ポーション", "\\I[12]Зелье"),
        ("ここで待て\\!", "Жди здесь\\!"),
        ("\\{大声\\}で叫ぶ", "Кричит \\{громко\\}"),
        ("所持金：\\G", "Деньги: \\G"),
        ("\\.\\.\\.そうか。\\|\\^", "\\.\\.\\.Понятно.\\|\\^"),
        ("魔王「覚悟しろ！」", "Король демонов: «Готовься!»"),
        ("第3章：旅立ち", "Глава 3: Отправление"),
        ("ダメージ\\V[3]！", "Урон: \\V[3]!"),
        ("\\N[1]は\\V[3]\\Gを手に入れた", "\\N[1] получает \\V[3]\\G"),
    ]],
    ("rpgmv", "%1は%2を手に入れた！", "%1 получает %2!"),
    ("rpgmz", "%1は%2を手に入れた！", "%1 получает %2!"),
    ("rpgmv", "HPが50%回復した", "Восстановлено 50% HP"),
    ("renpy", "{i}ゆっくり{/i}話す", "Говорит {i}медленно{/i}"),
    ("renpy", "[player]さん、こんにちは", "Здравствуйте, [player]"),
    ("renpy", "{w=0.5}待って", "Подожди{w=0.5}"),
    ("renpy", "{color=#f00}警告{/color}", "{color=#f00}Предупреждение{/color}"),
    ("renpy", "名前：%(name)s", "Имя: %(name)s"),
    ("renpy", "「えっ？」", "«Что?»"),
    ("tyrano", "ここで[r]改行", "Здесь[r]перенос"),
    ("tyrano", "待って[l][r]次へ[p]", "Подожди[l][r]дальше[p]"),
    ("kirikiri", "[font color=0xff0000]警告[resetfont]", "[font color=0xff0000]Внимание[resetfont]"),
    ("unity", "<color=#ff0000>危険</color>な場所", "Опасное <color=#ff0000>место</color>"),
    ("unity", "{0}個のコイン", "Монет: {0}"),
    ("godot", "{name}は{count}個持っている", "У {name} есть {count}"),
    ("wolf", "\\E\\cself[0]の攻撃", "Атака \\E\\cself[0]"),
]

# (движок, оригинал, СЛОМАННЫЙ перевод) — должен вызвать critical
BROKEN = [
    *[(e, "\\C[2]勇者\\C[0]が来た", "Герой пришёл") for e in ("rpgmv", "rpgmz", "vxace")],
    *[(e, "\\N[1]は逃げた", "\\N[2] сбежал") for e in ("rpgmv", "rpgmz", "vxace")],
    *[(e, "ここで待て\\!", "Жди здесь") for e in ("rpgmv", "rpgmz", "vxace")],
    *[(e, "\\I[12]ポーション", "Зелье") for e in ("rpgmv", "rpgmz", "vxace")],
    ("vxace", "\\N[1]は\\V[3]\\Gを手に入れた", "\\N[1] получает \\V[3]"),     # потерян \G
    ("rpgmv", "%1は%2を手に入れた！", "Предмет получен!"),
    ("renpy", "{i}ゆっくり{/i}話す", "Говорит медленно"),
    ("renpy", "[player]さん", "Игрок"),
    ("tyrano", "ここで[r]改行", "Здесь перенос"),
    ("kirikiri", "[font color=0xff0000]警告[resetfont]", "Внимание"),
    ("unity", "{0}個のコイン", "Монет"),
    ("unity", "<color=#ff0000>危険</color>", "Опасно"),
    ("godot", "{name}は{count}個持っている", "У него есть"),
]


def _issues(engine, original, translated):
    return check_entry({"original": original, "translated": translated, "game_type": engine})


@pytest.mark.parametrize("engine,original,translated", VALID)
def test_correct_translation_is_not_flagged_critical(engine, original, translated):
    critical = [i for i in _issues(engine, original, translated) if i["severity"] == "critical"]
    assert critical == [], f"ложный critical для {engine}: {original!r} → {translated!r}: {critical}"


@pytest.mark.parametrize("engine,original,translated", VALID)
def test_correct_translation_is_accepted_by_the_translate_validator(engine, original, translated):
    assert _validate_engine_tokens(original, translated, engine)


@pytest.mark.parametrize("engine,original,translated", BROKEN)
def test_broken_translation_is_caught(engine, original, translated):
    critical = [i for i in _issues(engine, original, translated) if i["severity"] == "critical"]
    assert critical, f"не поймана поломка для {engine}: {original!r} → {translated!r}"
    assert not _validate_engine_tokens(original, translated, engine)


def test_numerals_spelled_out_are_only_a_warning_never_blocking():
    issues = _issues("rpgmv", "2人の勇者と3匹のスライム", "Два героя и три слизня")
    assert issues and all(i["severity"] == "warning" for i in issues)


# ───────────────────────── VX Ace: регрессия на «жадный» шаблон ─────────────────────────

def test_vxace_tokens_are_codes_not_the_text_around_them():
    ja = "\\C[2]勇者\\C[0]が来た"                    # в японском нет пробелов
    ru = "\\C[2]Герой\\C[0] пришёл"
    assert _extract_engine_tokens(ja, "vxace") == ["\\C[2]", "\\C[0]"]
    assert _extract_engine_tokens(ja, "vxace") == _extract_engine_tokens(ru, "vxace")


@pytest.mark.parametrize("text,expected", [
    ("\\N[1]は\\V[3]\\Gを手に入れた", ["\\N[1]", "\\V[3]", "\\G"]),
    ("\\{大声\\}\\.\\|\\!\\^", ["\\{", "\\}", "\\.", "\\|", "\\!", "\\^"]),
    ("\\FS[22]特大", ["\\FS[22]"]),
    ("\\GHero", ["\\G"]),                                  # одиночная буква — код, дальше обычный текст
    ("パス\\\\ファイル", ["\\\\"]),                          # экранированный обратный слэш
])
def test_vxace_token_extraction(text, expected):
    assert _extract_engine_tokens(text, "vxace") == expected


def test_translate_accepts_vxace_lines_with_codes_next_to_japanese_text(tmp_path, monkeypatch):
    """Интеграция: настоящий Pipeline.translate для игры типа vxace. Раньше такие строки
    отвергались как «потерявшие коды» и оставались непереведёнными."""
    class Provider:
        def translate_batch(self, texts, log_cb=None):
            return ["\\C[2]Герой\\C[0] пришёл" if "勇者" in t else "\\N[1] получает \\V[3]\\G" for t in texts]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Provider())
    originals = ["\\C[2]勇者\\C[0]が来た", "\\N[1]は\\V[3]\\Gを手に入れた"]
    game, out, ext = make_mini_project(tmp_path, [(o, "") for o in originals], game_type="vxace")

    ok, msg = Pipeline(str(game), str(out), log_callback=lambda m: None).translate(
        provider="openai", api_key="k", request_delay=0, max_retries=1, use_global_tm=False)

    assert ok is True and "НЕ УДАЛОСЬ" not in msg
    state = TranslationState(out / STATE_FILE)
    assert state.get(originals[0]) == "\\C[2]Герой\\C[0] пришёл"
    assert state.get(originals[1]) == "\\N[1] получает \\V[3]\\G"
