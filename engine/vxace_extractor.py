"""
vxace_extractor.py — извлечение текста из RPG Maker VX/Ace игр.

Формат: .rvdata2 (Ruby Marshal).
Структура событий идентична MV/MZ (те же коды команд).
"""

from engine.file_utils import atomic_write_bytes, atomic_write_json, find_dir_ci
import io
from pathlib import Path
from typing import Callable

from .text_filter import should_translate

# Коды событий (те же что в MV/MZ)
CODE_TEXT_HEADER = 101   # начало диалога (speaker, face)
CODE_TEXT_LINE   = 401   # строка диалога
CODE_CHOICES     = 102   # выбор
CODE_SCROLL_TEXT = 105   # прокручиваемый текст
CODE_SCROLL_LINE = 405   # строка прокрутки
CODE_CHANGE_PROF = 324   # смена профиля актёра
CODE_PLUGIN_CMD  = 357   # плагин-команда (редко в VXAce)


def _get_str(obj) -> str:
    """Извлечь строку из RubyString или обычной str."""
    try:
        from rubymarshal.reader import RubyString
        if isinstance(obj, RubyString):
            return obj.text or ""
    except ImportError:
        pass
    return str(obj) if obj is not None else ""


def _get_attr(obj, key: str, default="") -> str:
    """Получить строковый атрибут из RubyObject."""
    try:
        val = obj.attributes.get(key)
        return _get_str(val) if val is not None else default
    except AttributeError:
        return default


def _log(msg: str, cb: Callable | None = None):
    print(msg, flush=True)
    if cb:
        cb(msg)


