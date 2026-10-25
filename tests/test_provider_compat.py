"""
Совместимость с настоящими API. Факты проверены по официальным страницам 2026-10-22
(см. комментарии у DEFAULT_MODELS / MODEL_LIFECYCLE в ai_translator.py):

  * Claude 5.5 отвергает любой temperature, кроме значения по умолчанию, ошибкой 400,
    а проект ВСЕГДА слал temperature — провайдер Claude с актуальными моделями не работал;
  * дефолтные модели Gemini / Groq / DeepSeek / Claude были закрыты провайдерами;
  * OpenAI снимает gpt-4o / gpt-4o-mini 2027-01-14.
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
from ai_translator import (
    DEFAULT_MODELS, MODEL_LIFECYCLE, QUALITY_MODES, ClaudeProvider, DeepSeekProvider,
    GroqProvider, OpenAIProvider, TranslatorConfig, _create_with_temperature_fallback,
    _is_unsupported_temperature_error, _model_rejects_sampling, model_lifecycle_notice,
)
from conftest import make_mini_project
from pipeline import Pipeline

VERIFIED_ON = date(2026, 10, 22)


class ApiError(Exception):
    """Похож на исключения SDK (anthropic/openai): есть status_code."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def _claude_reply(text="[1] Привет"):
    block = MagicMock()
    block.text = text
    reply = MagicMock()
    reply.content = [block]
    return reply


def _openai_reply(text="[1] Привет"):
    reply = MagicMock()
    reply.choices = [MagicMock()]
    reply.choices[0].message.content = text
    return reply


# ───────────────────────── какие модели принимают temperature ─────────────────────────

@pytest.mark.parametrize("model", [
    "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1",
    "claude-opus-5", "claude-sonnet-5", "claude-mythos-5-1", "CLAUDE-HAIKU-5-5",
])
def test_models_known_to_reject_sampling(model):
    assert _model_rejects_sampling(model)


@pytest.mark.parametrize("model", [
    "claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8",
    "claude-3-5-haiku-latest", "gpt-4o-mini", "gemini-3.6-flash", "openai/gpt-oss-20b",
    "deepseek-v4-flash", "", "mock",
])
def test_other_models_are_not_assumed_to_reject_sampling(model):
    assert not _model_rejects_sampling(model)


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"])
def test_claude_5x_requests_never_carry_temperature(model):
    cfg = TranslatorConfig(provider="claude", api_key="k", model=model, temperature=0.2)
    p = ClaudeProvider(cfg)
    p._client = MagicMock()
    p._client.messages.create.return_value = _claude_reply()

    assert p.translate_batch(["Hello"]) == ["Привет"]
    p._client.messages.create.return_value = _claude_reply("[1] Здравствуйте")
    p.polish_batch([("Hello", "Привет")])

    for call in p._client.messages.create.call_args_list:
        assert "temperature" not in call.kwargs, call.kwargs     # 400 на настоящем API
        assert call.kwargs["model"] == model and call.kwargs["max_tokens"] == cfg.max_tokens


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-3-5-haiku-latest"])
def test_older_claude_models_still_receive_temperature(model):
    cfg = TranslatorConfig(provider="claude", api_key="k", model=model, temperature=0.2)
    p = ClaudeProvider(cfg)
    p._client = MagicMock()
    p._client.messages.create.return_value = _claude_reply()
    p.translate_batch(["Hello"])
    assert p._client.messages.create.call_args.kwargs["temperature"] == 0.2


# ───────────────────────── откат при HTTP 400 про temperature ─────────────────────────

def _provider(cls, provider_name, model):
    cfg = TranslatorConfig(provider=provider_name, api_key="k", model=model, temperature=0.3)
    return cls(cfg)


CASES = [
    (ClaudeProvider,   "claude",   "claude-future-9",   lambda c: c.messages.create,           _claude_reply),
    (OpenAIProvider,   "openai",   "some-reasoning-1",  lambda c: c.chat.completions.create,   _openai_reply),
    (GroqProvider,     "groq",     "openai/gpt-oss-20b", lambda c: c.chat.completions.create,  _openai_reply),
    (DeepSeekProvider, "deepseek", "deepseek-v4-flash", lambda c: c.chat.completions.create,   _openai_reply),
]


def _wire(provider, getter, reply, rejector):
    client = MagicMock()
    provider._client = client
    getter(client).side_effect = rejector(reply)
    return getter(client)


