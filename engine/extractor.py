"""
extractor.py — извлечение текста из RPG Maker MV/MZ игры.
Читает JSON-файлы из data/ и извлекает все переводимые строки.
Сохраняет в structured JSON с полной информацией о местоположении.
"""

from engine.file_utils import find_dir_ci
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

from collections import Counter

from .text_filter import should_translate, classify_text_for_translation

EVENT_CODE_SHOW_TEXT_HEADER = 101
EVENT_CODE_SHOW_TEXT_LINE = 401
EVENT_CODE_SHOW_CHOICES = 102
EVENT_CODE_CHOICE_BRANCH = 402
EVENT_CODE_SCROLL_TEXT = 105
EVENT_CODE_SCROLL_LINE = 405
EVENT_CODE_CHANGE_NAME = 320
EVENT_CODE_CHANGE_NICKNAME = 321
EVENT_CODE_CHANGE_PROFILE = 324
EVENT_CODE_PLUGIN_CMD = 357
EVENT_CODE_SCRIPT      = 355   # первая строка JS скрипта
EVENT_CODE_SCRIPT_CTD  = 655   # продолжение скрипта

# BUG FIX: compiled once at module level, not inside the event-processing loop
_JS_STRING_RE = re.compile(r'[\u0022\u0027]([^\u0022\u0027\n]{3,})[\u0022\u0027]')


