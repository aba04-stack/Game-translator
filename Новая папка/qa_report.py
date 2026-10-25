"""
qa_report.py — пост-переводная проверка качества (QA) переведённых строк.

Идея взята из профессиональных localization QA-инструментов (Smartling,
SimpleLocalize, memoQ/Xbench): вместо того чтобы искать проблему в игре
методом "играть и смотреть, где сломалось", прогоняем автоматические
проверки по всем переведённым строкам ДО инъекции и получаем структурированный
отчёт: что именно подозрительно, в каком файле, и насколько это серьёзно.

Разделение важности (как у Smartling/SimpleLocalize):
  - critical — коды/плейсхолдеры потеряны или порядок изменился. Это то,
    что реально ломает игру (см. репорт про "коды анимации" — если движок
    ищет ресурс по коду, а код исчез из перевода, это тот же класс проблемы).
  - warning — числа разошлись или длина перевода сильно превышает оригинал.
    Не обязательно ошибка (грамматика языков разная), но стоит посмотреть
    глазами — особенно длина, если у game есть фиксированные текстовые поля.

Это НЕ блокирует Inject само по себе — это отчёт для человека, отдельный
шаг. Existing missing-code retry logic в ai_translator.py уже пытается
чинить критичные code-проблемы во время перевода; этот модуль — финальная
сверка по факту, на случай если что-то всё же проскочило (или было
отредактировано вручную после перевода).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

# Переиспользуем уже проверенную (retry-логика перевода использует именно
# её) регулярку RPG-Maker-стиля escape-кодов вместо того, чтобы придумывать
# новую. Она покрывает RPG Maker/VX Ace/Wolf/Kirikiri/Tyrano (общий стиль
# \C[1], \N[2] и т.п.). Unity/Godot/Ren'Py используют другой синтаксис тегов
# ({0}, [color=...], {i}...{/i}) и этой проверкой полноценно не покрываются —
# это осознанное ограничение, а не недосмотр.
from ai_translator import _extract_codes, _validate_codes, _validate_engine_tokens  # noqa: E402

_NUMBER_RE = re.compile(r"\d+")

DEFAULT_LENGTH_RATIO = 3.0   # перевод длиннее оригинала в N раз — предупреждение
MIN_LENGTH_FOR_RATIO_CHECK = 3  # не проверять соотношение на очень коротких строках


def _code_adjacency_pattern(text: str, codes: list[str]) -> list[bool] | None:
    """Для списка кодов (в порядке появления в тексте) вернуть по одному
    bool на каждую ПАРУ соседних кодов: True, если между ними есть непустой
    (не только пробелы) текст, False если коды идут вплотную друг за другом.

    Используется для обнаружения случая, когда пара кодов, оборачивавшая
    слово в оригинале (например "\\C[2]Герой\\C[0]"), в переводе съехалась
    вплотную друг к другу ("Герой\\C[2]\\C[0]") — список и порядок кодов при
    этом ОДИНАКОВЫЕ (см. _validate_codes), поэтому это не ловится базовой
    проверкой, а визуально форматирование (обёртка) пропадает.
    """
    if len(codes) < 2:
        return []
    positions = []
    pos = 0
    for c in codes:
        idx = text.find(c, pos)
        if idx == -1:
            return None
        positions.append((idx, idx + len(c)))
        pos = idx + len(c)
    return [bool(text[positions[i][1]:positions[i + 1][0]].strip())
            for i in range(len(positions) - 1)]


def _load_source_originals(extract_dir: Path, translated_filename: str) -> dict[str, str] | None:
    """Загрузить {path: original} из исходного (не _translated) Extract-файла,
    соответствующего данному *_translated.json.

    Возвращает None, если исходный файл не найден/не читается — в этом
    случае проверка P0-4 ниже НИЧЕГО не флагает (не можем ни подтвердить,
    ни опровергнуть — лучше промолчать, чем дать ложное critical).
    """
    if not translated_filename.endswith("_translated.json"):
        return None
    src_path = extract_dir / (translated_filename[: -len("_translated.json")] + ".json")
    if not src_path.exists():
        return None
    try:
        src_entries = json.loads(src_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(src_entries, list):
        return None
    return {
        e.get("path", ""): e.get("original", "")
        for e in src_entries if isinstance(e, dict) and e.get("path")
    }


def check_entry_against_source(entry: dict[str, Any], source_originals: dict[str, str] | None) -> list[dict[str, str]]:
    """
    BUG FIX (P0-4, внешний аудит): injector доверяет полям path/original
    ИЗ САМОГО translated.json — сверяет их только с ТЕКУЩИМ содержимым
    игрового файла (drift-защита), но не с тем, что РЕАЛЬНО извлёк Extract
    для этого path. Если path+original в translated.json окажутся
    рассинхронизированы с исходной Extract-записью для того же path
    (например, из-за бага где-то в пайплайне, повредившего файл вручную
    вне UI — StringsViewer сам эти поля не даёт редактировать), injector
    не имеет способа это заметить и может записать перевод не в ту строку.
    Source-файлы (не _translated) — уже существующая, неизменяемая с
    точки зрения Inject запись того, что извлёк Extract; сверяем с ней.

    source_originals=None означает "не смогли загрузить source для сверки"
    — в этом случае функция ничего не проверяет (см. _load_source_originals).
    """
    if source_originals is None:
        return []
    path = entry.get("path", "")
    if not path:
        return []
    if path not in source_originals:
        return [{
            "type": "entry_path_unknown",
            "severity": "critical",
            "detail": (
                f"path {path!r} отсутствует в исходном Extract-файле для этого "
                "translated.json — запись не привязана ни к одной реально "
                "извлечённой строке. Возможно, файл был повреждён вне UI, или "
                "нужен повторный Extract."
            ),
        }]
    expected = source_originals[path]
    actual = entry.get("original", "")
    if expected != actual:
        return [{
            "type": "entry_original_mismatch",
            "severity": "critical",
            "detail": (
                f"original для path {path!r} не совпадает с тем, что реально "
                f"извлёк Extract: при Extract было {expected!r}, в translated.json "
                f"указано {actual!r}. Запись может относиться к другой строке — "
                "Inject для неё небезопасен без повторного Extract."
            ),
        }]
    return []


def check_entry(entry: dict[str, Any], length_ratio: float = DEFAULT_LENGTH_RATIO) -> list[dict[str, str]]:
    """
    Прогнать все проверки для одной переведённой записи.
    Возвращает список найденных проблем (может быть пустым).
    Ничего не проверяет, если translated пустой (запись ещё не переведена,
    либо в карантине — см. _low_confidence в wolf_extractor.py).
    """
    original = entry.get("original", "")
    translated = entry.get("translated", "")
    if not translated or not original:
        return []

    issues: list[dict[str, str]] = []

    # ── 1. Engine syntax / placeholders (critical) ───────────────
    # Используем game_type из entry, чтобы Unity/Ren'Py/Godot не проверялись
    # только RPG Maker regex-ом. Потеря технического токена может привести
    # к runtime error, поэтому такой Inject нельзя пропускать.
    game_type = str(entry.get("game_type") or "unknown")
    # Always compute the code lists: the numeric QA stage below also uses them
    # to avoid counting digits inside technical tokens. Previously they were
    # created only inside the critical-error branch, so a perfectly valid entry
    # raised UnboundLocalError and blocked the entire Inject.
    orig_codes = _extract_codes(original)
    trans_codes = _extract_codes(translated)
    if not _validate_codes(original, translated):
        issues.append({
            "type": "codes",
            "severity": "critical",
            "detail": (
                f"Базовые escape-коды не совпадают: "
                f"было {orig_codes or '—'}, стало {trans_codes or '—'}"
            ),
        })
    elif not _validate_engine_tokens(original, translated, game_type):
        issues.append({
            "type": "engine_tokens",
            "severity": "critical",
            "detail": (
                f"Технические токены не совпадают для {game_type}: "
                f"было {orig_codes or 'engine placeholders'}, стало {trans_codes or 'engine placeholders'}"
            ),
        })
    elif len(orig_codes) >= 2:
        # BUG FIX (внешний аудит, подтверждено эмпирически): _validate_codes
        # выше сравнивает только СПИСОК кодов и их взаимный порядок — не их
        # положение относительно текста. "\C[2]Герой\C[0]" (обёртка вокруг
        # слова) и "Герой\C[2]\C[0]" (оба кода просто пришиты в конец) дают
        # ОДИНАКОВЫЙ список в ОДИНАКОВОМ порядке, так что _validate_codes
        # считает оба валидными — а форматирование при этом визуально
        # пропадает (коды съехались вплотную, ничего больше не оборачивают).
        # warning, не critical: эвристика по наличию текста между парой
        # соседних кодов, точную семантику кода (что именно он форматирует)
        # мы не знаем, так что это сигнал "проверить глазами", а не факт
        # поломки.
        orig_pattern = _code_adjacency_pattern(original, orig_codes)
        trans_pattern = _code_adjacency_pattern(translated, trans_codes)
        if orig_pattern and orig_pattern != trans_pattern:
            issues.append({
                "type": "code_position",
                "severity": "warning",
                "detail": (
                    "Коды на месте и в том же порядке, но их положение "
                    "относительно текста изменилось — похоже, обёртка "
                    "форматирования вокруг слова слетела. Стоит проверить."
                ),
            })

    # ── 2. Числа (warning) ────────────────────────────────────────
    # Вычищаем распознанные коды перед подсчётом чисел — иначе "\C[1]"
    # даёт отдельное "numbers"-предупреждение поверх уже пойманного выше
    # "codes"-critical для одной и той же причины (цифра внутри кода).
    orig_no_codes = original
    trans_no_codes = translated
    for c in orig_codes:
        orig_no_codes = orig_no_codes.replace(c, " ")
    for c in _extract_codes(translated):
        trans_no_codes = trans_no_codes.replace(c, " ")
    orig_nums = _NUMBER_RE.findall(orig_no_codes)
    trans_nums = _NUMBER_RE.findall(trans_no_codes)
    if Counter(orig_nums) != Counter(trans_nums):
        issues.append({
            "type": "numbers",
            "severity": "warning",
            "detail": f"Числа разошлись: было {orig_nums or '—'}, стало {trans_nums or '—'}",
        })

    # ── 3. Длина (warning) ─────────────────────────────────────────
    if len(original) >= MIN_LENGTH_FOR_RATIO_CHECK and len(translated) > len(original) * length_ratio:
        issues.append({
            "type": "length",
            "severity": "warning",
            "detail": f"Перевод длиннее оригинала в {len(translated) / len(original):.1f}x "
                      f"({len(original)} → {len(translated)} симв.)",
        })

    return issues


def run_qa_report(
    extract_dir: Path,
    length_ratio: float = DEFAULT_LENGTH_RATIO,
    progress_callback=None,
) -> dict[str, Any]:
    """
    Прогнать QA-проверки по всем *_translated.json в extract_dir.
    Возвращает структурированный отчёт:
      {
        "checked": int, "clean": int,
        "critical_count": int, "warning_count": int,
        "issues": [ {file, path, original, translated, type, severity, detail}, ... ],
        "unreadable_files": [ {file, error}, ... ],
      }
    issues отсортированы: сначала critical, внутри группы — по файлу.

    unreadable_files — файлы, которые НЕ удалось прочитать и потому НЕ проверены.
    Раньше они молча пропускались, и отчёт «проверено N, чисто» выглядел как
    гарантия по всем данным, хотя часть вообще не проверялась.

    progress_callback(current, total, eta, filename) — единый контракт прогресса
    (по одному вызову на файл).
    """
    import time as _time
    extract_dir = Path(extract_dir)
    trans_files = sorted(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []

    checked = 0
    clean = 0
    issues: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    started = _time.monotonic()
    total_files = len(trans_files)

    for file_index, tf in enumerate(trans_files, 1):
        try:
            entries = json.loads(tf.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                raise ValueError("ожидался JSON-массив записей")
        except Exception as e:
            unreadable.append({"file": tf.name, "error": f"{type(e).__name__}: {e}"})
            if progress_callback:
                progress_callback(file_index, total_files,
                                  (_time.monotonic() - started) / file_index * (total_files - file_index), tf.name)
            continue
        source_originals = _load_source_originals(extract_dir, tf.name)
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("translated"):
                continue
            checked += 1
            found = check_entry(entry, length_ratio=length_ratio)
            found = found + check_entry_against_source(entry, source_originals)
            if not found:
                clean += 1
                continue
            for issue in found:
                issues.append({
                    "file": entry.get("file", tf.name),
                    "path": entry.get("path", ""),
                    "original": entry.get("original", ""),
                    "translated": entry.get("translated", ""),
                    **issue,
                })

        if progress_callback:
            progress_callback(file_index, total_files,
                              (_time.monotonic() - started) / file_index * (total_files - file_index), tf.name)

    issues.sort(key=lambda i: (0 if i["severity"] == "critical" else 1, i["file"], i["path"]))

    return {
        "checked": checked,
        "clean": clean,
        "critical_count": sum(1 for i in issues if i["severity"] == "critical"),
        "warning_count": sum(1 for i in issues if i["severity"] == "warning"),
        "issues": issues,
        "unreadable_files": unreadable,
    }


def format_qa_report_text(report: dict[str, Any], max_lines: int = 50) -> str:
    """Человекочитаемый текстовый отчёт для лога/консоли (не JSON)."""
    lines = [
        f"QA-проверка: {report['checked']} переведённых строк, "
        f"{report['clean']} без замечаний, "
        f"{report['critical_count']} critical, {report['warning_count']} warning.",
    ]
    unreadable = report.get("unreadable_files") or []
    if unreadable:
        names = ", ".join(u["file"] for u in unreadable[:5]) + (" …" if len(unreadable) > 5 else "")
        lines.append(f"  ⚠ НЕ проверено: не удалось прочитать {len(unreadable)} файл(ов): {names}")
    for issue in report["issues"][:max_lines]:
        icon = "🔴" if issue["severity"] == "critical" else "🟡"
        orig_short = issue["original"][:40] + ("…" if len(issue["original"]) > 40 else "")
        lines.append(f"  {icon} [{issue['file']}:{issue['path']}] {issue['type']}: {issue['detail']} | «{orig_short}»")
    remaining = len(report["issues"]) - max_lines
    if remaining > 0:
        lines.append(f"  … и ещё {remaining} (см. полный JSON-отчёт)")
    return "\n".join(lines)