@pytest.mark.parametrize("cls,name,model,getter,reply", CASES)
def test_400_about_temperature_is_retried_without_it_and_remembered(cls, name, model, getter, reply):
    p = _provider(cls, name, model)

    def rejector(make_reply):
        def create(**kw):
            if "temperature" in kw:
                raise ApiError("Unsupported value: 'temperature' does not support 0.3 with this model.", 400)
            return make_reply()
        return create

    create = _wire(p, getter, reply, rejector)

    assert p.translate_batch(["Hello"]) == ["Привет"]
    assert [("temperature" in c.kwargs) for c in create.call_args_list] == [True, False]   # 400, затем без параметра
    p.translate_batch(["World"])
    assert [("temperature" in c.kwargs) for c in create.call_args_list][2:] == [False]     # запомнено: лишнего 400 нет


@pytest.mark.parametrize("exc", [
    ApiError("model_not_found: no such model", 400),               # 400, но не про temperature
    ApiError("temperature must be 1 (internal error)", 500),       # про temperature, но не 400
    ApiError("temperature invalid"),                               # нет status_code
    ValueError("temperature"),                                     # вообще не ошибка API
], ids=["400-other", "500-temperature", "no-status", "not-api"])
def test_other_errors_propagate_without_a_second_attempt(exc):
    """Откат применяется ТОЛЬКО к «400 про temperature». Любая другая ошибка уходит наверх
    как есть (там её обработают ретраи/классификация) и не вызывает лишний запрос."""
    class Owner:
        pass
    calls = []

    def create(**kw):
        calls.append(kw)
        raise exc

    with pytest.raises(type(exc)) as info:
        _create_with_temperature_fallback(Owner(), create, model="m", temperature=0.2)
    assert info.value is exc
    assert len(calls) == 1 and calls[0]["temperature"] == 0.2


@pytest.mark.parametrize("cls,name,model,getter,reply", CASES)
def test_providers_surface_a_non_temperature_400_instead_of_hiding_it(cls, name, model, getter, reply):
    p = _provider(cls, name, model)
    client = MagicMock()
    p._client = client
    getter(client).side_effect = ApiError("model_not_found: no such model", 400)

    with pytest.raises(ApiError, match="model_not_found"):
        p.translate_batch(["Hello"])
    assert getter(client).call_count == 1                          # ни тихого «успеха», ни повторного запроса


def test_fallback_helper_semantics_directly():
    class Owner:
        pass
    calls = []

    def create(**kw):
        calls.append(dict(kw))
        if "temperature" in kw and kw["model"] == "strict-model":
            raise ApiError("temperature is deprecated for this model", 400)
        return "ok"

    owner = Owner()
    assert _create_with_temperature_fallback(owner, create, model="lenient", temperature=0.5, x=1) == "ok"
    assert calls[-1] == {"model": "lenient", "temperature": 0.5, "x": 1}          # лояльной модели — как есть
    assert not getattr(owner, "_omit_temperature", False)

    assert _create_with_temperature_fallback(owner, create, model="strict-model", temperature=0.5, x=1) == "ok"
    assert calls[-2]["temperature"] == 0.5 and "temperature" not in calls[-1]       # 400 → повтор без параметра
    assert owner._omit_temperature is True

    n = len(calls)
    _create_with_temperature_fallback(owner, create, model="lenient", temperature=0.5)
    assert len(calls) == n + 1 and "temperature" not in calls[-1]                   # решение запомнено на владельце


def test_unsupported_temperature_error_detection():
    assert _is_unsupported_temperature_error(ApiError("`temperature` may only be set to 1", 400))
    resp_style = ApiError("temperature unsupported")
    resp_style.response = MagicMock(status_code=400)
    assert _is_unsupported_temperature_error(resp_style)                            # status_code в response
    assert not _is_unsupported_temperature_error(ApiError("max_tokens too large", 400))
    assert not _is_unsupported_temperature_error(ApiError("temperature oops", 429))
    assert not _is_unsupported_temperature_error(ValueError("temperature"))


# ───────────────────────── модели по умолчанию ─────────────────────────

CURRENT_CLAUDE_IDS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"}


