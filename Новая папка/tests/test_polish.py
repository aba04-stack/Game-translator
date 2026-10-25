"""
test_polish.py — тесты для AI-полировки уже переведённых строк (v50).

Полировка — это отдельный AI-проход поверх уже готового перевода: НЕ
переводит с нуля, а предлагает более естественную формулировку, сохраняя
смысл, тон и escape-коды. Ближайший аналог по духу — one-click polish в
AiNiee, но встроенный в существующий пайплайн extract/translate/inject
и защищённый той же §N§-плейсхолдер системой, что и обычный перевод.

Покрывает:
- _protect_pairs_for_polish / _build_polish_prompt (общая карта плейсхолдеров
  для original и draft)
- get_polish_system_prompt (не путается с get_system_prompt)
- _do_polish_batch: retry, rate-limit backoff, half-batch fallback, cancel
- polish_texts: happy path, "уже хорошо — без изменений", пропуск пар с
  изначально битыми кодами, безопасный откат при испорченной моделью
  правке, сохранение ПОРЯДКА входных pairs в result.results, отмена,
  пустой ввод, ошибка создания провайдера
- Все 5 провайдеров имеют polish_batch с ожидаемой сигнатурой;
  OpenAIProvider.polish_batch протестирован через мок реального клиента
- Pipeline.polish(): полный прогон, точечная полировка (texts=[...]),
  отсутствие переводов для полировки
- cli.py polish: полный проход через argparse + main()
"""
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# _protect_pairs_for_polish / _build_polish_prompt
# ─────────────────────────────────────────────────────────────────────────────

def test_protect_pairs_shares_one_placeholder_map():
    from ai_translator import _protect_pairs_for_polish

    pairs = [(r"\C[2]Yuusha\C[0]の運命", r"\C[2]Судьба героя\C[0]")]
    protected, placeholders = _protect_pairs_for_polish(pairs)
    orig_p, draft_p = protected[0]
    assert "§0§" in orig_p and "§1§" in orig_p
    assert "§0§" in draft_p and "§1§" in draft_p
    assert r"\C[2]" not in orig_p and r"\C[2]" not in draft_p
    assert placeholders[0]["§0§"] == r"\C[2]"
    assert placeholders[0]["§1§"] == r"\C[0]"


def test_protect_pairs_handles_pair_with_no_codes():
    from ai_translator import _protect_pairs_for_polish
    protected, placeholders = _protect_pairs_for_polish([("こんにちは", "Привет")])
    assert protected == [("こんにちは", "Привет")]
    assert placeholders == [{}]


def test_build_polish_prompt_includes_source_and_draft_labels():
    from ai_translator import _protect_pairs_for_polish, _build_polish_prompt

    pairs = [("こんにちは", "Привет"), ("さようなら", "Пока")]
    protected, _ = _protect_pairs_for_polish(pairs)
    prompt = _build_polish_prompt(pairs, protected, "Russian")
    assert "SOURCE:" in prompt and "DRAFT:" in prompt
    assert "[1]" in prompt and "[2]" in prompt
    assert "UNCHANGED" in prompt  # инструкция не трогать уже хороший черновик
    assert "こんにちは" in prompt and "Привет" in prompt


def test_build_polish_prompt_adds_placeholder_reminder_only_when_present():
    from ai_translator import _protect_pairs_for_polish, _build_polish_prompt

    with_codes = [(r"\C[1]Hi\C[0]", r"\C[1]Привет\C[0]")]
    protected, _ = _protect_pairs_for_polish(with_codes)
    prompt = _build_polish_prompt(with_codes, protected, "Russian")
    assert "§N§" in prompt

    no_codes = [("Hi", "Привет")]
    protected2, _ = _protect_pairs_for_polish(no_codes)
    prompt2 = _build_polish_prompt(no_codes, protected2, "Russian")
    assert "§N§" not in prompt2