def _log(msg: str, cb: Callable[[str], None] | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


class TextEntry:
    __slots__ = ("file", "path", "original", "context", "code")

    def __init__(self, file: str, path: str, original: str, context: str = "", code: int = 0):
        self.file = file
        self.path = path
        self.original = original
        self.context = context
        self.code = code

    def to_dict(self) -> dict:
        return {
            "file":      self.file,
            "path":      self.path,
            "original":  self.original,
            "translated": "",
            "context":   self.context,
            "game_type": "rpgmv",   # BUG FIX: was missing; all other engines set this
            "code":      self.code,
        }


class RPGMVExtractor:
    def __init__(self, game_dir: Path, log_cb: Callable[[str], None] | None = None):
        self.game_dir = game_dir
        self.log_cb = log_cb
        self.entries: list[TextEntry] = []
        self.skip_reasons: Counter[str] = Counter()

    def _log(self, msg: str):
        _log(msg, self.log_cb)

    def _data_dir(self) -> Path | None:
        for candidate in [
            self.game_dir / "data",
            self.game_dir / "www" / "data",
            *([d] if (d := find_dir_ci(self.game_dir, "Data")) else []),
        ]:
            if candidate.exists():
                return candidate
        return None

    def _add(self, file: str, path: str, text: str, context: str = "", code: int = 0):
        ok, reason = classify_text_for_translation(text)
        if ok:
            self.entries.append(TextEntry(file, path, text, context, code))
        else:
            self.skip_reasons[reason] += 1

    def _extract_field(self, obj: dict, field: str, file: str, base_path: str, context: str = ""):
        val = obj.get(field)
        if isinstance(val, str):
            self._add(file, f"{base_path}.{field}", val, context)

    def _extract_event_list(self, commands: list, file: str, base_path: str):
        """Извлечь текст из списка команд события."""
        i = 0
        while i < len(commands):
            cmd = commands[i]
            if not isinstance(cmd, dict):
                i += 1
                continue

            code = cmd.get("code", 0)
            params = cmd.get("parameters", [])

            if code == EVENT_CODE_SHOW_TEXT_HEADER:
                # MZ only: speaker name is params[4]. In MV params[0] is face file — not speaker.
                if len(params) > 4 and isinstance(params[4], str) and should_translate(params[4]):
                    self._add(file, f"{base_path}[{i}].parameters[4]", params[4], "speaker_name", code)
                i += 1
                line_texts = []
                line_indices = []
                while i < len(commands):
                    sub = commands[i]
                    if not isinstance(sub, dict) or sub.get("code") != EVENT_CODE_SHOW_TEXT_LINE:
                        break
                    sub_params = sub.get("parameters", [])
                    text = sub_params[0] if sub_params else ""
                    if isinstance(text, str):
                        line_texts.append(text)
                        line_indices.append(i)
                    i += 1
                if line_texts:
                    combined = "\n".join(line_texts)
                    if should_translate(combined):
                        path = f"{base_path}[{line_indices[0]}..{line_indices[-1]}].dialog"
                        self._add(file, path, combined, "dialog_text", EVENT_CODE_SHOW_TEXT_LINE)
                continue

            elif code == EVENT_CODE_SHOW_CHOICES:
                choices = params[0] if params else []
                if isinstance(choices, list):
                    for ci, choice in enumerate(choices):
                        if isinstance(choice, str):
                            self._add(file, f"{base_path}[{i}].parameters[0][{ci}]", choice, "choice", code)

            elif code == EVENT_CODE_SCROLL_TEXT:
                i += 1
                scroll_lines = []
                scroll_idx = []
                while i < len(commands):
                    sub = commands[i]
                    if not isinstance(sub, dict) or sub.get("code") != EVENT_CODE_SCROLL_LINE:
                        break
                    sp = sub.get("parameters", [])
                    t = sp[0] if sp else ""
                    if isinstance(t, str):
                        scroll_lines.append(t)
                        scroll_idx.append(i)
                    i += 1
                if scroll_lines:
                    combined = "\n".join(scroll_lines)
                    if should_translate(combined):
                        path = f"{base_path}[{scroll_idx[0]}..{scroll_idx[-1]}].scroll"
                        self._add(file, path, combined, "scroll_text", EVENT_CODE_SCROLL_LINE)
                continue

            elif code == EVENT_CODE_PLUGIN_CMD:  # 357 — MZ Plugin Commands
                # MZ format: params[3] is JSON-string with named args
                if len(params) > 3 and isinstance(params[3], str) and params[3].strip().startswith("{"):
                    try:
                        args = json.loads(params[3])
                        if isinstance(args, dict):
                            for key, val in args.items():
                                if isinstance(val, str) and should_translate(val):
                                    self._add(
                                        file,
                                        f"{base_path}[{i}].plugin_arg.{key}",
                                        val,
                                        "plugin_cmd_arg",
                                        code,
                                    )
                    except (json.JSONDecodeError, ValueError):
                        pass

            elif code == EVENT_CODE_CHANGE_NAME:
                if len(params) > 1 and isinstance(params[1], str):
                    self._add(file, f"{base_path}[{i}].parameters[1]", params[1], "actor_name", code)

            elif code == EVENT_CODE_CHANGE_NICKNAME:
                if len(params) > 1 and isinstance(params[1], str):
                    self._add(file, f"{base_path}[{i}].parameters[1]", params[1], "actor_nickname", code)

            elif code == EVENT_CODE_CHANGE_PROFILE:
                # BUG FIX: params[0] is actorId (int), params[1] is the profile text
                # Same layout as Change Name (320) and Change Nickname (321)
                if len(params) > 1 and isinstance(params[1], str):
                    self._add(file, f"{base_path}[{i}].parameters[1]", params[1], "actor_profile", code)

            elif code in (EVENT_CODE_SCRIPT, EVENT_CODE_SCRIPT_CTD):
                # JS скрипты — извлекаем строковые литералы с японским текстом
                line = params[0] if params else ""
                if isinstance(line, str) and line.strip():
                    for m in _JS_STRING_RE.finditer(line):
                        candidate = m.group(1)
                        if should_translate(candidate) and any(
                            0x3040 <= ord(c) <= 0x9FFF for c in candidate
                        ):
                            self._add(
                                file,
                                f"{base_path}[{i}].parameters[0]",
                                candidate,
                                "script_literal",
                                code,
                            )

            elif code == EVENT_CODE_SHOW_TEXT_LINE:
                # BUG FIX: "осиротевшая" строка диалога — 401 без непосредственно
                # предшествующего 101 в этом же проходе цикла. Если МЕЖДУ 101 и
                # первой 401 оказывается любая другая команда, цикл сбора внутри
                # ветки EVENT_CODE_SHOW_TEXT_HEADER обрывается с пустым
                # line_texts — ничего не добавляется, а сама 401 остаётся
                # необработанной на этом, внешнем уровне цикла, где раньше её
                # просто молча пропускали (см. идентичный фикс и его историю
                # в engine/vxace_extractor.py). Извлекаем отдельной строкой —
                # хуже группировки, но не потеря. Путь ОБЯЗАН быть self-range
                # "[i..i]", а не голым индексом "[i]" — _inject_dialog() в
                # injector.py матчит только "\[(\d+)\.\.(\d+)\]\.dialog$".
                text = params[0] if params else ""
                if isinstance(text, str) and should_translate(text):
                    self._add(file, f"{base_path}[{i}..{i}].dialog", text, "dialog_text", code)

            elif code == EVENT_CODE_SCROLL_LINE:
                # То же самое для "осиротевшей" строки прокручиваемого текста (405).
                text = params[0] if params else ""
                if isinstance(text, str) and should_translate(text):
                    self._add(file, f"{base_path}[{i}..{i}].scroll", text, "scroll_text", code)

            i += 1

    def _extract_actors(self, data: list, file: str):
        for idx, actor in enumerate(data):
            if not isinstance(actor, dict):
                continue
            base = f"[{idx}]"
            ctx = f"Actor: {actor.get('name', '')}"
            for field in ("name", "nickname", "profile"):
                self._extract_field(actor, field, file, base, ctx)

    def _extract_simple_items(self, data: list, file: str, fields: tuple[str, ...]):
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            ctx = f"{item.get('name', '')}"
            for field in fields:
                self._extract_field(item, field, file, f"[{idx}]", ctx)

    def _extract_map(self, data: dict, file: str):
        display_name = data.get("displayName", "")
        if display_name and should_translate(display_name):
            self._add(file, "displayName", display_name, "map_name")

        events = data.get("events", [])
        if isinstance(events, list):
            for eidx, event in enumerate(events):
                if not isinstance(event, dict):
                    continue
                pages = event.get("pages", [])
                for pidx, page in enumerate(pages):
                    if not isinstance(page, dict):
                        continue
                    cmd_list = page.get("list", [])
                    self._extract_event_list(cmd_list, file, f"events[{eidx}].pages[{pidx}].list")
        elif isinstance(events, dict):
            for eid, event in events.items():
                if not isinstance(event, dict):
                    continue
                pages = event.get("pages", [])
                for pidx, page in enumerate(pages):
                    if not isinstance(page, dict):
                        continue
                    cmd_list = page.get("list", [])
                    self._extract_event_list(cmd_list, file, f"events[{eid}].pages[{pidx}].list")

    def _extract_common_events(self, data: list, file: str):
        for idx, event in enumerate(data):
            if not isinstance(event, dict):
                continue
            cmd_list = event.get("list", [])
            self._extract_event_list(cmd_list, file, f"[{idx}].list")

    def _extract_troops(self, data: list, file: str):
        for idx, troop in enumerate(data):
            if not isinstance(troop, dict):
                continue
            name = troop.get("name", "")
            if name and should_translate(name):
                self._add(file, f"[{idx}].name", name, "troop_name")
            pages = troop.get("pages", [])
            for pidx, page in enumerate(pages):
                if not isinstance(page, dict):
                    continue
                self._extract_event_list(page.get("list", []), file, f"[{idx}].pages[{pidx}].list")

    def _extract_system(self, data: dict, file: str):
        for field in ("gameTitle", "currencyUnit"):
            self._extract_field(data, field, file, "", "system")

        terms = data.get("terms", data.get("words", {}))
        if isinstance(terms, dict):
            for key, val in terms.items():
                if isinstance(val, str) and should_translate(val):
                    self._add(file, f"terms.{key}", val, "ui_term")
                elif isinstance(val, list):
                    for i, v in enumerate(val):
                        if isinstance(v, str) and should_translate(v):
                            self._add(file, f"terms.{key}[{i}]", v, "ui_term")
                elif isinstance(val, dict):
                    # terms.messages — вложенный dict с UI сообщениями
                    for sub_key, sub_val in val.items():
                        if isinstance(sub_val, str) and should_translate(sub_val):
                            self._add(file, f"terms.{key}.{sub_key}", sub_val, "ui_message")

        messages = data.get("messages", {})
        if isinstance(messages, dict):
            for key, val in messages.items():
                if isinstance(val, str):
                    self._add(file, f"messages.{key}", val, "ui_message")

        armor_types = data.get("armorTypes", [])
        for i, t in enumerate(armor_types):
            if isinstance(t, str) and should_translate(t):
                self._add(file, f"armorTypes[{i}]", t, "armor_type")

        weapon_types = data.get("weaponTypes", [])
        for i, t in enumerate(weapon_types):
            if isinstance(t, str) and should_translate(t):
                self._add(file, f"weaponTypes[{i}]", t, "weapon_type")

        skill_types = data.get("skillTypes", [])
        for i, t in enumerate(skill_types):
            if isinstance(t, str) and should_translate(t):
                self._add(file, f"skillTypes[{i}]", t, "skill_type")

        equip_types = data.get("equipTypes", [])
        for i, t in enumerate(equip_types):
            if isinstance(t, str) and should_translate(t):
                self._add(file, f"equipTypes[{i}]", t, "equip_type")

        element_names = data.get("elements", [])
        for i, t in enumerate(element_names):
            if isinstance(t, str) and should_translate(t):
                self._add(file, f"elements[{i}]", t, "element")

    def extract(
        self,
        progress_callback: Callable[[int, int, float, str], None] | None = None,
    ) -> list[dict]:
        data_dir = self._data_dir()
        if not data_dir:
            raise FileNotFoundError(
                f"Папка data/ не найдена в {self.game_dir}\n"
                "Убедитесь, что это корректная папка RPG Maker MV/MZ."
            )

        self._log(f"[Engine] Папка данных: {data_dir}")
        json_files = sorted(data_dir.glob("*.json"))
        self._log(f"[Engine] Найдено файлов: {len(json_files)}")

        HANDLERS: dict[str, str] = {
            "Actors": "actors",
            "Armors": "items",
            "Classes": "classes",
            "CommonEvents": "common_events",
            "Enemies": "enemies",
            "Items": "items",
            "Skills": "skills",
            "States": "states",
            "System": "system",
            "Troops": "troops",
            "Weapons": "items",
        }

        ITEM_FIELDS: dict[str, tuple[str, ...]] = {
            "actors": ("name", "nickname", "profile"),
            "items": ("name", "description"),
            "classes": ("name",),
            "enemies": ("name",),
            "skills": ("name", "description", "message1", "message2"),
            "states": ("name", "message1", "message2", "message3", "message4"),
        }

        for file_idx, jf in enumerate(json_files):
            if progress_callback:
                progress_callback(file_idx + 1, len(json_files), -1.0, jf.name)
            stem = jf.stem
            fname = jf.name
            self._log(f"[Engine] Обработка: {fname}")

            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                self._log(f"  ✗ Ошибка чтения {fname}: {e}")
                continue

            if stem == "System":
                if isinstance(data, dict):
                    self._extract_system(data, fname)
            elif stem == "CommonEvents":
                if isinstance(data, list):
                    self._extract_common_events(data, fname)
            elif stem == "Troops":
                if isinstance(data, list):
                    self._extract_troops(data, fname)
            elif stem.startswith("Map") and stem != "MapInfos":
                if isinstance(data, dict):
                    self._extract_map(data, fname)
            elif stem == "MapInfos":
                if isinstance(data, list):
                    for idx, info in enumerate(data):
                        if isinstance(info, dict):
                            self._extract_field(info, "name", fname, f"[{idx}]", "map_info_name")
            else:
                handler = HANDLERS.get(stem)
                if handler == "actors":
                    if isinstance(data, list):
                        self._extract_actors(data, fname)
                elif handler in ITEM_FIELDS:
                    fields = ITEM_FIELDS[handler]
                    if isinstance(data, list):
                        self._extract_simple_items(data, fname, fields)
                else:
                    pass

        self._log(f"[Engine] Извлечено строк: {len(self.entries)}")
        if self.skip_reasons:
            top = ', '.join(f"{k}={v}" for k, v in self.skip_reasons.most_common(6))
            self._log(f"[Engine] Пропущено: {sum(self.skip_reasons.values())} ({top})")
        return [e.to_dict() for e in self.entries]


def extract_game(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int, float, str], None] | None = None,
) -> dict:
    """
    Главная функция извлечения.
    Сохраняет результат в output_dir/extracted/ по одному файлу на каждый игровой JSON.
    Возвращает сводку.
    """
    _log(f"[Engine] Начало извлечения: {game_dir}", log_callback)

    extractor = RPGMVExtractor(game_dir, log_callback)
    entries = extractor.extract(progress_callback=progress_callback)

    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list[dict]] = {}
    for entry in entries:
        key = entry["file"]
        by_file.setdefault(key, []).append(entry)

    # BUG FIX: при повторном Extract оставались старые *.json от карт/файлов,
    # которые удалили из игры. Эти файлы потом попадали в перевод как ghost-строки.
    # Удаляем source-файлы (не _translated), которых нет в новом extract.
    existing_src = {
        f for f in extract_dir.glob("*.json")
        if not f.name.endswith("_translated.json")
    }
    new_src_names = {fname for fname in by_file}
    stale = existing_src - {extract_dir / n for n in new_src_names}
    for stale_path in stale:
        try:
            stale_path.unlink()
            # Удаляем соответствующий _translated.json тоже, иначе он останется
            tr_path = stale_path.with_name(stale_path.stem + "_translated.json")
            if tr_path.exists():
                tr_path.unlink()
            _log(f"  ✂ Удалён устаревший файл: {stale_path.name}", log_callback)
        except Exception as e:
            _log(f"  ⚠ Не удалось удалить устаревший {stale_path.name}: {e}", log_callback)

    
    saved_files = []
    for fname, file_entries in by_file.items():
        out_path = extract_dir / fname
        # BUG FIX: атомарная запись — прямой open() при сбое обнулял файл
        content = json.dumps(file_entries, ensure_ascii=False, indent=2).encode("utf-8")
        tmp_fd, tmp_path = tempfile.mkstemp(dir=extract_dir, suffix=".tmp", prefix=".rpgmtl_")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(content)
            os.replace(tmp_path, out_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
        saved_files.append(out_path)
        _log(f"  ✓ {fname}: {len(file_entries)} строк", log_callback)

    index_path = output_dir / "extract_index.json"
    index = {
        "game_dir": str(game_dir),
        "total_strings": len(entries),
        "files": {fname: len(entries_) for fname, entries_ in by_file.items()},
    }
    # Атомарная запись индекса
    idx_content = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp", prefix=".rpgmtl_")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(idx_content)
        os.replace(tmp_path, index_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    _log(f"[Engine] ✓ Сохранено файлов: {len(saved_files)}, строк: {len(entries)}", log_callback)
    return index