def test_default_models_cover_every_provider_and_feed_the_config():
    assert set(DEFAULT_MODELS) == {"openai", "gemini", "claude", "groq", "deepseek", "mock"}
    for provider, model in DEFAULT_MODELS.items():
        assert TranslatorConfig(provider=provider).get_model() == model
    assert TranslatorConfig(provider="claude", model="my-model").get_model() == "my-model"   # явная модель главнее


def test_no_default_points_at_a_model_the_provider_already_closed():
    """Дефолты и пресеты качества не должны указывать на закрытые модели (на дату проверки).
    Фиксированная дата — чтобы тест не «протухал» сам по себе."""
    chosen = {(p, m) for p, m in DEFAULT_MODELS.items()}
    for mode in QUALITY_MODES.values():
        chosen |= set(mode["models"].items())
    for provider, model in sorted(chosen):
        notice = model_lifecycle_notice(provider, model, today=VERIFIED_ON)
        assert notice is None or "будет закрыта" in notice, f"{provider}/{model}: {notice}"


def test_claude_defaults_are_current_model_ids_with_known_prices():
    claude_models = {DEFAULT_MODELS["claude"]} | {m["models"]["claude"] for m in QUALITY_MODES.values()}
    assert claude_models <= CURRENT_CLAUDE_IDS
    for model in CURRENT_CLAUDE_IDS:
        assert model in ai_translator._COST_PER_1K_TOKENS


def test_gui_presets_come_from_the_single_source_of_truth():
    pytest.importorskip("PyQt6")
    import rpgmtl_desktop as rd
    for provider, (model, _batch, _delay) in rd.SettingsPanel._PROVIDER_PARAMS.items():
        assert model == DEFAULT_MODELS[provider]


# ───────────────────────── жизненный цикл моделей ─────────────────────────

def test_lifecycle_notice_for_closed_closing_and_gone_models():
    closed = model_lifecycle_notice("gemini", "gemini-2.0-flash", today=VERIFIED_ON)
    assert "закрыта провайдером с 2026-06-01" in closed and "gemini-3.6-flash" in closed

    closing = model_lifecycle_notice("openai", "gpt-4o-mini", today=VERIFIED_ON)
    assert "будет закрыта" in closing and "2027-01-14" in closing and "84 дн." in closing

    on_the_day = model_lifecycle_notice("openai", "gpt-4o-mini", today=date(2027, 1, 14))
    assert "закрыта провайдером с 2027-01-14" in on_the_day

    gone = model_lifecycle_notice("claude", "claude-3-5-haiku-latest", today=VERIFIED_ON)
    assert "больше не значится" in gone and "claude-haiku-5-5" in gone

    assert model_lifecycle_notice("claude", "claude-haiku-5-5", today=VERIFIED_ON) is None
    assert model_lifecycle_notice("GEMINI", "gemini-2.0-flash", today=VERIFIED_ON)         # регистр провайдера неважен
    assert model_lifecycle_notice("openai", "", today=VERIFIED_ON) is None


def test_every_lifecycle_entry_has_a_replacement_note_and_valid_date():
    for (provider, model), (shutdown, note) in MODEL_LIFECYCLE.items():
        assert provider in DEFAULT_MODELS and model and note
        if shutdown is not None:
            date.fromisoformat(shutdown)


def _translate_with_fake(tmp_path, monkeypatch, provider, model=""):
    class Fake:
        def translate_batch(self, texts, log_cb=None):
            return [f"RU:{t}" for t in texts]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Fake())
    game, out, ext = make_mini_project(tmp_path, [("文0", "")])
    logs: list[str] = []
    ok, msg = Pipeline(str(game), str(out), log_callback=logs.append).translate(
        provider=provider, api_key="k", model=model, request_delay=0, use_global_tm=False)
    return ok, logs


def test_pipeline_warns_about_a_dead_model_but_does_not_block(tmp_path, monkeypatch):
    ok, logs = _translate_with_fake(tmp_path, monkeypatch, "claude", "claude-3-5-haiku-latest")
    assert ok is True                                                                 # запрос не блокируется
    assert any("больше не значится" in m for m in logs)


def test_pipeline_shows_which_default_model_is_used(tmp_path, monkeypatch):
    ok, logs = _translate_with_fake(tmp_path, monkeypatch, "claude")
    assert any("авто → claude-haiku-5-5" in m for m in logs)
    assert not any("⚠ Модель" in m for m in logs)                                      # у дефолта предупреждений нет