# ─────────────────────────────────────────────────────────────────────────────
# get_polish_system_prompt
# ─────────────────────────────────────────────────────────────────────────────

def test_polish_system_prompt_differs_from_translate_prompt():
    from ai_translator import TranslatorConfig
    cfg = TranslatorConfig(source_lang="Japanese", target_lang="Russian")
    translate_prompt = cfg.get_system_prompt()
    polish_prompt = cfg.get_polish_system_prompt()
    assert translate_prompt != polish_prompt
    assert "editor" in polish_prompt.lower()
    assert "UNCHANGED" in polish_prompt
    assert "§0§" in polish_prompt or "§N§" in polish_prompt  # защита кодов упомянута


def test_polish_system_prompt_includes_glossary_and_extra_instructions():
    from ai_translator import TranslatorConfig
    cfg = TranslatorConfig(
        glossary_block="GLOSSARY (always use these translations, never change them):\n  勇者 → Герой",
        extra_instructions="Use formal speech.",
    )
    prompt = cfg.get_polish_system_prompt()
    assert "勇者 → Герой" in prompt
    assert "Use formal speech." in prompt


# ─────────────────────────────────────────────────────────────────────────────
# _do_polish_batch — retry / rate-limit / half-batch / cancel
# ─────────────────────────────────────────────────────────────────────────────

def test_do_polish_batch_retries_then_succeeds(monkeypatch):
    from ai_translator import _do_polish_batch, TranslatorConfig

    calls = {"n": 0}

    class FlakyProvider:
        def polish_batch(self, pairs, log_cb=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporary failure")
            return [d for _, d in pairs]

    cfg = TranslatorConfig(max_retries=3, retry_delay=0.01)
    ok, out = _do_polish_batch(FlakyProvider(), [("こんにちは", "Привет")], cfg, None, None)
    assert ok is True
    assert out == ["Привет"]
    assert calls["n"] == 2


def test_do_polish_batch_gives_up_and_returns_drafts_unchanged():
    from ai_translator import _do_polish_batch, TranslatorConfig

    class AlwaysFailsProvider:
        def polish_batch(self, pairs, log_cb=None):
            raise RuntimeError("permanent failure")

    cfg = TranslatorConfig(max_retries=2, retry_delay=0.01)
    pairs = [("A", "draftA")]  # единственная пара -> half-batch split невозможен
    ok, out = _do_polish_batch(AlwaysFailsProvider(), pairs, cfg, None, None)
    assert ok is False
    assert out == ["draftA"], "БАГ: при провале полировка должна вернуть ЧЕРНОВИК, а не пустую строку"


def test_do_polish_batch_half_batch_fallback():
    from ai_translator import _do_polish_batch, TranslatorConfig

    class HalfFailsProvider:
        """Падает на батче из 2+ строк, но работает на батче из 1 строки."""
        def polish_batch(self, pairs, log_cb=None):
            if len(pairs) > 1:
                raise RuntimeError("batch too large, simulated")
            return [d + "!" for _, d in pairs]

    cfg = TranslatorConfig(max_retries=1, retry_delay=0.01)
    pairs = [("A", "draftA"), ("B", "draftB")]
    ok, out = _do_polish_batch(HalfFailsProvider(), pairs, cfg, None, None)
    assert ok is True
    assert out == ["draftA!", "draftB!"]


def test_do_polish_batch_respects_cancel_event():
    from ai_translator import _do_polish_batch, TranslatorConfig

    class NeverCalledProvider:
        def polish_batch(self, pairs, log_cb=None):
            raise AssertionError("не должен был вызываться — cancel_event уже выставлен")

    cancel = threading.Event()
    cancel.set()
    cfg = TranslatorConfig(max_retries=3)
    ok, out = _do_polish_batch(NeverCalledProvider(), [("A", "draftA")], cfg, None, cancel)
    assert ok is False
    assert out == ["draftA"]


# ─────────────────────────────────────────────────────────────────────────────
# polish_texts — основной сквозной путь
# ─────────────────────────────────────────────────────────────────────────────

class _EchoImprovedProvider:
    """Фейковый провайдер: 'улучшает' только строки с маркером IMPROVE_ME,
    остальные возвращает как есть (не изменены)."""
    def polish_batch(self, pairs, log_cb=None):
        out = []
        for _, draft in pairs:
            if "IMPROVE_ME" in draft:
                out.append(draft.replace("IMPROVE_ME", "improved"))
            else:
                out.append(draft)
        return out


@pytest.fixture
def fake_provider(monkeypatch):
    import ai_translator
    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: _EchoImprovedProvider())
    return _EchoImprovedProvider()