class VXAceExtractor:
    def __init__(self, game_dir: Path, log_cb: Callable | None = None):
        self.game_dir = Path(game_dir)
        self.log_cb   = log_cb
        self.entries: list[dict] = []

    def _log(self, msg: str):
        _log(msg, self.log_cb)

    def _find_data_dir(self) -> Path | None:
        d = find_dir_ci(self.game_dir, "Data")
        if d and list(d.glob("*.rvdata2")):
            return d
        return None

    def _load(self, path: Path):
        """Загрузить .rvdata2 файл."""
        import rubymarshal.reader as rdr
        # Добавляем try/except для защиты от неизвестных Ruby-объектов
        try:
            with open(path, "rb") as f:
                return rdr.load(f)
        except ValueError as e:
            if "Unknown Ruby object type" in str(e) or "Unknown class" in str(e):
                self._log(f"  ⚠ Пропуск {path.name}: содержит неизвестные Ruby классы ({e})")
                return None
            raise
        except Exception as e:
            self._log(f"  ⚠ Пропуск {path.name}: ошибка парсинга ({e})")
            return None

    def _save(self, path: Path, data) -> bool:
        """Сохранить данные обратно в .rvdata2."""
        import rubymarshal.writer as wrt
        buf = io.BytesIO()
        wrt.write(buf, data)
        atomic_write_bytes(path, buf.getvalue())
        return True

    def _add(self, file: str, path: str, text: str, context: str):
        self.entries.append({
            "file":       file,
            "path":       path,
            "original":   text,
            "translated": "",
            "context":    context,
            "code":       0,
            "game_type":  "vxace",
        })

    # ── Простые файлы (Actors, Items, Weapons, etc.) ──────────

    def _extract_named(self, path: Path, context_prefix: str,
                    fields: tuple = ("@name", "@description")):
        """Извлечь из файлов с массивом объектов: Actors, Items, Weapons, Armors, Enemies.

        Не включаем @note по умолчанию — это системные метаданные, не отображаемый текст.
        """
        try:
            data = self._load(path)
        except Exception as e:
            self._log(f"  ✗ {path.name}: {e}")
            return
        if not isinstance(data, list):
            return
        fname = path.name
        for idx, obj in enumerate(data):
            if obj is None:
                continue
            for field in fields:
                val = _get_attr(obj, field)
                if val and should_translate(val):
                    self._add(fname, f"[{idx}]{field}", val, f"{context_prefix}:{field[1:]}")

    # ── System.rvdata2 ─────────────────────────────────────────

    def _extract_system(self, path: Path):
        try:
            data = self._load(path)
        except Exception as e:
            self._log(f"  ✗ System.rvdata2: {e}")
            return
        fname = path.name
        for field in ("@game_title", "@currency_unit"):
            val = _get_attr(data, field)
            if val and should_translate(val):
                self._add(fname, field, val, f"system:{field[1:]}")

    # ── Map*.rvdata2 ───────────────────────────────────────────

    def _extract_map(self, path: Path):
        try:
            data = self._load(path)
        except Exception as e:
            self._log(f"  ✗ {path.name}: {e}")
            return
        fname = path.name

        # displayName (RPGMVXAce Map hea)
        display = _get_attr(data, "@display_name")
        if display and should_translate(display):
            self._add(fname, "@display_name", display, "map_name")

        # events — хэш {id: RPG::Event}
        events_hash = None
        try:
            events_hash = data.attributes.get("@events")
        except AttributeError:
            # BUG FIX: раньше молча пропускалось — файл вносил в перевод
            # ТОЛЬКО display_name (извлечённый выше), все диалоги/события
            # карты терялись без единого сообщения о причине.
            self._log(f"  ⚠ {fname}: неожиданная структура карты (нет @events) — события пропущены")
        if not events_hash:
            return

        for ev_id, event in events_hash.items():
            if event is None:
                continue
            pages = None
            try:
                pages = event.attributes.get("@pages", [])
            except AttributeError:
                continue
            if not pages:
                continue
            for pi, page in enumerate(pages):
                try:
                    cmds = page.attributes.get("@list", [])
                except AttributeError:
                    continue
                self._extract_event_list(fname, f"events[{ev_id}].pages[{pi}].list", cmds)

    def _extract_event_list(self, fname: str, base_path: str, commands: list):
        """Извлечь текст из списка команд события."""
        if not commands:
            return
        i = 0
        while i < len(commands):
            cmd = commands[i]
            try:
                code   = cmd.attributes.get("@code", 0)
                params = cmd.attributes.get("@parameters", [])
            except AttributeError:
                i += 1
                continue

            if code == CODE_TEXT_HEADER:
                # params[0]=face_file, [1]=face_index, [2]=background, [3]=position
                # VXAce НЕ хранит speaker в params — имя идёт отдельной командой 408
                # Собираем строки диалога
                i += 1
                lines = []
                indices = []
                while i < len(commands):
                    sub = commands[i]
                    try:
                        sc = sub.attributes.get("@code", 0)
                        sp = sub.attributes.get("@parameters", [])
                    except AttributeError:
                        break
                    if sc != CODE_TEXT_LINE:
                        break
                    t = _get_str(sp[0]) if sp else ""
                    if t:
                        lines.append(t)
                        indices.append(i)
                    i += 1
                if lines:
                    full = "\n".join(lines)
                    if should_translate(full):
                        self._add(fname, f"{base_path}[{indices[0]}..{indices[-1]}].dialog",
                                  full, "dialog_text")
                continue

            elif code == CODE_CHOICES:
                choices = params[0] if params else []
                if isinstance(choices, list):
                    for ci, choice in enumerate(choices):
                        text = _get_str(choice)
                        if text and should_translate(text):
                            self._add(fname, f"{base_path}[{i}].parameters[0][{ci}]",
                                      text, "choice")

            elif code == CODE_SCROLL_TEXT:
                i += 1
                lines = []
                indices = []
                while i < len(commands):
                    sub = commands[i]
                    try:
                        sc = sub.attributes.get("@code", 0)
                        sp = sub.attributes.get("@parameters", [])
                    except AttributeError:
                        break
                    if sc != CODE_SCROLL_LINE:
                        break
                    t = _get_str(sp[0]) if sp else ""
                    if t:
                        lines.append(t)
                        indices.append(i)
                    i += 1
                if lines:
                    full = "\n".join(lines)
                    if should_translate(full):
                        self._add(fname, f"{base_path}[{indices[0]}..{indices[-1]}].scroll",
                                  full, "scroll_text")
                continue

            elif code == CODE_CHANGE_PROF:
                if len(params) > 1:
                    text = _get_str(params[1])
                    if text and should_translate(text):
                        self._add(fname, f"{base_path}[{i}].parameters[1]",
                                  text, "actor_profile")

            elif code == CODE_TEXT_LINE:
                # BUG FIX: "осиротевшая" строка диалога — 401 без непосредственно
                # предшествующего 101 в этом же цикле сбора (см. ветку
                # CODE_TEXT_HEADER выше: если МЕЖДУ 101 и первой 401 оказывается
                # любая другая команда, вложенный цикл сбора обрывается с ПУСТЫМ
                # lines, ничего не добавляется, а сама 401 остаётся необработанной
                # на этом, внешнем уровне цикла — где раньше её просто молча
                # пропускали, теряя текст без единого предупреждения. Извлекаем
                # её отдельной строкой — хуже группировки построчно, но не потеря.
                #
                # ВАЖНО: путь использует "range" [i..i] (а не голый индекс [i]),
                # потому что vxace_injector._apply_translation() для .dialog/
                # .scroll ищет ИМЕННО ('range', (start,end)) шаг перед последним
                # шагом — голый ('index', i) там не распознаётся, и инъекция
                # молча проваливалась на 100% таких строк (нашёл при сквозной
                # проверке extract→inject после первого варианта фикса).
                text = _get_str(params[0]) if params else ""
                if text and should_translate(text):
                    self._add(fname, f"{base_path}[{i}..{i}].dialog", text, "dialog_text")

            elif code == CODE_SCROLL_LINE:
                # То же самое для "осиротевшей" строки прокручиваемого текста (405).
                text = _get_str(params[0]) if params else ""
                if text and should_translate(text):
                    self._add(fname, f"{base_path}[{i}..{i}].scroll", text, "scroll_text")

            i += 1

    # ── CommonEvents.rvdata2 ───────────────────────────────────

    def _extract_common_events(self, path: Path):
        try:
            data = self._load(path)
        except Exception as e:
            self._log(f"  ✗ CommonEvents.rvdata2: {e}")
            return
        if not isinstance(data, list):
            return
        fname = path.name
        for idx, ev in enumerate(data):
            if ev is None:
                continue
            try:
                cmds = ev.attributes.get("@list", [])
            except AttributeError:
                continue
            self._extract_event_list(fname, f"[{idx}].list", cmds)

    # ── Troops.rvdata2 ─────────────────────────────────────────

    def _extract_troops(self, path: Path):
        try:
            data = self._load(path)
        except Exception as e:
            self._log(f"  ✗ Troops.rvdata2: {e}")
            return
        if not isinstance(data, list):
            return
        fname = path.name
        for idx, troop in enumerate(data):
            if troop is None:
                continue
            try:
                pages = troop.attributes.get("@pages", [])
            except AttributeError:
                continue
            for pi, page in enumerate(pages):
                try:
                    cmds = page.attributes.get("@list", [])
                except AttributeError:
                    continue
                self._extract_event_list(fname, f"[{idx}].pages[{pi}].list", cmds)

    # ── Главный метод ──────────────────────────────────────────

    def extract(self, progress_cb: Callable | None = None) -> list[dict]:
        data_dir = self._find_data_dir()
        if not data_dir:
            raise FileNotFoundError(f".rvdata2 файлы не найдены в {self.game_dir}")

        # Проверяем rubymarshal
        import importlib.util
        if importlib.util.find_spec("rubymarshal") is None:
            raise RuntimeError(
                "rubymarshal не установлен.\n"
                "Установите: pip install rubymarshal"
            )

        # Список файлов для обработки
        tasks = []

        # Простые файлы с именами/описаниями
        for name, ctx, fields in [
            ("Actors.rvdata2",  "actor",  ("@name", "@nickname", "@description")),
            ("Items.rvdata2",   "item",   ("@name", "@description")),
            ("Weapons.rvdata2", "weapon", ("@name", "@description")),
            ("Armors.rvdata2",  "armor",  ("@name", "@description")),
            ("Skills.rvdata2",  "skill",  ("@name", "@description", "@message1", "@message2")),
            ("Enemies.rvdata2", "enemy",  ("@name",)),
            ("Classes.rvdata2", "class",  ("@name",)),
        ]:
            f = data_dir / name
            if f.exists():
                tasks.append(("named", f, ctx, fields))

        # Системные данные
        sys_f = data_dir / "System.rvdata2"
        if sys_f.exists():
            tasks.append(("system", sys_f, None, None))

        # Общие события
        ce_f = data_dir / "CommonEvents.rvdata2"
        if ce_f.exists():
            tasks.append(("common", ce_f, None, None))

        # Битвы
        tr_f = data_dir / "Troops.rvdata2"
        if tr_f.exists():
            tasks.append(("troops", tr_f, None, None))

        # Карты
        for f in sorted(data_dir.glob("Map[0-9]*.rvdata2")):
            tasks.append(("map", f, None, None))

        total = len(tasks)
        self._log(f"[VXAce] Файлов для обработки: {total}")

        for i, task in enumerate(tasks):
            kind = task[0]
            path = task[1]
            if progress_cb:
                progress_cb(i + 1, total, -1.0, "")
            self._log(f"[VXAce] Обработка: {path.name}")
            before = len(self.entries)

            if kind == "named":
                self._extract_named(path, task[2], task[3])
            elif kind == "system":
                self._extract_system(path)
            elif kind == "common":
                self._extract_common_events(path)
            elif kind == "troops":
                self._extract_troops(path)
            elif kind == "map":
                self._extract_map(path)

            added = len(self.entries) - before
            if added:
                self._log(f"[VXAce]   → {added} строк")

        self._log(f"[VXAce] ✓ Итого: {len(self.entries)} строк из {total} файлов")
        return self.entries


