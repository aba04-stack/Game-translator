"""
Лог в stdout: строки из параллельных потоков не склеиваются, а ошибки вывода
(stdout=None в GUI без консоли, закрытый пайп, консоль без «✓») не роняют работу.
"""
import io
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_translator
import pipeline
from engine import state as state_mod
from error_utils import console_print


class _ContendedOut:
    """stdout, который отдаёт управление другим потокам на каждой записи.

    Раньше print(msg) делал ДВЕ записи (текст и перевод строки); между ними другой
    поток успевал вписать свой текст — строки склеивались. Здесь это воспроизводится
    детерминированно, а не «иногда, если повезёт».
    """
    encoding = "utf-8"

    def __init__(self):
        self.chunks: list[str] = []

    def write(self, s):
        time.sleep(0.0003)
        self.chunks.append(s)
        return len(s)

    def flush(self):
        pass


def _hammer(log_fn, monkeypatch, threads=8, per_thread=40):
    out = _ContendedOut()
    monkeypatch.setattr(sys, "stdout", out)

    def work(t):
        for i in range(per_thread):
            log_fn(f"T{t}-{i}")

    ts = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def _assert_intact(out, expected):
    assert all(c.endswith("\n") and c.count("\n") == 1 for c in out.chunks), \
        "строка лога должна доходить до stdout ОДНОЙ записью"
    lines = "".join(out.chunks).splitlines()
    assert len(lines) == expected
    assert all(re.fullmatch(r"T\d-\d+", ln) for ln in lines), "строки склеились"


def test_console_print_lines_from_many_threads_stay_intact(monkeypatch):
    _assert_intact(_hammer(console_print, monkeypatch), 8 * 40)


def test_ai_translator_pipeline_and_state_loggers_use_the_safe_writer(monkeypatch):
    _assert_intact(_hammer(lambda m: ai_translator._log(m), monkeypatch), 8 * 40)
    _assert_intact(_hammer(lambda m: pipeline._log(m), monkeypatch), 8 * 40)
    _assert_intact(_hammer(lambda m: state_mod._log(m), monkeypatch), 8 * 40)


def test_callback_still_receives_every_message(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    got = []
    ai_translator._log("hello", got.append)
    assert got == ["hello"]


def test_stdout_none_is_a_noop_like_pythonw_noconsole(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    console_print("nothing to print to")                       # не должно бросать
    got = []
    ai_translator._log("still reaches the GUI log", got.append)
    assert got == ["still reaches the GUI log"]


def test_console_that_cannot_encode_a_checkmark_does_not_crash(monkeypatch):
    raw = io.BytesIO()
    narrow = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", narrow)
    console_print("✓ Готово: 5 строк")                        # раньше → UnicodeEncodeError
    narrow.flush()
    out = raw.getvalue().decode("ascii")
    assert out.endswith("\n") and "5" in out and "?" in out    # заменено, но не потеряно целиком


def test_closed_pipe_does_not_kill_the_run(monkeypatch):
    class Broken:
        encoding = "utf-8"

        def write(self, s):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError
    monkeypatch.setattr(sys, "stdout", Broken())
    console_print("x")                                         # не должно бросать