def test_polish_texts_happy_path_marks_changed(fake_provider):
    from ai_translator import polish_texts, TranslatorConfig
    cfg = TranslatorConfig(batch_size=10, max_retries=1)
    result = polish_texts([("orig", "draft IMPROVE_ME text")], cfg)
    assert result.succeeded == 1
    assert result.changed == 1
    assert result.results[0].after == "draft improved text"
    assert result.results[0].changed is True
    assert result.results[0].success is True


def test_polish_texts_unchanged_when_already_good(fake_provider):
    from ai_translator import polish_texts, TranslatorConfig
    cfg = TranslatorConfig(batch_size=10, max_retries=1)
    result = polish_texts([("orig", "already natural draft")], cfg)
    assert result.succeeded == 1
    assert result.changed == 0
    assert result.results[0].after == "already natural draft"
    assert result.results[0].changed is False
    assert result.results[0].success is True


def test_polish_texts_skips_pairs_with_already_broken_codes(fake_provider):
    """Пара, где original/draft уже не проходят _validate_codes, не должна
    даже отправляться в AI — для восстановления кодов есть отдельный,
    уже существующий путь (_repair_codes / _validate_translations)."""
    from ai_translator import polish_texts, TranslatorConfig
    cfg = TranslatorConfig(batch_size=10, max_retries=1)
    pairs = [(r"\C[1]hi", "перевод без кода вообще")]
    result = polish_texts(pairs, cfg)
    assert result.skipped == 1
    assert result.succeeded == 0
    assert result.results[0].success is False
    assert result.results[0].after == result.results[0].before  # черновик не тронут


def test_polish_texts_rejects_result_that_breaks_codes():
    """Если модель при полировке случайно съела код — правка отклоняется,
    черновик остаётся как был (полировка не должна портить то, что уже
    работало корректно)."""
    import ai_translator

    class BreaksCodeProvider:
        def polish_batch(self, pairs, log_cb=None):
            return ["перевод без кода вообще"]  # потерял \C[1]

    orig_get_provider = ai_translator._get_provider
    ai_translator._get_provider = lambda cfg: BreaksCodeProvider()
    try:
        cfg = ai_translator.TranslatorConfig(batch_size=10, max_retries=1)
        pairs = [(r"\C[1]hi\C[0]", r"\C[1]привет\C[0]")]
        result = ai_translator.polish_texts(pairs, cfg)
        assert result.results[0].after == r"\C[1]привет\C[0]", (
            "БАГ: правка с испорченным кодом должна была откатиться к черновику"
        )
        assert result.results[0].changed is False
        assert result.results[0].success is True  # батч отработал, просто правка отклонена
    finally:
        ai_translator._get_provider = orig_get_provider