def extract_vxace(
    game_dir: Path,
    output_dir: Path,
    log_callback: Callable | None = None,
    progress_callback: Callable | None = None,
) -> dict:
    """Главная функция извлечения для RPG Maker VX/Ace."""
    _log(f"[VXAce] Начало извлечения: {game_dir}", log_callback)

    # BUG FIX: без этой проверки отсутствие rubymarshal проявлялось как per-file
    # "✗ Actors.rvdata2: No module named 'rubymarshal'" на КАЖДЫЙ файл отдельно —
    # легко потерять в логе среди других сообщений, и результат выглядел как
    # "Extract завершён, 0 строк" без ясной причины. Теперь — одна явная ошибка
    # в начале, до попытки обработки файлов.
    try:
        import rubymarshal.reader  # noqa: F401
    except ImportError:
        msg = (
            "[VXAce] ✗ Библиотека 'rubymarshal' не установлена — она нужна для "
            "чтения .rvdata2 файлов RPG Maker VX/Ace.\n"
            "  Установите командой: pip install rubymarshal>=1.2.0"
        )
        _log(msg, log_callback)
        return {
            "game_dir": str(game_dir), "game_type": "vxace",
            "total_strings": 0, "files": {},
            "error": "rubymarshal_missing",
        }

    ext = VXAceExtractor(game_dir, log_callback)
    entries = ext.extract(progress_cb=progress_callback)

    extract_dir = output_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list[dict]] = {}
    for e in entries:
        by_file.setdefault(e["file"], []).append(e)

    for fname, file_entries in by_file.items():
        # BUG FIX: was fname + ".json" → double extension "Actors.rvdata2.json"
        # apply_dedup_to_files uses jf.name as key; entries must match.
        # Strip original extension before adding .json.
        base = fname.replace("/", "_").replace("\\", "_")
        stem = base.rsplit(".", 1)[0] if "." in base else base
        safe = stem + ".json"
        # Update file field and preserve original path for injector
        for e in file_entries:
            e["_source_rvdata2"] = e["file"]  # original .rvdata2 filename for injector
            e["file"] = safe
        out = extract_dir / safe
        atomic_write_json(out, file_entries)

    index = {
        "game_dir":      str(game_dir),
        "game_type":     "vxace",
        "total_strings": len(entries),
        "files":         {f: len(e) for f, e in by_file.items()},
    }
    atomic_write_json(output_dir / "extract_index.json", index)
    _log(f"[VXAce] ✓ Сохранено: {len(entries)} строк", log_callback)
    return index
