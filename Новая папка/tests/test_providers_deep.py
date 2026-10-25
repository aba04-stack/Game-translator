"""
test_providers_deep.py — глубокая проверка translate_batch/polish_batch для
всех 5 AI-провайдеров через мок реального клиента, а не только структурные
проверки (hasattr/inspect.signature).

Раньше только OpenAIProvider.polish_batch был проверен через мок реального
клиента (test_polish.py::test_openai_provider_polish_batch_wires_prompt_and_restores_codes).
Остальные 4 провайдера (Gemini — оба SDK, Claude, Groq, DeepSeek) были
покрыты только структурными тестами (инициализация клиента, наличие метода),
а сам путь prompt → API-ответ → _parse_numbered → _restore_codes — тот
самый код, который отвечает за сохранность escape-кодов и текста при
РЕАЛЬНОМ переводе — никогда не исполнялся тестами. Учитывая, что именно в
этом пайплайне (build prompt / parse response / restore codes) уже был
найден не один баг в этой сессии, а провайдер выбирается пользователем в
настройках произвольно, каждый из 5 заслуживает одинаковой проверки.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Gemini — новый SDK (google-genai)
# ─────────────────────────────────────────────────────────────────────────────

def test_gemini_new_sdk_translate_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("google.genai")
    from ai_translator import GeminiProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="gemini", api_key="fake-key", target_lang="Russian")
    provider = GeminiProvider(cfg)
    if not provider._use_new_sdk:
        pytest.skip("new Gemini SDK not active in this environment")

    fake_response = MagicMock()
    fake_response.text = "[1] §0§Привет§1§"
    provider._client = MagicMock()
    provider._client.models.generate_content.return_value = fake_response

    result = provider.translate_batch([r"\C[1]Hi\C[0]"])
    assert result == [r"\C[1]Привет\C[0]"], f"got {result!r}"

    _, kwargs = provider._client.models.generate_content.call_args
    assert kwargs["model"] == cfg.get_model()
    assert "Hi" not in kwargs["contents"] or "§" in kwargs["contents"]  # protected, not raw


def test_gemini_new_sdk_translate_batch_handles_blocked_response_gracefully():
    """
    resp.text может кинуть ValueError/AttributeError, если ответ заблокирован
    safety-фильтром Gemini или пуст — должно откатиться на "" и корректно
    завершиться (все строки как success=False дальше по пайплайну), а не
    уронить весь батч необработанным исключением.
    """
    pytest.importorskip("google.genai")
    from ai_translator import GeminiProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="gemini", api_key="fake-key")
    provider = GeminiProvider(cfg)
    if not provider._use_new_sdk:
        pytest.skip("new Gemini SDK not active in this environment")

    fake_response = MagicMock()
    type(fake_response).text = property(lambda self: (_ for _ in ()).throw(ValueError("blocked")))
    provider._client = MagicMock()
    provider._client.models.generate_content.return_value = fake_response

    result = provider.translate_batch(["こんにちは"])
    assert result == [""], f"got {result!r}"


def test_gemini_new_sdk_polish_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("google.genai")
    from ai_translator import GeminiProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="gemini", api_key="fake-key", target_lang="Russian")
    provider = GeminiProvider(cfg)
    if not provider._use_new_sdk:
        pytest.skip("new Gemini SDK not active in this environment")

    fake_response = MagicMock()
    fake_response.text = "[1] §0§Улучшенный привет§1§"
    provider._client = MagicMock()
    provider._client.models.generate_content.return_value = fake_response

    result = provider.polish_batch([(r"\C[1]Hi\C[0]", r"\C[1]Привет\C[0]")])
    assert result == [r"\C[1]Улучшенный привет\C[0]"], f"got {result!r}"

    _, kwargs = provider._client.models.generate_content.call_args
    config_arg = kwargs["config"]
    assert "editor" in config_arg.system_instruction.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Gemini — устаревший SDK (google-generativeai)
# ─────────────────────────────────────────────────────────────────────────────

def test_gemini_legacy_sdk_translate_batch_wires_prompt_and_restores_codes(monkeypatch):
    pytest.importorskip("google.generativeai")
    from ai_translator import GeminiProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="gemini", api_key="fake-key", target_lang="Russian")
    provider = GeminiProvider(cfg)
    # Принудительно переключаем на legacy-путь независимо от того, что реально
    # установлено в этом окружении — именно этот путь и хотим проверить.
    provider._use_new_sdk = False

    fake_model = MagicMock()
    fake_response = MagicMock()
    fake_response.text = "[1] §0§Привет§1§"
    fake_model.generate_content.return_value = fake_response

    fake_genai_legacy = MagicMock()
    fake_genai_legacy.GenerativeModel.return_value = fake_model
    fake_genai_legacy.GenerationConfig = MagicMock(side_effect=lambda **kw: kw)
    provider._genai_legacy = fake_genai_legacy

    result = provider.translate_batch([r"\C[1]Hi\C[0]"])
    assert result == [r"\C[1]Привет\C[0]"], f"got {result!r}"

    _, kwargs = fake_genai_legacy.GenerativeModel.call_args
    assert kwargs["model_name"] == cfg.get_model()
    prompt_arg = fake_model.generate_content.call_args[0][0]
    assert "§0§" in prompt_arg and "§1§" in prompt_arg


def test_gemini_legacy_sdk_translate_batch_handles_exception_gracefully():
    """generate_content может кинуть любое исключение (сеть, квота) —
    должно откатиться на "" для этого батча, а не уронить процесс."""
    pytest.importorskip("google.generativeai")
    from ai_translator import GeminiProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="gemini", api_key="fake-key")
    provider = GeminiProvider(cfg)
    provider._use_new_sdk = False

    fake_model = MagicMock()
    fake_model.generate_content.side_effect = RuntimeError("network error")
    fake_genai_legacy = MagicMock()
    fake_genai_legacy.GenerativeModel.return_value = fake_model
    fake_genai_legacy.GenerationConfig = MagicMock(side_effect=lambda **kw: kw)
    provider._genai_legacy = fake_genai_legacy

    result = provider.translate_batch(["こんにちは"])
    assert result == [""], f"got {result!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Claude
# ─────────────────────────────────────────────────────────────────────────────

def test_claude_translate_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("anthropic")
    from ai_translator import ClaudeProvider, TranslatorConfig

    # Модель, которая ПРИНИМАЕТ параметры сэмплирования (claude-haiku-4-5): температура
    # обязана доходить до API (BUG FIX #existing: раньше её забывали). Для дефолтной
    # claude-haiku-5-5 всё наоборот — она отвергает temperature (см. test_provider_compat.py).
    cfg = TranslatorConfig(provider="claude", api_key="fake-key", target_lang="Russian",
                           model="claude-haiku-4-5")
    provider = ClaudeProvider(cfg)

    text_block = MagicMock()
    text_block.text = "[1] §0§Привет§1§"
    fake_response = MagicMock()
    fake_response.content = [text_block]
    provider._client = MagicMock()
    provider._client.messages.create.return_value = fake_response

    result = provider.translate_batch([r"\C[1]Hi\C[0]"])
    assert result == [r"\C[1]Привет\C[0]"], f"got {result!r}"

    _, kwargs = provider._client.messages.create.call_args
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["temperature"] == cfg.temperature


def test_claude_translate_batch_skips_non_text_blocks():
    """
    BUG FIX (уже существующий, проверяем что не регрессирует): content[0].text
    кидает AttributeError если первый блок — не текстовый (например
    tool_use). Должны пройти по всем блокам и найти первый текстовый.
    """
    pytest.importorskip("anthropic")
    from ai_translator import ClaudeProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="claude", api_key="fake-key")
    provider = ClaudeProvider(cfg)

    non_text_block = MagicMock(spec=[])  # намеренно без атрибута .text вовсе
    text_block = MagicMock()
    text_block.text = "[1] Привет"
    fake_response = MagicMock()
    fake_response.content = [non_text_block, text_block]
    provider._client = MagicMock()
    provider._client.messages.create.return_value = fake_response

    result = provider.translate_batch(["こんにちは"])
    assert result == ["Привет"], f"БАГ (регрессия): {result!r}"


def test_claude_translate_batch_all_blocks_non_text_falls_back_to_empty():
    pytest.importorskip("anthropic")
    from ai_translator import ClaudeProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="claude", api_key="fake-key")
    provider = ClaudeProvider(cfg)

    non_text_block = MagicMock(spec=[])
    fake_response = MagicMock()
    fake_response.content = [non_text_block]
    provider._client = MagicMock()
    provider._client.messages.create.return_value = fake_response

    result = provider.translate_batch(["こんにちは"])
    assert result == [""], f"got {result!r}"


def test_claude_polish_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("anthropic")
    from ai_translator import ClaudeProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="claude", api_key="fake-key", target_lang="Russian")
    provider = ClaudeProvider(cfg)

    text_block = MagicMock()
    text_block.text = "[1] §0§Улучшенный привет§1§"
    fake_response = MagicMock()
    fake_response.content = [text_block]
    provider._client = MagicMock()
    provider._client.messages.create.return_value = fake_response

    result = provider.polish_batch([(r"\C[1]Hi\C[0]", r"\C[1]Привет\C[0]")])
    assert result == [r"\C[1]Улучшенный привет\C[0]"], f"got {result!r}"

    _, kwargs = provider._client.messages.create.call_args
    assert "editor" in kwargs["system"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Groq
# ─────────────────────────────────────────────────────────────────────────────

def test_groq_translate_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("openai")
    from ai_translator import GroqProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="groq", api_key="fake-key", target_lang="Russian")
    provider = GroqProvider(cfg)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "[1] §0§Привет§1§"
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = fake_response

    result = provider.translate_batch([r"\C[1]Hi\C[0]"])
    assert result == [r"\C[1]Привет\C[0]"], f"got {result!r}"

    _, kwargs = provider._client.chat.completions.create.call_args
    assert kwargs["model"] == cfg.get_model()
    system_msg = next(m["content"] for m in kwargs["messages"] if m["role"] == "system")
    user_msg = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
    assert "§0§" in user_msg
    assert isinstance(system_msg, str) and system_msg


def test_groq_translate_batch_handles_empty_content_gracefully():
    """resp.choices[0].message.content может быть None (пустой ответ модели)."""
    pytest.importorskip("openai")
    from ai_translator import GroqProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="groq", api_key="fake-key")
    provider = GroqProvider(cfg)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = None
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = fake_response

    result = provider.translate_batch(["こんにちは"])
    assert result == [""], f"got {result!r}"


def test_groq_polish_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("openai")
    from ai_translator import GroqProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="groq", api_key="fake-key", target_lang="Russian")
    provider = GroqProvider(cfg)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "[1] §0§Улучшенный привет§1§"
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = fake_response

    result = provider.polish_batch([(r"\C[1]Hi\C[0]", r"\C[1]Привет\C[0]")])
    assert result == [r"\C[1]Улучшенный привет\C[0]"], f"got {result!r}"


# ─────────────────────────────────────────────────────────────────────────────
# DeepSeek
# ─────────────────────────────────────────────────────────────────────────────

def test_deepseek_translate_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("openai")
    from ai_translator import DeepSeekProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="deepseek", api_key="fake-key", target_lang="Russian")
    provider = DeepSeekProvider(cfg)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "[1] §0§Привет§1§"
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = fake_response

    result = provider.translate_batch([r"\C[1]Hi\C[0]"])
    assert result == [r"\C[1]Привет\C[0]"], f"got {result!r}"


def test_deepseek_translate_batch_handles_think_block_and_markdown():
    """
    DeepSeek-reasoner добавляет <think>...</think> блоки и иногда markdown —
    _clean_translation (вызываемый внутри _parse_numbered) должен их убрать.
    Это ключевая для DeepSeek особенность, специально упомянутая в шапке
    ai_translator.py ("DeepSeek compatibility: stripping markdown/think blocks").
    """
    pytest.importorskip("openai")
    from ai_translator import DeepSeekProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="deepseek", api_key="fake-key")
    provider = DeepSeekProvider(cfg)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = (
        "<think>сначала подумаю над переводом...</think>\n[1] **Привет**"
    )
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = fake_response

    result = provider.translate_batch(["こんにちは"])
    assert result == ["Привет"], f"БАГ: think-блок или markdown не убраны: {result!r}"


def test_deepseek_polish_batch_wires_prompt_and_restores_codes():
    pytest.importorskip("openai")
    from ai_translator import DeepSeekProvider, TranslatorConfig

    cfg = TranslatorConfig(provider="deepseek", api_key="fake-key", target_lang="Russian")
    provider = DeepSeekProvider(cfg)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "[1] §0§Улучшенный привет§1§"
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = fake_response

    result = provider.polish_batch([(r"\C[1]Hi\C[0]", r"\C[1]Привет\C[0]")])
    assert result == [r"\C[1]Улучшенный привет\C[0]"], f"got {result!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Параллельный режим (max_workers > 1) — до этой сессии НИ ОДИН тест во всём
# наборе не проверял этот путь ни разу. ThreadPoolExecutor + as_completed()
# означает, что батчи возвращаются в ПРОИЗВОЛЬНОМ порядке завершения — если
# бы сборка финального result.results была неверной, порядок original ↔
# translated мог бы перепутаться без единого явного краша.
# ─────────────────────────────────────────────────────────────────────────────

def test_translate_texts_parallel_mode_preserves_order(monkeypatch):
    import time as time_module
    import random
    import ai_translator

    call_log = []

    def fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event):
        # Случайная микро-задержка — поощряем реальное завершение батчей НЕ
        # в порядке отправки, чтобы тест реально нагружал сборку по индексам,
        # а не проходил только потому что батчи случайно завершились по порядку.
        time_module.sleep(random.uniform(0, 0.02))
        call_log.append(list(batch))
        return True, [f"ПЕРЕВОД:{t}" for t in batch]

    monkeypatch.setattr(ai_translator, "_do_translate_batch", fake_do_translate_batch)

    texts = [f"строка{i:02d}" for i in range(30)]
    cfg = ai_translator.TranslatorConfig(
        provider="openai", api_key="fake-key-for-test",
        batch_size=3, max_workers=4, request_delay=0.0, use_global_tm=False,
    )
    result = ai_translator.translate_texts(texts, cfg)

    assert result.total == 30
    assert result.succeeded == 30
    assert len(call_log) == 10, f"Ожидалось 10 батчей по 3 строки, было {len(call_log)}"

    # Самое главное: результаты должны соответствовать ИСХОДНОМУ порядку
    # texts, а не порядку завершения потоков.
    for i, text in enumerate(texts):
        r = result.results[i]
        assert r.original == text, (
            f"БАГ: результат на позиции {i} — {r.original!r}, ожидался {text!r} "
            f"(результаты параллельного режима перепутаны по порядку)"
        )
        assert r.translated == f"ПЕРЕВОД:{text}", f"БАГ: неверный перевод для {text!r}: {r.translated!r}"
        assert r.success is True


def test_translate_texts_parallel_mode_with_partial_batch_failure():
    """Один батч из нескольких падает (например временная ошибка сети) —
    остальные батчи должны обработаться нормально, а упавший — вернуться
    как success=False, без искажения порядка остальных результатов."""
    import ai_translator

    def fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event):
        if "плохая" in batch[0]:
            return False, [""] * len(batch)
        return True, [f"ok:{t}" for t in batch]

    orig = ai_translator._do_translate_batch
    ai_translator._do_translate_batch = fake_do_translate_batch
    try:
        texts = ["строка1", "строка2", "плохая_строка", "строка4", "строка5", "строка6"]
        cfg = ai_translator.TranslatorConfig(
            provider="openai", api_key="fake-key-for-test",
            batch_size=2, max_workers=3, request_delay=0.0, use_global_tm=False,
        )
        result = ai_translator.translate_texts(texts, cfg)

        assert result.total == 6
        assert result.succeeded == 4
        assert result.failed == 2
        for i, text in enumerate(texts):
            assert result.results[i].original == text, (
                f"БАГ: порядок результатов нарушен на позиции {i}"
            )
        # Упавший батч -> success=False, но остальные не задеты
        bad_idx = texts.index("плохая_строка")
        assert result.results[bad_idx].success is False
        assert result.results[0].translated == "ok:строка1"
        assert result.results[5].translated == "ok:строка6"
    finally:
        ai_translator._do_translate_batch = orig


def test_translate_texts_parallel_mode_respects_cancel_event():
    import threading
    import ai_translator

    cancel = threading.Event()
    calls = {"n": 0}

    def fake_do_translate_batch(provider, batch, cfg, log_cb, cancel_event):
        calls["n"] += 1
        if calls["n"] >= 2:
            cancel.set()  # симулируем отмену пользователем на середине работы
        return True, [f"ok:{t}" for t in batch]

    orig = ai_translator._do_translate_batch
    ai_translator._do_translate_batch = fake_do_translate_batch
    try:
        texts = [f"строка{i}" for i in range(20)]
        cfg = ai_translator.TranslatorConfig(
            provider="openai", api_key="fake-key-for-test",
            batch_size=2, max_workers=2, request_delay=0.0, use_global_tm=False,
        )
        result = ai_translator.translate_texts(texts, cfg, cancel_event=cancel)
        assert result.cancelled is True
        # Не все 20 строк должны быть обработаны — отмена должна была
        # остановить отправку новых батчей.
        assert result.succeeded < 20, "БАГ: отмена не остановила обработку оставшихся батчей"
    finally:
        ai_translator._do_translate_batch = orig