def test_polish_texts_preserves_input_order_with_interspersed_skips(fake_provider):
    """
    BUG FIX: result.results раньше собирался в порядке ОБРАБОТКИ (сначала
    все пропущенные, потом обработанные батчами), а не в порядке входных
    pairs. Любой код, полагающийся на zip(pairs, result.results) по позиции
    (Pipeline.polish(), StringsViewer _on_polish_finished), получил бы
    рассинхронизацию original/before/after при чередовании eligible/skipped.
    """
    from ai_translator import polish_texts, TranslatorConfig
    cfg = TranslatorConfig(batch_size=2, max_retries=1)
    pairs = [
        ("A1", "draft A1 IMPROVE_ME"),
        (r"\C[1]B2", "битый код тут"),   # skip
        ("A3", "draft A3"),
        (r"\N[2]B4", "тоже битый"),        # skip
        ("A5", "draft A5 IMPROVE_ME"),
    ]
    result = polish_texts(pairs, cfg)
    assert len(result.results) == len(pairs)
    for (orig_in, draft_in), r in zip(pairs, result.results):
        assert r.original == orig_in
        assert r.before == draft_in


def test_polish_texts_cancelled_before_start_leaves_drafts_unchanged():
    import ai_translator
    orig_get_provider = ai_translator._get_provider

    class NeverCalledProvider:
        def polish_batch(self, pairs, log_cb=None):
            raise AssertionError("не должен вызываться — уже отменено")

    ai_translator._get_provider = lambda cfg: NeverCalledProvider()
    try:
        cancel = threading.Event()
        cancel.set()
        cfg = ai_translator.TranslatorConfig(batch_size=10, max_retries=1)
        result = ai_translator.polish_texts([("A", "draftA")], cfg, cancel_event=cancel)
        assert result.cancelled is True
        assert result.results[0].after == "draftA"
    finally:
        ai_translator._get_provider = orig_get_provider


def test_polish_texts_empty_input_returns_empty_result():
    from ai_translator import polish_texts, TranslatorConfig
    result = polish_texts([], TranslatorConfig())
    assert result.total == 0
    assert result.results == []


def test_polish_texts_provider_creation_failure_is_graceful():
    """Если провайдер не может быть создан (например SDK не установлен) —
    polish_texts не должен падать, а должен вернуть черновики без изменений
    с понятным failed-статусом."""
    import ai_translator
    orig_get_provider = ai_translator._get_provider
    ai_translator._get_provider = MagicMock(side_effect=RuntimeError("SDK не установлен"))
    try:
        cfg = ai_translator.TranslatorConfig()
        result = ai_translator.polish_texts([("A", "draftA")], cfg)
        assert result.failed == 1
        assert result.results[0].after == "draftA"
        assert result.results[0].success is False
    finally:
        ai_translator._get_provider = orig_get_provider


def test_polish_texts_progress_callback_gets_four_args(fake_provider):
    """progress_callback должен вызываться с (done, total, eta, fname) —
    той же сигнатурой, что используется во всём остальном пайплайне
    (см. Round 10: cli.py progress() арность)."""
    from ai_translator import polish_texts, TranslatorConfig
    calls = []

    def progress(done, total, eta, fname=""):
        calls.append((done, total, eta, fname))

    cfg = TranslatorConfig(batch_size=10, max_retries=1)
    polish_texts([("A", "draft A"), ("B", "draft B")], cfg, progress_callback=progress)
    assert len(calls) >= 1
    assert all(len(c) == 4 for c in calls)


def test_polish_texts_writes_improved_translation_to_global_tm(monkeypatch, tmp_path):
    """
    Улучшенный полировкой перевод должен так же попадать в Global TM, как
    это уже делает translate_texts для свежих переводов — иначе улучшение
    остаётся только в текущем проекте, а следующий проект с тем же
    исходным текстом снова получит неотполированный вариант.
    """
    import global_tm
    monkeypatch.setattr(global_tm.GlobalTM, "DB_PATH", tmp_path / "test_global_tm.db")

    import ai_translator

    class ImprovingProvider:
        def polish_batch(self, pairs, log_cb=None):
            return [d + " [IMPROVED]" for _, d in pairs]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: ImprovingProvider())

    cfg = ai_translator.TranslatorConfig(
        target_lang="Russian", batch_size=10, max_retries=1, use_global_tm=True,
    )
    result = ai_translator.polish_texts([("こんにちは", "Привет")], cfg)
    assert result.changed == 1

    tm_ns = "Japanese|Russian"  # BUG FIX P1-22: source_lang теперь в составе namespace
    tm = global_tm.GlobalTM(target_lang=tm_ns).open()
    try:
        hit = tm.bulk_lookup(["こんにちは"])
        assert hit.get("こんにちは") == "Привет [IMPROVED]", (
            f"БАГ: улучшенный перевод не попал в Global TM: {hit}"
        )
    finally:
        tm.close()


