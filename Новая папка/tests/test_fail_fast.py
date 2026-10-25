"""
Fail-fast на ПОСТОЯННЫХ ошибках API (неверный ключ, доступ запрещён, модель не найдена /
закрыта, закончились средства) и — не менее важно — НЕ fail-fast на временных.

Замер до исправления: 401 / 404 на 12 строках → 60 секунд и 60 запросов (каждый батч
повторялся, делился пополам и снова повторялся) и только потом «переведено 0/12».
После: 1 запрос, 0.0 секунды, понятная причина.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
import cli
from ai_translator import _classify_permanent_error
from conftest import make_mini_project
from engine.state import SOURCE_MANUAL, STATE_FILE, TranslationState
from pipeline import Pipeline


class ApiError(Exception):
    def __init__(self, message, status_code=None, code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


# ───────────────────────── классификатор ─────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (ApiError("401 invalid x-api-key", 401), "API-ключ"),
    (ApiError("Incorrect API key provided", 401), "API-ключ"),
    (ApiError("API key not valid. Please pass a valid API key.", code=400), "API-ключ"),     # google: .code
    (ApiError("API_KEY_INVALID", 400), "API-ключ"),
    (ApiError("permission denied", 403), "доступ запрещён"),
    (ApiError("404 model not found", 404), "закрыта провайдером"),
    (ApiError("models/gemini-1.5-pro is not found for API version v1beta", code=404), "закрыта провайдером"),
    (ApiError("The model `llama-3.1-70b-versatile` has been decommissioned", 400), "закрыта провайдером"),
    (ApiError("model_not_found", 400), "закрыта провайдером"),
    (ApiError("Your credit balance is too low to access the Anthropic API", 400), "средств"),
    (ApiError("You exceeded your current quota, insufficient_quota", 429), "средств"),
])
def test_permanent_errors_are_recognised(exc, expected):
    reason = _classify_permanent_error(exc)
    assert reason and expected in reason


@pytest.mark.parametrize("exc", [
    ApiError("Rate limit reached for requests", 429),                 # обычный rate limit — ждём и повторяем
    ApiError("Resource exhausted: per-minute quota", 429),
    ApiError("Internal server error", 500),
    ApiError("Overloaded", 529),
    ApiError("Service unavailable", 503),
    ApiError("max_tokens: 99999 > 8192, the maximum allowed", 400),   # общий 400 про конкретный запрос
    ApiError("Invalid request: messages must alternate", 400),
    ApiError("content filtered", 400),
    TimeoutError("timed out"),                                        # нет HTTP-статуса — временная
    ConnectionError("connection reset"),
    ValueError("Получено 2 переводов, ожидалось 3 — повтор"),
    RuntimeError("401"),                                              # статус только в тексте — не доверяем
], ids=lambda e: f"{type(e).__name__}:{str(e)[:30]}")
def test_transient_or_ambiguous_errors_are_not_treated_as_permanent(exc):
    assert _classify_permanent_error(exc) is None


def test_boolean_code_attribute_is_not_mistaken_for_a_status():
    class Weird(Exception):
        code = True
    assert _classify_permanent_error(Weird("api key not valid")) is None


# ───────────────────────── Translate ─────────────────────────

def _run(tmp_path, monkeypatch, provider, entries, **kw):
    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: provider)
    game, out, ext = make_mini_project(tmp_path, entries)
    kw.setdefault("batch_size", 3)
    kw.setdefault("request_delay", 0)
    p = Pipeline(str(game), str(out), log_callback=lambda m: None)
    started = time.time()
    ok, msg = p.translate(provider="openai", api_key="k", use_global_tm=False, **kw)
    return p, ok, msg, time.time() - started, out


class Always:
    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def translate_batch(self, texts, log_cb=None):
        self.calls += 1
        raise self.exc


def test_invalid_key_stops_after_one_request_instead_of_a_minute_of_retries(tmp_path, monkeypatch):
    prov = Always(ApiError("401 invalid x-api-key", 401))
    p, ok, msg, elapsed, out = _run(tmp_path, monkeypatch, prov, [(f"文{i}", "") for i in range(12)],
                                    max_workers=1, max_retries=3)

    assert ok is False and prov.calls == 1                       # до исправления: 60 запросов
    assert elapsed < 1.5                                         # до исправления: 60 секунд
    assert "⛔ Остановлено" in msg and "неверный или отозванный API-ключ" in msg
    assert "повторите" in msg and "из кэша" in msg
    assert p.last_translate_summary["fatal_error"].startswith("неверный или отозванный API-ключ")
    assert p.last_translate_summary["translated"] == 0


def test_closed_model_404_is_explained_as_such(tmp_path, monkeypatch):
    prov = Always(ApiError("404 model: claude-3-5-haiku-latest not found", 404))
    p, ok, msg, elapsed, out = _run(tmp_path, monkeypatch, prov, [(f"文{i}", "") for i in range(6)],
                                    max_workers=1, max_retries=3)
    assert ok is False and prov.calls == 1 and "закрыта провайдером" in msg


def test_parallel_mode_stops_too(tmp_path, monkeypatch):
    prov = Always(ApiError("401 invalid x-api-key", 401))
    p, ok, msg, elapsed, out = _run(tmp_path, monkeypatch, prov, [(f"文{i}", "") for i in range(30)],
                                    max_workers=3, max_retries=3, batch_size=2)
    assert ok is False and "⛔ Остановлено" in msg
    assert prov.calls <= 3                                       # максимум то, что уже было в полёте (15 батчей по 2)
    assert elapsed < 2.0


def test_key_revoked_mid_run_keeps_what_was_already_translated(tmp_path, monkeypatch):
    class Revoked:
        calls = 0

        def translate_batch(self, texts, log_cb=None):
            Revoked.calls += 1
            if Revoked.calls >= 2:
                raise ApiError("401 key revoked", 401)
            return [f"RU:{t}" for t in texts]

    entries = [(f"文{i}", "") for i in range(9)]
    p, ok, msg, elapsed, out = _run(tmp_path, monkeypatch, Revoked(), entries, max_workers=1, max_retries=3)

    assert ok is False and Revoked.calls == 2                     # третий и далее батчи не запрашивались
    cache = TranslationState(out / STATE_FILE)
    assert [cache.get(f"文{i}") for i in range(3)] == [f"RU:文{i}" for i in range(3)]
    assert not any(cache.has(f"文{i}") for i in range(3, 9))
    assert p.last_translate_summary["translated"] == 3


def test_manual_edits_survive_a_fatal_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Always(ApiError("403 forbidden", 403)))
    game, out, ext = make_mini_project(tmp_path, [("手動", "моя правка"), ("文1", ""), ("文2", "")])
    st = TranslationState(out / STATE_FILE)
    st.set("手動", "моя правка", source=SOURCE_MANUAL)
    st.save()

    ok, msg = Pipeline(str(game), str(out), log_callback=lambda m: None).translate(
        provider="openai", api_key="k", request_delay=0, use_global_tm=False, max_workers=1)

    assert ok is False and "доступ запрещён" in msg
    after = TranslationState(out / STATE_FILE)
    assert after.get("手動") == "моя правка" and after.protected_originals() == {"手動"}


def test_next_run_after_fixing_the_key_is_not_poisoned_by_the_previous_failure(tmp_path, monkeypatch):
    game, out, ext = make_mini_project(tmp_path, [(f"文{i}", "") for i in range(4)])
    pipe = Pipeline(str(game), str(out), log_callback=lambda m: None)

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Always(ApiError("401", 401)))
    ok, _ = pipe.translate(provider="openai", api_key="bad", request_delay=0, use_global_tm=False, max_workers=1)
    assert ok is False

    class Good:
        def translate_batch(self, texts, log_cb=None):
            return [f"RU:{t}" for t in texts]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Good())
    ok, msg = pipe.translate(provider="openai", api_key="good", request_delay=0, use_global_tm=False, max_workers=1)
    assert ok is True
    assert pipe.last_translate_summary == {"unique": 4, "translated": 4, "failed": 0, "remaining": 0,
                                           "cancelled": False, "fatal_error": ""}


# ───────────────────────── временные ошибки по-прежнему повторяются ─────────────────────────

def test_rate_limit_is_still_retried_and_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_translator.time, "sleep", lambda s: None)

    class Limited:
        calls = 0

        def translate_batch(self, texts, log_cb=None):
            Limited.calls += 1
            if Limited.calls == 1:
                raise ApiError("429 Rate limit reached, try again", 429)
            return [f"RU:{t}" for t in texts]

    p, ok, msg, elapsed, out = _run(tmp_path, monkeypatch, Limited(), [("文0", ""), ("文1", "")],
                                    max_workers=1, max_retries=3)
    assert ok is True and Limited.calls == 2 and p.last_translate_summary["translated"] == 2


def test_generic_400_is_retried_not_treated_as_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_translator.time, "sleep", lambda s: None)
    prov = Always(ApiError("max_tokens: 99999 > 8192, the maximum allowed", 400))
    p, ok, msg, elapsed, out = _run(tmp_path, monkeypatch, prov, [("文0", "")],
                                    max_workers=1, max_retries=3)
    assert prov.calls == 3                                         # все положенные попытки
    assert "⛔" not in msg and p.last_translate_summary["fatal_error"] == ""


def test_server_error_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_translator.time, "sleep", lambda s: None)
    prov = Always(ApiError("Internal server error", 500))
    p, ok, msg, elapsed, out = _run(tmp_path, monkeypatch, prov, [("文0", "")], max_workers=1, max_retries=2)
    assert prov.calls >= 2 and "⛔" not in msg


# ───────────────────────── Polish ─────────────────────────

def test_polish_stops_on_a_permanent_error_and_leaves_drafts_untouched(tmp_path, monkeypatch):
    class Dead:
        calls = 0

        def polish_batch(self, pairs, log_cb=None):
            Dead.calls += 1
            raise ApiError("401 invalid x-api-key", 401)

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Dead())
    game, out, ext = make_mini_project(tmp_path, [(f"文{i}", f"черновик {i}") for i in range(8)])
    st = TranslationState(out / STATE_FILE)
    for i in range(8):
        st.set(f"文{i}", f"черновик {i}")
    st.save()

    started = time.time()
    ok, msg = Pipeline(str(game), str(out), log_callback=lambda m: None).polish(
        provider="openai", api_key="k", batch_size=2, request_delay=0, max_retries=3)

    assert ok is False and Dead.calls == 1 and time.time() - started < 1.5
    assert "⛔ Остановлено" in msg and "API-ключ" in msg
    import json
    data = json.loads((ext / "Actors_translated.json").read_text(encoding="utf-8"))
    assert [e["translated"] for e in data] == [f"черновик {i}" for i in range(8)]     # перевод не стёрт


# ───────────────────────── CLI ─────────────────────────

def test_cli_reports_the_reason_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: Always(ApiError("401 invalid x-api-key", 401)))
    game, out, ext = make_mini_project(tmp_path, [("文0", ""), ("文1", "")])
    monkeypatch.setattr(sys, "argv", ["cli.py", "translate", "--game", str(game), "--output", str(out),
                                      "--provider", "openai", "--api-key", "sk-bad", "--max-workers", "1"])

    rc = cli.main()

    assert rc == cli.EXIT_FAIL
    printed = capsys.readouterr().out
    assert "⛔ Остановлено" in printed and "неверный или отозванный API-ключ" in printed
