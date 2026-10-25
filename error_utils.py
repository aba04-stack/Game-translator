"""Utilities for precise error reporting and persistent logs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import sys
import threading
import traceback
from typing import Callable

# См. BUG FIX в config.py: RPGMTL_HOME изолирует тестовую сессию от
# настоящего ~/.rpgmtl_logs пользователя.
LOG_DIR = Path(os.environ.get("RPGMTL_HOME", str(Path.home()))) / ".rpgmtl_logs"
LOG_FILE = LOG_DIR / "rpgmtl.log"


def _ensure_log_dir() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def console_print(message: str) -> None:
    """Напечатать ОДНУ строку лога в stdout — безопасно.

    * Один вызов write(): print() делает два (текст и «\\n»), поэтому строки из
      параллельных потоков перевода склеивались («✓ Батч  ✓ Батч\\n\\n») — это
      хорошо видно в CLI и CI-логах.
    * Ничего не делает, если stdout is None (pythonw / PyInstaller --noconsole).
    * Никогда не бросает исключений: закрытый пайп или консоль, которая не умеет
      кодировать «✓» (cp1251 / cp866 при перенаправленном выводе на Windows), не
      должны убивать многочасовой перевод из-за строки лога.
    """
    out = sys.stdout
    if out is None:
        return
    line = message + "\n"
    try:
        out.write(line)
        out.flush()
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "ascii"
        try:
            out.write(line.encode(enc, "replace").decode(enc))
            out.flush()
        except Exception:
            pass
    except (OSError, ValueError):   # BrokenPipeError, закрытый файл
        pass


def append_log_to_file(message: str) -> None:
    """Append a message to a persistent app log."""
    try:
        _ensure_log_dir()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            for line in message.splitlines() or [""]:
                f.write(f"[{stamp}] {line}\n")
    except Exception:
        # Logging must never crash the app.
        pass


def _last_frame(exc: BaseException):
    tb = exc.__traceback__
    frames = traceback.extract_tb(tb) if tb else []
    return frames[-1] if frames else None


def format_exception_details(
    exc: BaseException,
    context: str | None = None,
    *,
    include_traceback: bool = True,
    include_source_line: bool = True,
) -> str:
    """Return a human-readable error report with exact file/line/function."""
    parts: list[str] = []
    if context:
        parts.append(f"[{context}]")

    parts.append(f"Type: {type(exc).__name__}")
    parts.append(f"Message: {exc}")

    frame = _last_frame(exc)
    if frame is not None:
        parts.append(f"Where: {frame.filename}:{frame.lineno} in {frame.name}")
        if include_source_line and frame.line:
            parts.append(f"Code: {frame.line.strip()}")

    if include_traceback:
        parts.append("Traceback:")
        parts.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())

    return "\n".join(parts)


def install_excepthook(log_fn: Callable[[str], None] | None = None) -> None:
    """Install hooks for uncaught exceptions in main and worker threads."""
    def _emit(report: str) -> None:
        if log_fn is not None:
            try:
                log_fn(report)
                return
            except Exception:
                pass
        print(report, file=sys.stderr)

    def _handle(exc_type, exc, tb):
        _emit(format_exception_details(exc, context="UNCAUGHT"))

    sys.excepthook = _handle

    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            _emit(format_exception_details(args.exc_value, context=f"THREAD:{getattr(args.thread, 'name', 'unknown')}"))
        threading.excepthook = _thread_hook