def test_polish_texts_global_tm_disabled_does_not_write(monkeypatch, tmp_path):
    """use_global_tm=False должен полностью отключать запись — полировка не
    должна тихо игнорировать явный выбор пользователя не использовать TM."""
    import global_tm
    monkeypatch.setattr(global_tm.GlobalTM, "DB_PATH", tmp_path / "test_global_tm.db")

    import ai_translator

    class ImprovingProvider:
        def polish_batch(self, pairs, log_cb=None):
            return [d + " [IMPROVED]" for _, d in pairs]

    monkeypatch.setattr(ai_translator, "_get_provider", lambda cfg: ImprovingProvider())

    cfg = ai_translator.TranslatorConfig(
        target_lang="Russian", batch_size=10, max_retries=1, use_global_tm=False,
    )
    ai_translator.polish_texts([("こんにちは", "Привет")], cfg)

    tm = global_tm.GlobalTM(target_lang="Russian").open()
    try:
        hit = tm.bulk_lookup(["こんにちは"])
        assert not hit.get("こんにちは"), "БАГ: запись в Global TM произошла несмотря на use_global_tm=False"
    finally:
        tm.close()


# ─────────────────────────────────────────────────────────────────────────────
# Провайдеры — polish_batch присутствует и имеет ожидаемую сигнатуру
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls_name", [
    "OpenAIProvider", "GeminiProvider", "ClaudeProvider", "GroqProvider", "DeepSeekProvider",
])
def test_all_providers_expose_polish_batch(cls_name):
    import ai_translator
    cls = getattr(ai_translator, cls_name)
    assert hasattr(cls, "polish_batch"), f"{cls_name} не реализует polish_batch"
    import inspect
    sig = inspect.signature(cls.polish_batch)
    params = list(sig.parameters)
    assert "pairs" in params, f"{cls_name}.polish_batch должен принимать 'pairs'"


def test_openai_provider_polish_batch_wires_prompt_and_restores_codes():
    """Мокаем реальный клиент openai.chat.completions.create, чтобы
    убедиться, что OpenAIProvider.polish_batch (а не тестовый фейк) реально
    строит промпт полировки и восстанавливает §N§ обратно в код."""
    from ai_translator import OpenAIProvider, TranslatorConfig
    pytest.importorskip("openai")

    cfg = TranslatorConfig(provider="openai", api_key="fake-key-for-test", target_lang="Russian")
    provider = OpenAIProvider(cfg)

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "[1] §0§Улучшенный привет§1§"
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = fake_response

    result = provider.polish_batch([(r"\C[1]Hi\C[0]", r"\C[1]Привет\C[0]")])
    assert result == [r"\C[1]Улучшенный привет\C[0]"], f"got {result!r}"

    # Убедимся что промпт полировки (не обычного перевода) реально ушёл в API
    _, kwargs = provider._client.chat.completions.create.call_args
    sent_messages = kwargs["messages"]
    system_msg = next(m["content"] for m in sent_messages if m["role"] == "system")
    user_msg = next(m["content"] for m in sent_messages if m["role"] == "user")
    assert "editor" in system_msg.lower()
    assert "DRAFT:" in user_msg and "SOURCE:" in user_msg


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline.polish()
# ─────────────────────────────────────────────────────────────────────────────

def _make_project(tmp_path, original, translated):
    game_dir = tmp_path / "game"
    output_dir = tmp_path / "out"
    (game_dir / "www" / "data").mkdir(parents=True)
    (game_dir / "www" / "data" / "Actors.json").write_text("{}", encoding="utf-8")
    ext_dir = output_dir / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Actors.json").write_text(
        json.dumps([{"file": "Actors.json", "path": "x", "original": original, "translated": ""}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    (ext_dir / "Actors_translated.json").write_text(
        json.dumps([{"file": "Actors.json", "path": "x", "original": original, "translated": translated}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return game_dir, output_dir, ext_dir


def test_pipeline_polish_end_to_end(tmp_path, monkeypatch):
    import ai_translator
    monkeypatch.setattr(
        ai_translator, "_get_provider",
        lambda cfg: type("P", (), {"polish_batch": lambda self, pairs, log_cb=None:
                                    [d.replace("IMPROVE_ME", "improved") for _, d in pairs]})()
    )
    game_dir, output_dir, ext_dir = _make_project(tmp_path, "勇者", "черновик IMPROVE_ME текст")

    from pipeline import Pipeline
    p = Pipeline(str(game_dir), str(output_dir), log_callback=lambda m: None)
    ok, msg = p.polish(provider="openai", api_key="sk-fake", target_lang="Russian")
    assert ok is True

    saved = json.loads((ext_dir / "Actors_translated.json").read_text(encoding="utf-8"))
    assert saved[0]["translated"] == "черновик improved текст"


def test_pipeline_polish_no_api_key_fails_gracefully(tmp_path):
    game_dir, output_dir, ext_dir = _make_project(tmp_path, "勇者", "черновик")
    from pipeline import Pipeline
    p = Pipeline(str(game_dir), str(output_dir), log_callback=lambda m: None)
    ok, msg = p.polish(provider="openai", api_key="")
    assert ok is False
    assert "ключ" in msg.lower()


def test_pipeline_polish_no_extract_fails_gracefully(tmp_path):
    game_dir = tmp_path / "game"
    output_dir = tmp_path / "out"
    game_dir.mkdir()
    output_dir.mkdir()
    from pipeline import Pipeline
    p = Pipeline(str(game_dir), str(output_dir), log_callback=lambda m: None)
    ok, msg = p.polish(provider="openai", api_key="sk-fake")
    assert ok is False
    assert "Extract" in msg


def test_pipeline_polish_nothing_translated_yet_fails_gracefully(tmp_path):
    """Если ни одна строка ещё не переведена — полировать нечего, и это
    должно быть понятной ошибкой, а не пустым 'успехом'."""
    game_dir = tmp_path / "game"
    output_dir = tmp_path / "out"
    (game_dir / "www" / "data").mkdir(parents=True)
    (game_dir / "www" / "data" / "Actors.json").write_text("{}", encoding="utf-8")
    ext_dir = output_dir / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Actors.json").write_text(
        json.dumps([{"file": "Actors.json", "path": "x", "original": "勇者", "translated": ""}], ensure_ascii=False),
        encoding="utf-8",
    )
    from pipeline import Pipeline
    p = Pipeline(str(game_dir), str(output_dir), log_callback=lambda m: None)
    ok, msg = p.polish(provider="openai", api_key="sk-fake")
    assert ok is False


def test_pipeline_polish_selective_texts_only_touches_requested(tmp_path, monkeypatch):
    """texts=[...] должен полировать ТОЛЬКО указанные оригиналы, оставляя
    остальные уже переведённые строки нетронутыми (точечная полировка из
    StringsViewer, а не всего проекта)."""
    import ai_translator
    monkeypatch.setattr(
        ai_translator, "_get_provider",
        lambda cfg: type("P", (), {"polish_batch": lambda self, pairs, log_cb=None:
                                    [d + " [ПОЛИРОВАНО]" for _, d in pairs]})()
    )

    game_dir = tmp_path / "game"
    output_dir = tmp_path / "out"
    (game_dir / "www" / "data").mkdir(parents=True)
    (game_dir / "www" / "data" / "Actors.json").write_text("{}", encoding="utf-8")
    ext_dir = output_dir / "extracted"
    ext_dir.mkdir(parents=True)
    entries = [
        {"file": "Actors.json", "path": "[0]", "original": "勇者", "translated": ""},
        {"file": "Actors.json", "path": "[1]", "original": "村人", "translated": ""},
    ]
    (ext_dir / "Actors.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    tr_entries = [
        {"file": "Actors.json", "path": "[0]", "original": "勇者", "translated": "Герой черновик"},
        {"file": "Actors.json", "path": "[1]", "original": "村人", "translated": "Житель черновик"},
    ]
    (ext_dir / "Actors_translated.json").write_text(json.dumps(tr_entries, ensure_ascii=False), encoding="utf-8")

    from pipeline import Pipeline
    p = Pipeline(str(game_dir), str(output_dir), log_callback=lambda m: None)
    ok, msg = p.polish(provider="openai", api_key="sk-fake", texts=["勇者"])
    assert ok is True

    saved = json.loads((ext_dir / "Actors_translated.json").read_text(encoding="utf-8"))
    by_orig = {e["original"]: e["translated"] for e in saved}
    assert by_orig["勇者"] == "Герой черновик [ПОЛИРОВАНО]"
    assert by_orig["村人"] == "Житель черновик", "БАГ: полировка задела строку, не входящую в texts="


# ─────────────────────────────────────────────────────────────────────────────
# cli.py polish
# ─────────────────────────────────────────────────────────────────────────────

def test_cli_polish_base_url_argument_and_full_run(monkeypatch, tmp_path):
    """Тот же сценарий, что поймал баг у `cli.py translate` (Round 10):
    --base-url должен парситься, и весь путь cmd_polish -> Pipeline.polish()
    -> polish_texts() -> progress_callback должен отработать без
    AttributeError/TypeError."""
    import cli
    import ai_translator

    def fake_polish_batch(self, pairs, log_cb=None):
        return [d + " ПОЛИРОВАНО" for _, d in pairs]

    monkeypatch.setattr(ai_translator.OpenAIProvider, "polish_batch", fake_polish_batch)

    game_dir = tmp_path / "game"
    output_dir = tmp_path / "out"
    (game_dir / "www" / "data").mkdir(parents=True)
    (game_dir / "www" / "data" / "Actors.json").write_text("{}", encoding="utf-8")
    ext_dir = output_dir / "extracted"
    ext_dir.mkdir(parents=True)
    (ext_dir / "Actors.json").write_text(
        json.dumps([{"file": "Actors.json", "path": "x", "original": "勇者", "translated": ""}], ensure_ascii=False),
        encoding="utf-8",
    )
    (ext_dir / "Actors_translated.json").write_text(
        json.dumps([{"file": "Actors.json", "path": "x", "original": "勇者", "translated": "Герой"}], ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", [
        "cli.py", "polish",
        "--game", str(game_dir), "--output", str(output_dir),
        "--provider", "openai", "--api-key", "sk-fake",
        "--base-url", "http://localhost:1234/v1",
    ])
    rc = cli.main()
    assert rc == 0

    saved = json.loads((ext_dir / "Actors_translated.json").read_text(encoding="utf-8"))
    assert saved[0]["translated"] == "Герой ПОЛИРОВАНО"


def test_cli_polish_registered_in_dispatch_and_parser():
    """Структурная проверка на случай будущего рефакторинга cli.py: команда
    polish должна быть зарегистрирована в argparse и доходить до cmd_polish."""
    import cli
    import io
    import contextlib

    parser_help = io.StringIO()
    with contextlib.redirect_stdout(parser_help):
        try:
            sys.argv = ["cli.py", "--help"]
            cli.main()
        except SystemExit:
            pass
    assert "polish" in parser_help.getvalue()
