"""
pipeline.py — улучшенный pipeline: extract → translate → inject.

Лучшие практики:
- Дедупликация (экономия 60-80% API запросов)
- Глоссарий (стабильные имена и термины)
- Resume (продолжить прерванный перевод)
- Кэш переводов (не переводить дважды)
"""

from engine.file_utils import atomic_write_json, find_dir_ci
from engine.state import load_state
from engine.project_lock import read_manifest, verify_manifest, write_manifest

# CJK / Japanese character range check — module-level, no closure overhead
def _has_cjk(text: str) -> bool:
    """Return True if text contains hiragana, katakana or CJK ideographs."""
    return any(0x3040 <= ord(ch) <= 0x30FF or 0x4E00 <= ord(ch) <= 0x9FFF
               for ch in text)
import json
import os
import sys
import tempfile
import time
import shutil
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))

from error_utils import console_print, format_exception_details


def _log(msg: str, cb: Callable[[str], None] | None = None) -> None:
    console_print(msg)
    if cb:
        cb(msg)


class Pipeline:
    # Кэш статистики на уровне класса: {output_dir_str: (cache_key, stats)}
    # Живёт дольше одного экземпляра, поэтому UI-поллинг не читает диск каждые 3с.
    _stats_class_cache: dict = {}

    # BUG FIX: stale_extract раньше жил ВНУТРИ _stats_class_cache и наследовал его
    # cache_key = (extract_dir mtime, state_file mtime). Это НЕ включает game_dir,
    # поэтому если пользователь патчил/менял файлы игры после Extract, кэш не
    # инвалидировался — stale_extract застревал в последнем известном значении
    # до следующего Extract/Translate. Плюс сам rglob() по game_dir может быть
    # дорогим для игр с тысячами ассетов, а UI дёргает get_stats() каждые 4с
    # СИНХРОННО на главном потоке (через QTimer, не воркер) — риск подвисания UI.
    # Теперь это отдельный, независимо троттлящийся кэш с safety-cap на сканирование.
    _stale_check_cache: dict = {}      # {output_dir_str: (wall_time, is_stale)}
    _STALE_CHECK_THROTTLE_SEC = 45.0   # не чаще одного полного скана раз в 45с
    _STALE_CHECK_MAX_FILES = 20000     # safety cap — не зависаем на гигантских играх

    def __init__(
        self,
        game_dir: str | Path,
        output_dir: str | Path,
        log_callback: Callable[[str], None] | None = None,
    ):
        self.game_dir = Path(game_dir)
        self.output_dir = Path(output_dir)
        self.log_cb = log_callback
        # Cache for get_stats() to avoid re-reading all files on every UI poll
        self._stats_cache: tuple | None = None

    def _log(self, msg: str) -> None:
        _log(msg, self.log_cb)

    def _log_model(self, provider: str, model: str) -> None:
        """Строка «Модель:» + предупреждение, если модель закрыта / скоро будет закрыта.

        Мёртвая модель иначе проявляется потоком непонятных ошибок API посреди
        перевода; здесь пользователь узнаёт об этом сразу (запрос не блокируется —
        таблица жизненного цикла может отставать от провайдера).
        """
        from ai_translator import DEFAULT_MODELS, model_lifecycle_notice
        default = DEFAULT_MODELS.get(provider, "")
        self._log(f"  Модель:     {model or ('авто → ' + default if default else 'авто')}")
        notice = model_lifecycle_notice(provider, model or default)
        if notice:
            self._log(f"  {notice}")

    # Структурированный итог последнего translate(): для скриптов и CLI.
    # translate() намеренно возвращает (True, "⚠ ...") при частичном провале (GUI
    # показывает предупреждение в главном сообщении), поэтому по булеву значению
    # «всё ли переведено» не определить. Ключи: unique, translated, failed,
    # remaining, cancelled, fatal_error (причина остановки из-за постоянной ошибки API).
    last_translate_summary: dict = {}

    def _sync_existing_translations(
        self,
        extract_dir: Path,
        state,
    ) -> dict[str, int]:
        """Подхватить уже существующие *_translated.json в кэш, чтобы продолжать с места остановки."""
        from ai_translator import _validate_codes, _repair_codes

        stats = {
            "files": 0,
            "entries": 0,
            "added": 0,
            "updated": 0,
            "broken": 0,
            "repaired": 0,
            "invalid_json": 0,
        }

        if not extract_dir.exists():
            return stats

        for jf in sorted(extract_dir.glob("*_translated.json")):
            stats["files"] += 1
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                stats["invalid_json"] += 1
                self._log(f"[Translate] ⚠ Не удалось прочитать уже сохранённый перевод {jf.name}: {e}")
                continue

            file_changed = False
            for entry in data:
                orig = (entry.get("original") or "").strip()
                trans = (entry.get("translated") or "").strip()
                if not orig:
                    continue
                stats["entries"] += 1
                if not trans:
                    continue
                if _validate_codes(orig, trans):
                    if not state.has(orig):
                        state.set(orig, trans, source="sync")
                        stats["added"] += 1
                    elif state.get(orig) != trans:
                        # BUG FIX: файл на диске отличается от уже закэшированного
                        # значения — единственная причина этому: ручная правка
                        # через StringsViewer (Save / Bulk Replace / Re-translate),
                        # ведь сам pipeline всегда пишет файл ИЗ state. Раньше
                        # `if not state.has(orig)` пропускал этот случай целиком,
                        # так что следующий запуск Translate брал из state старый
                        # AI-перевод и apply_dedup_to_files() тихо затирал им
                        # ручную правку пользователя в файле. Файл — источник
                        # истины для ручных правок; обновляем state под него.
                        state.set(orig, trans, source="sync")
                        stats["updated"] += 1
                else:
                    # BUG FIX: try to repair broken codes instead of silently discarding
                    repaired = _repair_codes(orig, trans)
                    if _validate_codes(orig, repaired):
                        entry["translated"] = repaired
                        file_changed = True
                        if not state.has(orig):
                            state.set(orig, repaired, source="sync")
                            stats["added"] += 1
                        elif state.get(orig) != repaired:
                            state.set(orig, repaired, source="sync")
                            stats["updated"] += 1
                        stats["repaired"] += 1
                    else:
                        stats["broken"] += 1
                        self._log(f"[Translate] ⚠ Пропущен сохранённый перевод с битым кодом: {orig[:80]!r}")

            # Write back repaired entries
            if file_changed:
                try:

                    atomic_write_json(jf, data)
                except Exception as e:
                    self._log(f"[Translate] ⚠ Не удалось записать исправления в {jf.name}: {e}")

        if stats["added"] or stats["updated"] or stats["repaired"]:
            state.save()

        return stats

    def get_game_type(self) -> str:
        """Определить тип игры: 'rpgmv' | 'rpgmz' | 'renpy' | 'unity' | 'godot' | 'wolf' | 'vxace' | 'kirikiri' | 'tyrano' | 'unknown'."""
        return self.get_game_type_verbose()[0]

    def get_game_type_verbose(self) -> tuple[str, str]:
        """Как get_game_type(), но также возвращает причину (какой маркер сработал) — для диагностики в логах."""
        try:
            from game_detector import detect_game_type_verbose
            return detect_game_type_verbose(self.game_dir)
        except Exception as e:
            return "unknown", f"Ошибка при определении типа: {e}"

    def check_game_dir(self) -> tuple[bool, str]:
        if not self.game_dir.exists():
            return False, f"Папка игры не найдена: {self.game_dir}"

        try:
            detected = self.get_game_type()
            if detected != "unknown":
                return True, ""
        except Exception:
            pass

        # Консервативная проверка для случаев, когда детектор не увидел игру,
        # но структура проекта всё ещё явно похожа на поддерживаемый движок.
        if (self.game_dir / "project.godot").exists():
            # Надёжный признак: project.godot — всегда Godot
            return True, ""
        if any(self.game_dir.glob("*.pck")):
            # .pck также бывает у RPG Maker плагинов и других движков.
            # BUG FIX: проверяем дополнительные признаки Godot.
            # Godot .pck создаётся рядом с .exe того же имени.
            exe_candidates = list(self.game_dir.glob("*.exe")) + list(self.game_dir.glob("*.x86_64"))
            pck_stems = {p.stem for p in self.game_dir.glob("*.pck")}
            exe_stems = {e.stem for e in exe_candidates}
            if pck_stems & exe_stems:  # совпадение имён .pck и .exe → Godot
                return True, ""
        if (self.game_dir / "ProjectSettings" / "ProjectVersion.txt").exists():
            return True, ""
        if (self.game_dir / "game").is_dir() and any((self.game_dir / "game").rglob("*.rpy")):
            return True, ""
        if any(self.game_dir.glob("*.rpy")):
            return True, ""
        _wolf_data_dir = find_dir_ci(self.game_dir, "Data")
        for candidate in [
            (_wolf_data_dir / "BasicData" if _wolf_data_dir else self.game_dir / "Data" / "BasicData"),
            self.game_dir / "data" / "BasicData",
        ]:
            if candidate.is_dir() and (list(candidate.glob("*.wolf")) or list(candidate.glob("*.mps"))):
                return True, ""
        for candidate in filter(None, [find_dir_ci(self.game_dir, "Data"), self.game_dir / "data"]):
            if candidate.is_dir() and list(candidate.glob("*.rvdata2")):
                return True, ""
        if any(self.game_dir.rglob("*.ks")):
            return True, ""

        return False, (
            f"Не удалось определить тип игры: {self.game_dir}\n"
            "Поддерживаются: RPG Maker MV/MZ, Ren'Py, Unity, Godot, Wolf RPG Editor, VX/Ace, TyranoBuilder, Kirikiri/KAG"
        )

    # ──────────────────────────────────────────────────────────
    # ШАГ 1: EXTRACT
    # ──────────────────────────────────────────────────────────
    def extract(
        self,
        progress_callback: Callable[[int, int, float, str], None] | None = None,
    ) -> tuple[bool, str]:
        
        def p_wrap(c: int, t: int, eta: float = -1.0, fname: str = ""):
            if progress_callback:
                progress_callback(c, t, eta, fname)

        self._log("\n" + "═" * 58)
        self._log("[Extract] Начало извлечения текста...")
        self._log(f"  Игра:    {self.game_dir}")
        self._log(f"  Вывод:   {self.output_dir}")

        ok, err = self.check_game_dir()
        if not ok:
            self._log(f"[Extract] ✗ {err}")
            return False, err

        game_type, detect_reason = self.get_game_type_verbose()
        self._log(f"[Extract] Тип игры: {game_type.upper()} ({detect_reason})")

        existing_manifest = read_manifest(self.output_dir)
        if existing_manifest:
            ok_lock, lock_msg = verify_manifest(self.output_dir, self.game_dir, game_type)
            if not ok_lock:
                self._log(f"[Extract] ✗ {lock_msg}")
                return False, "[Extract] ✗ " + lock_msg
        else:
            # Legacy workspaces without a manifest are ambiguous. Never reuse one
            # for another game automatically: this is exactly the cross-game overwrite
            # class we are trying to eliminate. An empty/new workspace is fine.
            legacy_index = self.output_dir / "extract_index.json"
            legacy_extracted = self.output_dir / "extracted"
            if legacy_index.exists() or (legacy_extracted.exists() and any(legacy_extracted.iterdir())):
                return False, "[Extract] ✗ Рабочая папка уже содержит старые данные без project_manifest.json. Создайте новую папку вывода или удалите старую рабочую папку и выполните Extract заново."

        try:
            if game_type == "wolf":
                from engine.wolf_extractor import extract_wolf
                index = extract_wolf(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    progress_callback=p_wrap,
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))
                msg = (
                    f"[Extract] ✓ Wolf RPG: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )
            elif game_type == "vxace":
                from engine.vxace_extractor import extract_vxace
                index = extract_vxace(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    progress_callback=p_wrap,
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))
                msg = (
                    f"[Extract] ✓ VX/Ace: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )
            elif game_type == "kirikiri":
                from engine.kirikiri_extractor import extract_kirikiri
                index = extract_kirikiri(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    progress_callback=p_wrap,
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))
                msg = (
                    f"[Extract] ✓ Kirikiri/KAG: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )
            elif game_type == "tyrano":
                from engine.tyrano_extractor import extract_tyrano
                index = extract_tyrano(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    progress_callback=p_wrap,
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))
                msg = (
                    f"[Extract] ✓ TyranoBuilder: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )
            elif game_type == "renpy":
                from renpy_extractor import extract_renpy
                index = extract_renpy(
                    self.game_dir, self.output_dir, self.log_cb, p_wrap
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))
                msg = (
                    f"[Extract] ✓ Ren'Py: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )
            elif game_type == "unity":
                from engine.unity_extractor import extract_unity
                index = extract_unity(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    progress_callback=p_wrap,
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))
                msg = (
                    f"[Extract] ✓ Unity: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )
            elif game_type == "godot":
                from engine.godot_extractor import extract_godot
                index = extract_godot(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    progress_callback=p_wrap,
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))
                msg = (
                    f"[Extract] ✓ Godot Engine: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )
            else:
                from engine.extractor import extract_game
                from engine.glossary import load_or_create_glossary

                index = extract_game(
                    self.game_dir, self.output_dir, self.log_cb, p_wrap
                )
                total = index.get("total_strings", 0)
                files = len(index.get("files", {}))

                glossary = load_or_create_glossary(self.output_dir, self.game_dir, self.log_cb)
                if len(glossary) > 0:
                    self._log(f"[Extract] Глоссарий: {len(glossary)} терминов")

                msg = (
                    f"[Extract] ✓ RPG Maker: извлечено {total} строк из {files} файлов\n"
                    f"  → {self.output_dir / 'extracted'}"
                )

            self._log(msg)

            # Жёстко привязываем workspace к игре. Путь не входит в identity,
            # поэтому игру можно переместить, но другой проект использовать нельзя.
            try:
                manifest = write_manifest(self.output_dir, self.game_dir, game_type)
                fingerprint = manifest.get("fingerprint", "")[:16]
                self._log(f"[Extract] Project lock: {fingerprint}…")
            except Exception as e:
                return False, f"[Extract] ✗ Не удалось создать project lock: {e}"

            # Сохраняем метаданные экстракта, НЕ уничтожая engine-specific
            # поля extractor-а (sources, files, offsets и т.п.). Unity injector
            # например использует `sources` для точного обратного пути.
            try:
                index_path = self.output_dir / "extract_index.json"
                existing_index = {}
                if index_path.exists():
                    try:
                        existing_index = json.loads(index_path.read_text(encoding="utf-8"))
                    except Exception:
                        existing_index = {}
                existing_index.update({
                    "game_dir": str(self.game_dir),
                    "game_type": game_type,
                    "project_fingerprint": (read_manifest(self.output_dir) or {}).get("fingerprint", ""),
                    "timestamp": time.time(),
                    "total_strings": total,
                    "files": files,
                })
                atomic_write_json(index_path, existing_index)
                # Инвалидируем троттлированный stale-кэш немедленно — иначе UI мог бы
                # до 45с показывать «⚠ игра изменилась» уже ПОСЛЕ свежего Extract.
                Pipeline._stale_check_cache.pop(str(self.output_dir), None)
            except Exception:
                pass
            return True, msg
        except Exception as e:
            self._log(format_exception_details(e, context="Pipeline.extract"))
            msg = f"[Extract] ✗ Ошибка: {e}"
            self._log(msg)
            return False, msg

    # ──────────────────────────────────────────────────────────
    # ШАГ 2: TRANSLATE
    # ──────────────────────────────────────────────────────────
    def translate(
        self,
        provider: str,
        api_key: str,
        model: str = "",
        source_lang: str = "Japanese",
        target_lang: str = "Russian",
        batch_size: int = 25,
        max_workers: int = 3,
        max_retries: int = 3,
        request_delay: float = 0.5,
        extra_instructions: str = "",
        use_context: bool = True,
        base_url: str = "",
        use_global_tm: bool = True,
        include_low_confidence: bool = False,
        progress_callback: Callable[[int, int, float, str], None] | None = None,
        cancel_event=None,
    ) -> tuple[bool, str]:
        batch_size = max(1, int(batch_size or 1))
        max_workers = max(1, min(10, int(max_workers or 3)))
        max_retries = max(1, int(max_retries or 1))
        request_delay = max(0.0, float(request_delay or 0.0))
        self._log("\n" + "═" * 58)
        self._log("[Translate] Начало перевода...")
        self._log(f"  Провайдер:  {provider}")
        self._log_model(provider, model)
        self._log(f"  Язык:       {source_lang} → {target_lang}")
        self._log(f"  Батч:       {batch_size} строк, потоков: {max_workers}, задержка: {request_delay}с")

        if not api_key.strip():
            return False, "[Translate] ✗ API ключ не указан."

        extract_dir = self.output_dir / "extracted"
        if not extract_dir.exists():
            return False, "[Translate] ✗ Сначала выполните Extract."

        json_files = sorted(f for f in extract_dir.glob("*.json") if not f.name.endswith("_translated.json"))
        if not json_files:
            return False, "[Translate] ✗ JSON-файлы не найдены. Сначала выполните Extract."

        # Engine profile is fixed to the engine recorded at Extract time.
        try:
            saved_index = json.loads((self.output_dir / "extract_index.json").read_text(encoding="utf-8"))
            game_type = saved_index.get("game_type") or self.get_game_type()
        except Exception:
            game_type = self.get_game_type()
        lock_ok, lock_msg = verify_manifest(self.output_dir, self.game_dir, game_type)
        if not lock_ok:
            return False, "[Translate] ✗ " + lock_msg
        self._log(f"[Translate] Engine profile: {game_type}")

        # Загрузка всех строк
        all_entries: list[dict] = []
        for jf in json_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    entries: list[dict] = json.load(f)
                all_entries.extend(entries)
            except Exception as e:
                self._log(format_exception_details(e, context=f"Pipeline.extract.read:{jf.name}"))

        self._log(f"[Translate] Файлов: {len(json_files)}, строк: {len(all_entries)}")

        # Карантин: экстрактор мог пометить часть строк как low-confidence
        # (сейчас — Wolf RPG: .wolf-таблицы БД, где эвристика не отличает
        # диалог от имени ресурса/анимации). Такие строки остаются в
        # extracted JSON нетронутыми (translated="") — просто не уходят
        # в AI-перевод по умолчанию, поэтому и не будут инъецированы
        # (все injector-ы пропускают пустой translated). include_low_confidence=True
        # снимает фильтр, если пользователь явно решил их тоже перевести.
        if not include_low_confidence:
            n_before = len(all_entries)
            all_entries = [e for e in all_entries if not e.get("_low_confidence")]
            n_quarantined = n_before - len(all_entries)
            if n_quarantined:
                self._log(
                    f"[Translate] ⚠ В карантине (не переводятся автоматически): {n_quarantined} строк. "
                    f"Это строки из .wolf-таблиц БД (вероятно, не диалог — имена анимаций/ресурсов "
                    f"и т.п.), перевод которых может сломать игру. Проверьте вручную в extracted/*.json "
                    f"или включите include_low_confidence=True, если уверены."
                )

        # Дедупликация
        from engine.deduplicator import build_dedup_map, apply_dedup_to_files
        dm = build_dedup_map(all_entries, self.log_cb)

        # Кэш / resume
        state = load_state(self.output_dir, self.log_cb)
        state.set_meta("provider", provider)
        state.set_meta("target_lang", target_lang)

        # Если часть *_translated.json уже существует, подхватываем их в кэш.
        resume_stats = self._sync_existing_translations(extract_dir, state)
        if resume_stats["files"]:
            self._log(
                f"[Translate] Автоскан: файлов перевода={resume_stats['files']}, "
                f"строк в них={resume_stats['entries']}, "
                f"добавлено в кэш={resume_stats['added']}, "
                f"обновлено правок={resume_stats['updated']}, "
                f"восстановлено кодов={resume_stats['repaired']}, "
                f"битых={resume_stats['broken']}"
            )

        # Глоссарий
        from engine.glossary import load_or_create_glossary
        glossary = load_or_create_glossary(self.output_dir, self.game_dir, self.log_cb)
        # Автодетект терминов из текста игры (имена персонажей, локации)
        from engine.glossary import extract_game_terms
        new_terms = extract_game_terms(all_entries, log_cb=self.log_cb)
        added = 0
        for term, _ in new_terms.items():
            if term not in glossary.terms:
                glossary.terms[term] = ""
                added += 1
        if added:
            self._log(f"[Translate] Новых терминов обнаружено: {added} — добавлены в глоссарий")

        glossary_block = glossary.to_prompt_block(source_lang, target_lang) if len(glossary) > 0 else ""
        if glossary_block:
            self._log(f"[Translate] Глоссарий: {len(glossary)} терминов в промпте")

        from ai_translator import TranslatorConfig, translate_texts

        config = TranslatorConfig(
            provider=provider,
            api_key=api_key,
            model=model,
            source_lang=source_lang,
            target_lang=target_lang,
            batch_size=batch_size,
            max_workers=max_workers,
            max_retries=max_retries,
            request_delay=request_delay,
            glossary_block=glossary_block,
            extra_instructions=extra_instructions,
            use_context=use_context,
            base_url=base_url,  # BUG FIX: was silently dropped — LM Studio/Ollama never worked
            use_global_tm=use_global_tm,
            game_type=game_type,
            style_profile=extra_instructions,
        )

        # Фильтруем уникальные ненереведённые строки
        unique_texts = dm.unique_texts()
        to_translate = [t for t in unique_texts if not state.has(t) and t]
        already_cached = len(unique_texts) - len(to_translate)

        self._log(
            f"[Translate] Автоскан: уникальных={len(unique_texts)}, "
            f"уже переведено={already_cached}, к переводу={len(to_translate)}"
        )

        # Автоопределение языка источника — только если не задан явно.
        # BUG FIX: ранее "Japanese" тоже попадал в auto-detect, что могло
        # перебить явный выбор пользователя (напр. для смешанных CJK игр).
        detected_source_lang = source_lang
        if source_lang in ("", "auto"):
            detected_source_lang = self._detect_source_language(all_entries) or "Japanese"
            self._log(f"[Translate] Авто-определён язык источника: {detected_source_lang}")

        config.source_lang = detected_source_lang

        # Оценка стоимости перевода
        try:
            from ai_translator import estimate_cost
            model_name = config.get_model()
            tokens, cost = estimate_cost(to_translate, model_name)
            if cost > 0:
                self._log(f"[Translate] 💰 Оценка стоимости: ~${cost:.4f} (~{tokens:,} токенов)")
            else:
                self._log(f"[Translate] 💰 Провайдер бесплатный (~{tokens:,} токенов)")
        except Exception:
            pass

        # Собираем контексты для строк (говорящий, тип строки)
        # Для каждой уникальной строки берём контекст первого вхождения
        text_to_ctx: dict[str, str] = {}
        text_to_speaker: dict[str, str] = {}
        for entry in all_entries:
            t   = entry.get("original", "")
            ctx = entry.get("context", "")
            if t and t not in text_to_ctx:
                text_to_ctx[t] = ctx
        # Ищем имена говорящих: приоритет — явное поле entry["speaker"]
        # (renpy_extractor.py/tyrano_extractor.py уже резолвят его через
        # char_names lookup — может отличаться от сырой переменной, это
        # единственный источник имени, когда context — просто "dialog"/
        # "narrator" без embedded-имени, как у Ren'Py). Fallback — парсинг
        # префикса "say:Имя"/"dialog:Имя" из context (Kirikiri/Tyrano, где
        # поля entry["speaker"] может не быть или context уже содержит то
        # же самое).
        # BUG FIX: раньше проверялся ТОЛЬКО context-префикс — entry["speaker"]
        # вычислялся экстрактором (Ren'Py: char_names.get(char_var, char_var))
        # и просто нигде не читался дальше, теряя более точное разрешённое
        # имя персонажа без единого предупреждения.
        for entry in all_entries:
            t   = entry.get("original", "")
            ctx = entry.get("context", "")
            speaker_field = entry.get("speaker", "")
            if not t or t in text_to_speaker:
                continue
            if speaker_field:
                text_to_speaker[t] = speaker_field
            elif ctx and (ctx.startswith("say:") or ctx.startswith("dialog:")):
                name = ctx.split(":", 1)[1].strip()
                if name:
                    text_to_speaker[t] = name

        # Строим enriched contexts для to_translate
        def _build_ctx(text: str) -> str:
            ctx = text_to_ctx.get(text, "")
            speaker = text_to_speaker.get(text, "")
            if speaker:
                return f"say:{speaker}"
            return ctx

        translate_contexts = [_build_ctx(t) for t in to_translate]

        was_cancelled = False
        fatal_error = ""
        result_failed = 0
        result_success = 0

        overall_total = len(unique_texts)
        done_before = already_cached
        if progress_callback:
            progress_callback(done_before, overall_total, -1.0, "")
            
        start_time = time.monotonic()

        def _overall_progress(done_now: int, remaining_total: int, eta_inner: float = -1.0, fname: str = "") -> None:
            if progress_callback:
                total_done = done_before + done_now
                elapsed = time.monotonic() - start_time
                eta = -1.0
                if done_now > 0 and elapsed > 0:
                    speed = done_now / elapsed
                    remaining = overall_total - total_done
                    eta = remaining / speed if speed > 0 else -1.0
                progress_callback(min(total_done, overall_total), overall_total, eta, fname)

        if to_translate:
            result = translate_texts(
                to_translate,
                config,
                self.log_cb,
                _overall_progress,
                state=state,
                cancel_event=cancel_event,
                contexts=translate_contexts,
            )
            was_cancelled = result.cancelled
            fatal_error = result.fatal_error

            # Adaptive batch size hint: если много ошибок — рекомендуем снизить батч
            result_failed  = sum(1 for r in result.results if not r.success)
            result_success = sum(1 for r in result.results if r.success)
            if result_failed > 0 and result_success > 0:
                fail_rate = result_failed / len(result.results)
                if fail_rate > 0.3 and config.batch_size > 5:
                    suggested = max(5, config.batch_size // 2)
                    self._log(
                        f"[Translate] ⚠ {fail_rate:.0%} ошибок — "
                        f"рекомендуется снизить Batch Size до {suggested} в настройках"
                    )

            for tr in result.results:
                if tr.success and tr.translated:
                    dm.set_translation(tr.original, tr.translated)
                    state.set(tr.original, tr.translated)

        # Добавляем из кэша
        for text in unique_texts:
            if not dm.get_translation(text) and state.has(text):
                dm.set_translation(text, state.get(text) or "")

        state.save()

        # Применить к файлам с постпроцессингом через глоссарий
        self._log("[Translate] Применение переводов к файлам...")
        applied = apply_dedup_to_files(dm, extract_dir, self.log_cb)

        # Применить глоссарий к переведённым файлам (постпроцессинг)
        if len(glossary) > 0:
            self._apply_glossary_postprocess(extract_dir, glossary, state.protected_originals())

        total_ok = dm.translated_count
        savings = dm.savings_pct

        # Validation pass — проверяем уже собранные переводы по нормализованным ключам.
        # BUG FIX: раньше этот блок стоял ПОСЛЕ `if was_cancelled: return`, то есть
        # при отмене перевода не выполнялся вовсе. Но переводы (в том числе с кодами,
        # которые могли быть испорчены, если cancel_event сработал прямо в середине
        # retry-цикла для конкретной строки) уже записаны в state и в файлы
        # *_translated.json строкой выше — отмена не откатывает apply_dedup_to_files.
        # Без этой проверки испорченные коды молча оставались и в кэше, и в файлах,
        # без единой попытки восстановления или предупреждения. Валидация должна
        # выполняться независимо от отмены, раз уж перевод уже применён к диску.
        translated_lookup = {
            text: (dm.get_translation(text) or state.get(text) or "")
            for text in unique_texts
        }
        self._validate_translations(
            translated_lookup,
            to_translate,
            detected_source_lang,
            state=state,
            extract_dir=extract_dir,
        )

        if was_cancelled:
            remaining = max(0, len(to_translate) - result_success)
            msg = (
                f"[Translate] ⏹ Прервано. Уже переведено {already_cached + result_success}/{len(unique_texts)} уникальных строк, "
                f"осталось непереведено {remaining}, применено к {applied} записям (прогресс сохранён)."
            )
            self._log(msg)
            self.last_translate_summary = {
                "unique": len(unique_texts), "translated": already_cached + result_success,
                "failed": result_failed, "remaining": remaining, "cancelled": True,
                "fatal_error": "",
            }
            return False, msg

        if fatal_error:
            # Постоянная ошибка API (ключ / модель / доступ): повторы бессмысленны, прогон
            # остановлен на ПЕРВОЙ такой ошибке. Уже полученное сохранено выше.
            remaining = max(0, len(to_translate) - result_success)
            msg = (
                f"[Translate] ⛔ Остановлено: {fatal_error}. "
                f"Переведено {already_cached + result_success}/{len(unique_texts)} уникальных строк "
                f"(прогресс сохранён). Исправьте ключ / модель в настройках и повторите — "
                f"переведённое берётся из кэша."
            )
            self._log(msg)
            self.last_translate_summary = {
                "unique": len(unique_texts), "translated": already_cached + result_success,
                "failed": result_failed, "remaining": remaining, "cancelled": False,
                "fatal_error": fatal_error,
            }
            return False, msg

        # BUG FIX: раньше при result_failed > 0 (часть строк не перевелась —
        # retry исчерпан, но was_cancelled=False, т.к. сам процесс не
        # прерывался) возвращалось success=True с сообщением "✓ Переведено
        # N/M", где несовпадение N и M нужно было заметить и вычесть самому
        # — число неудачных строк упоминалось только во ВТОРОЙ, отдельной
        # строке лога. Найдено при разборе похожей проблемы в стороннем
        # инструменте того же класса (там разработчик сам отметил в issues:
        # "Partial translation is not clearly indicated to the user after a
        # run"). Непереведённые строки не инжектятся молча (как и карантин
        # Wolf) — пользователь должен узнать об этом из ГЛАВНОГО сообщения,
        # а не только пересчитав числа.
        if result_failed > 0:
            msg = (
                f"[Translate] ⚠ Переведено {total_ok}/{len(unique_texts)} уникальных строк, "
                f"НЕ УДАЛОСЬ перевести {result_failed} (см. лог выше) — эти строки не будут "
                f"вставлены при Insert. Из кэша/перенесено={already_cached}, применено к {applied} записям "
                f"(экономия дедупликации: {savings}%)"
            )
        else:
            msg = (
                f"[Translate] ✓ Переведено {total_ok}/{len(unique_texts)} уникальных строк, "
                f"из кэша/перенесено={already_cached}, применено к {applied} записям "
                f"(экономия дедупликации: {savings}%)"
            )
        self._log(msg)
        self._log(f"[Translate] Отчёт: уникальных={len(unique_texts)}, из кэша={already_cached}, к переводу={len(to_translate)}, ошибок={result_failed}")
        self.last_translate_summary = {
            "unique": len(unique_texts), "translated": total_ok, "failed": result_failed,
            "remaining": max(0, len(unique_texts) - total_ok), "cancelled": False,
            "fatal_error": "",
        }
        return True, msg

    def polish(
        self,
        provider: str,
        api_key: str,
        model: str = "",
        target_lang: str = "Russian",
        batch_size: int = 10,
        max_retries: int = 3,
        request_delay: float = 0.5,
        extra_instructions: str = "",
        base_url: str = "",
        use_global_tm: bool = True,
        texts: list[str] | None = None,
        progress_callback: Callable[[int, int, float, str], None] | None = None,
        cancel_event=None,
        include_protected: bool = False,
    ) -> tuple[bool, str]:
        """
        AI-полировка уже переведённых строк: не переводит с нуля, а
        предлагает более естественную формулировку существующего перевода,
        сохраняя смысл, тон и escape-коды (см. ai_translator.polish_texts).

        texts=None — полирует ВСЕ уникальные строки проекта, у которых уже
        есть перевод. texts=[...] — полирует только указанные оригиналы
        (используется для точечной полировки выбранных строк из
        StringsViewer, а не всего проекта разом).

        Строки со статусом MANUAL_EDITED / APPROVED / LOCKED (см. engine/state.py)
        по умолчанию ПРОПУСКАЮТСЯ: полировка не должна молча переписывать
        правки человека. include_protected=True — явный запрос пользователя
        «полировать именно эти строки»: разрешает MANUAL_EDITED и APPROVED,
        но LOCKED не трогается никогда (сначала снимите блокировку).
        """
        batch_size = max(1, int(batch_size or 1))
        max_retries = max(1, int(max_retries or 1))
        request_delay = max(0.0, float(request_delay or 0.0))
        self._log("\n" + "═" * 58)
        self._log("[Polish] Начало полировки перевода...")
        self._log(f"  Провайдер:  {provider}")
        self._log_model(provider, model)

        if not api_key.strip():
            return False, "[Polish] ✗ API ключ не указан."

        extract_dir = self.output_dir / "extracted"
        if not extract_dir.exists():
            return False, "[Polish] ✗ Сначала выполните Extract."

        json_files = sorted(f for f in extract_dir.glob("*.json") if not f.name.endswith("_translated.json"))
        if not json_files:
            return False, "[Polish] ✗ JSON-файлы не найдены. Сначала выполните Extract."

        all_entries: list[dict] = []
        for jf in json_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    all_entries.extend(json.load(f))
            except Exception as e:
                self._log(format_exception_details(e, context=f"Pipeline.polish.read:{jf.name}"))

        from engine.deduplicator import build_dedup_map, apply_dedup_to_files
        dm = build_dedup_map(all_entries, self.log_cb)

        state = load_state(self.output_dir, self.log_cb)
        # Подхватываем ручные правки (StringsViewer) в кэш перед полировкой —
        # та же защита, что и в translate(): иначе полировка может взять
        # устаревшее значение из state вместо того, что реально лежит на диске.
        self._sync_existing_translations(extract_dir, state)

        unique_texts_list = dm.unique_texts()
        unique_texts = set(unique_texts_list)

        # BUG FIX: apply_dedup_to_files() ниже перестраивает *_translated.json
        # ЦЕЛИКОМ из dm — а dm.get_translation() пуст для всего, что явно не
        # проставлено через dm.set_translation(). При точечной полировке
        # (texts=[несколько строк]) без этого шага ВСЕ остальные уже
        # переведённые строки проекта, не входящие в texts, стирались бы
        # пустым переводом при перезаписи файлов. Прогреваем dm существующим
        # состоянием ДЛЯ ВСЕХ уникальных строк — так же, как это делает
        # translate() перед своим вызовом apply_dedup_to_files().
        for t in unique_texts_list:
            if not dm.get_translation(t) and state.has(t):
                dm.set_translation(t, state.get(t) or "")

        candidates = texts if texts is not None else unique_texts_list
        pairs: list[tuple[str, str]] = []
        protected_skipped = 0
        for t in candidates:
            if t not in unique_texts:
                continue  # игнорируем строки не из текущего extract
            current = dm.get_translation(t) or state.get(t) or ""
            if current:
                if state.is_protected(t) and not (
                    include_protected and state.status_of(t) != "locked"
                ):
                    protected_skipped += 1
                    continue
                pairs.append((t, current))

        if protected_skipped:
            self._log(
                f"[Polish] Пропущено защищённых строк "
                f"(ручные правки / одобрено / заблокировано): {protected_skipped}"
            )

        if not pairs:
            hint = " Все подходящие строки защищены от перезаписи." if protected_skipped else ""
            return False, "[Polish] ✗ Нет уже переведённых строк для полировки." + hint

        self._log(f"[Polish] К полировке: {len(pairs)} уникальных строк")

        from engine.glossary import load_or_create_glossary
        glossary = load_or_create_glossary(self.output_dir, self.game_dir, self.log_cb)
        glossary_block = glossary.to_prompt_block("", target_lang) if len(glossary) > 0 else ""

        from ai_translator import TranslatorConfig, polish_texts
        try:
            _idx = json.loads((self.output_dir / "extract_index.json").read_text(encoding="utf-8"))
            polish_game_type = _idx.get("game_type") or self.get_game_type()
        except Exception:
            polish_game_type = self.get_game_type()

        config = TranslatorConfig(
            provider=provider,
            api_key=api_key,
            model=model,
            target_lang=target_lang,
            batch_size=batch_size,
            max_retries=max_retries,
            request_delay=request_delay,
            glossary_block=glossary_block,
            extra_instructions=extra_instructions,
            base_url=base_url,
            use_global_tm=use_global_tm,
            game_type=polish_game_type,
            style_profile=extra_instructions,
        )

        result = polish_texts(
            pairs, config,
            log_callback=self.log_cb,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )

        applied = 0
        for r in result.results:
            if r.success and r.changed:
                # state.set() — единственный арбитр защиты: если строка защищена
                # (и пользователь явно не просил include_protected), запись
                # отклоняется и в файлы ничего не попадает.
                if state.set(r.original, r.after, source="ai", force=include_protected):
                    dm.set_translation(r.original, r.after)
                    applied += 1

        if applied:
            state.save()
            applied_files = apply_dedup_to_files(dm, extract_dir, self.log_cb)
            if len(glossary) > 0:
                self._apply_glossary_postprocess(extract_dir, glossary, state.protected_originals())
            self._log(f"[Polish] Применено к {applied_files} записям в файлах")

        was_cancelled = result.cancelled
        if result.fatal_error:
            msg = (
                f"[Polish] ⛔ Остановлено: {result.fatal_error}. Обработано "
                f"{result.succeeded}/{result.total}; исправьте ключ / модель в настройках и повторите."
            )
            self._log(msg)
            return False, msg
        msg = (
            f"[Polish] {'⏹ Прервано' if was_cancelled else '✓ Готово'}: "
            f"{result.succeeded}/{result.total} обработано, "
            f"улучшено={result.changed}, без изменений={result.succeeded - result.changed}, "
            f"пропущено={result.skipped}, ошибок={result.failed}"
        )
        self._log(msg)
        return (not was_cancelled), msg

    def _apply_glossary_postprocess(
        self,
        extract_dir: Path,
        glossary,
        protected: "frozenset[str] | set[str]" = frozenset(),
    ) -> None:
        """Применить глоссарий к переведённым строкам (постпроцессинг).

        BUG FIX: раньше вызывался glossary.apply(t) в цикле по каждой
        entry — apply() пересортировывает термины глоссария на каждый
        вызов (см. Glossary._sorted_terms()/apply_batch() в engine/glossary.py
        для замеров). Собираем непустые переводы файла в один список и
        обрабатываем их одним apply_batch() — сортировка происходит один
        раз на файл вместо одного раза на каждую строку.
        """
        count = 0
        for jf in extract_dir.glob("*_translated.json"):
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data: list[dict] = json.load(f)
                # Защищённые строки (ручные правки / одобрено / заблокировано)
                # глоссарием автоматически не переписываются.
                translatable = [
                    (entry, entry.get("translated", "")) for entry in data
                    if entry.get("translated", "")
                    and (entry.get("original") or "").strip() not in protected
                ]
                if not translatable:
                    continue
                corrected_list = glossary.apply_batch([t for _, t in translatable])
                changed = False
                for (entry, original_t), corrected in zip(translatable, corrected_list):
                    if corrected != original_t:
                        entry["translated"] = corrected
                        changed = True
                        count += 1
                if changed:

                    atomic_write_json(jf, data)
            except Exception as e:
                self._log(format_exception_details(e, context=f"Pipeline.glossary_post:{jf.name}"))
        if count:
            self._log(f"  [Glossary Post] ✓ Скорректировано строк по глоссарию: {count}")

    # ──────────────────────────────────────────────────────────
    # ШАГ 3: INJECT
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_game_relative(game_dir: Path, rel: str) -> Path | None:
        """Resolve an extractor source path to one unambiguous file under game_dir."""
        raw = Path(str(rel).replace("\\", "/").lstrip("/"))
        game_root = Path(game_dir).resolve()
        candidates = [game_dir / raw]
        for root in ("data", "Data", "www/data", "Assets", "StreamingAssets", "Resources", "game", "Localization", "Localizations"):
            candidates.append(game_dir / root / raw)
        found = []
        seen = set()
        for c in candidates:
            try:
                # BUG FIX (внешний аудит, тот же класс, что P1-3 в
                # project_lock.py): rel приходит из entry["source_path"]/
                # ["file"]/extract_index.json и только lstrip("/") —
                # "../evil.json" проходит невредимым. c.is_file() резолвит
                # ".." на уровне ОС и найдёт файл ВНЕ game_dir; эта функция
                # питает _make_inject_rollback (копирует найденный файл в
                # backup) и snapshot_json_targets (читает/хэширует его) —
                # то есть внешний файл мог попасть в бэкап или в safety-
                # сравнение. Ничего не пишет наружу напрямую (у инжекторов
                # своя логика поиска целевого файла), но чтение/копирование
                # чужого файла — уже реальная проблема. Явно проверяем
                # принадлежность game_root, как и в project_lock.py.
                if not c.resolve().is_relative_to(game_root):
                    continue
                if c.is_file():
                    k = str(c.resolve())
                    if k not in seen:
                        seen.add(k); found.append(c)
            except OSError:
                pass
        if not found and raw.name:
            try:
                for c in game_dir.rglob(raw.name):
                    if c.is_file() and not any(part.startswith("_backup_") for part in c.relative_to(game_dir).parts):
                        k = str(c.resolve())
                        if k not in seen:
                            seen.add(k); found.append(c)
                            if len(found) > 1:
                                break
            except OSError:
                pass
        return found[0] if len(found) == 1 else None

    def _collect_inject_sources(self, translated_files: list[Path]) -> dict[str, list[dict]]:
        """Build exact game-relative source -> translated entries map for safety/rollback."""
        index = {}
        try:
            index = json.loads((self.output_dir / "extract_index.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        source_map = index.get("sources", {}) if isinstance(index, dict) else {}
        out: dict[str, list[dict]] = {}
        for tf in translated_files:
            try:
                rows = json.loads(tf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            fallback = tf.stem[:-len("_translated")] if tf.stem.endswith("_translated") else tf.stem
            indexed = source_map.get(fallback) or source_map.get(fallback + ".json")
            for e in rows:
                if not isinstance(e, dict) or not e.get("translated"):
                    continue
                rel = e.get("source_path") or e.get("_source_path") or e.get("file") or indexed
                if not isinstance(rel, str) or not rel.strip():
                    continue
                resolved = self._resolve_game_relative(self.game_dir, rel)
                if resolved is None:
                    continue
                actual = resolved.relative_to(self.game_dir).as_posix()
                out.setdefault(actual, []).append(e)
        return out

    def _make_inject_rollback(self, source_paths: list[Path], protected_paths: list[Path]):
        """Create a real transactional snapshot using independent file copies.

        Hardlinks are deliberately NOT used: an injector that writes in-place
        would mutate the rollback copy too, defeating the safety guarantee.
        This snapshot is correctness-first; speed is secondary to preserving
        the original game byte-for-byte.
        """
        root = Path(tempfile.mkdtemp(prefix="rpgmtl_inject_rollback_"))
        manifest = []
        paths = []
        seen = set()
        for p in source_paths + protected_paths:
            try:
                p = p.resolve()
            except OSError:
                continue
            if not p.is_file() or p in seen:
                continue
            seen.add(p); paths.append(p)
        for p in paths:
            try:
                rel = p.relative_to(self.game_dir.resolve())
            except ValueError:
                continue
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            mode = "copy"
            manifest.append({"relative": rel.as_posix(), "mode": mode})
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return root

    def _rollback_inject(self, rollback_dir: Path) -> tuple[bool, list[str]]:
        """Restore every snapshotted file and remove newly-created protected/source files.

        BUG FIX (P1-2, внешний аудит, подтверждено чтением кода): раньше эта
        функция ничего не возвращала (-> None), и все 4 места её вызова
        безусловно логировали "[Rollback] ✓ Игра восстановлена ..." сразу
        после вызова — даже если КОНКРЕТНЫЙ файл не восстановился (per-file
        ошибка уже логировалась отдельным ✗-сообщением чуть выше, но общий
        итог всё равно объявлялся успешным). Пользователь мог увидеть
        противоречивую пару сообщений и поверить, что игра полностью цела,
        хотя часть файлов осталась в изменённом/промежуточном состоянии.
        Теперь возвращаем (all_ok, failed_relative_paths) — вызывающий код
        сам решает, что логировать.
        """
        try:
            manifest = json.loads((rollback_dir / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            return False, ["<manifest.json>"]
        failed: list[str] = []
        for item in manifest:
            rel = str(item.get("relative", ""))
            if not rel:
                continue
            src = rollback_dir / rel
            dst = self.game_dir / rel
            if not src.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError as e:
                self._log(f"[Rollback] ✗ Не удалось восстановить {rel}: {e}")
                failed.append(rel)
        # Do not recursively delete unrelated game files. We only remove a file
        # if it is a protected/source path we explicitly snapshotted and it did
        # not exist before; new-file detection is handled by safety and is logged.
        return not failed, failed

    def _log_rollback_result(self, ok: bool, failed: list[str], context: str) -> None:
        """Единая, честная формулировка итога rollback — см. BUG FIX в _rollback_inject."""
        if ok:
            self._log(f"[Rollback] ✓ Игра восстановлена {context}.")
        else:
            self._log(
                f"[Rollback] ✗ ЧАСТИЧНЫЙ откат {context}: не удалось восстановить "
                f"{len(failed)} файл(ов): {', '.join(failed[:5])}"
                f"{' ...' if len(failed) > 5 else ''}. "
                "Игра может быть в смешанном состоянии — оригиналы всё ещё "
                "лежат в папке резервной копии (_backup_original) для ручного "
                "восстановления."
            )

    def inject(
        self,
        apply_warp: bool = True,
        max_line_width: int = 400,
        progress_callback: Callable[[int, int, float, str], None] | None = None,
        backup: bool = True,
    ) -> tuple[bool, str]:
        def p_wrap(c: int, t: int, eta: float = -1.0, fname: str = ""):
            if progress_callback:
                progress_callback(c, t, eta, fname)
                
        self._log("\n" + "═" * 58)
        self._log("[Inject] Начало вставки перевода...")
        self._log(f"  Игра:       {self.game_dir}")
        self._log(f"  Переводы:   {self.output_dir}")
        warp_str = f"да ({max_line_width}px)" if apply_warp else "нет"
        self._log(f"  SmartWarp:  {warp_str}")

        ok, err = self.check_game_dir()
        if not ok:
            self._log(f"[Inject] ✗ {err}")
            return False, err

        extract_dir = self.output_dir / "extracted"
        translated_files = list(extract_dir.glob("*_translated.json")) if extract_dir.exists() else []
        if not translated_files:
            msg = "[Inject] ✗ Файлы с переводом не найдены. Сначала Extract → Translate."
            self._log(msg)
            return False, msg

        # Animation/asset safety snapshot MUST happen before SmartWarp or any injector write.
        # It covers binary/media assets and exact JSON paths outside the translated fields.
        safety_before_assets = {}
        safety_before_json = {}
        rollback_dir = None
        entries_by_source = {}
        try:
            from engine.asset_safety import snapshot_protected_assets, snapshot_json_targets
            safety_before_assets = snapshot_protected_assets(self.game_dir)
            entries_by_source = self._collect_inject_sources(translated_files)
            safety_before_json = snapshot_json_targets(self.game_dir, entries_by_source)
            source_paths = []
            for rel in entries_by_source:
                pth = self.game_dir / rel
                if pth.is_file(): source_paths.append(pth)
            protected_paths = [self.game_dir / rel for rel in safety_before_assets]
            rollback_dir = self._make_inject_rollback(source_paths, protected_paths)
            self._log(f"[Safety] Защищено ресурсов: {len(safety_before_assets)}, JSON-структур: {len(safety_before_json)}; транзакционный rollback готов")
        except Exception as e:
            msg = f"[Inject] ✗ Не удалось создать Safety Snapshot: {e}"
            self._log(msg)
            return False, msg

        # Cross-game protection MUST happen before SmartWarp or any injector write.
        saved_index = {}
        try:
            saved_index = json.loads((self.output_dir / "extract_index.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        locked_type = saved_index.get("game_type") or self.get_game_type()
        lock_ok, lock_msg = verify_manifest(self.output_dir, self.game_dir, locked_type)
        if not lock_ok:
            msg = "[Inject] ✗ " + lock_msg
            self._log(msg)
            return False, msg

        # BUG FIX: раньше здесь была свежая, живая детекция (self.get_game_type())
        # — если файлы игры менялись между Extract и Inject настолько, что
        # детектор теперь видит ДРУГОЙ движок (законный сценарий — см.
        # detect_game_type_verbose и его multi-match репортинг), Inject мог
        # применить инжектор ОДНОГО движка к данным, извлечённым ДРУГИМ.
        # Форматы path/source_path несовместимы между движками (Unity —
        # "source_path", Godot — "_source_path", Wolf — байтовые смещения,
        # RPG Maker — JSON key path) — в лучшем случае инжектор просто не
        # найдёт совпадений и молча ничего не вставит, в худшем — по
        # случайному совпадению попытается интерпретировать чужой формат.
        # Теперь используем ТОТ ЖЕ game_type, что сохранён в
        # extract_index.json при Extract — переводы вставляются той же
        # логикой, что их извлекла, независимо от того, что говорит живой
        # детектор прямо сейчас.
        game_type = None
        extract_index_path = self.output_dir / "extract_index.json"
        if extract_index_path.exists():
            try:
                saved_index = json.loads(extract_index_path.read_text(encoding="utf-8"))
                game_type = saved_index.get("game_type") or None
            except Exception:
                game_type = None
        if game_type:
            self._log(f"[Inject] Тип игры: {game_type.upper()} (сохранён при Extract)")
        else:
            game_type = self.get_game_type()
            self._log(
                f"[Inject] ⚠ Не найден game_type из Extract (extract_index.json "
                f"отсутствует/повреждён) — определяю заново: {game_type.upper()}. "
                f"Если игра изменилась с момента Extract, результат может быть неточным — "
                f"рекомендуется повторить Extract."
            )

        # Create the user-visible original backup before any QA/SmartWarp/inject
        # work when requested. This is not used as a substitute for the
        # transaction rollback snapshot: it is the durable recovery copy used by
        # project_lock and the engine injectors on later runs.
        if backup:
            try:
                backup_dir = self.game_dir / ("_backup_unity_original" if game_type == "unity" else "_backup_original")
                backup_dir.mkdir(parents=True, exist_ok=True)
                for rel in entries_by_source:
                    src = self.game_dir / rel
                    if not src.is_file():
                        continue
                    dst = backup_dir / rel
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                    # RPG Maker's legacy injector and project_lock both support
                    # flat Data/*.json backups. Keep that compatibility copy so
                    # older recovery code and external tools can still restore
                    # the original files without understanding nested paths.
                    if game_type in {"rpgmv", "rpgmz"} and src.suffix.lower() == ".json":
                        flat = backup_dir / src.name
                        if not flat.exists():
                            shutil.copy2(src, flat)
                self._log(f"[Backup] ✓ Оригиналы защищены в {backup_dir}")
            except Exception as e:
                if rollback_dir:
                    self._rollback_inject(rollback_dir)
                    shutil.rmtree(rollback_dir, ignore_errors=True)
                msg = f"[Inject] ✗ Не удалось создать резервную копию оригиналов: {e}"
                self._log(msg)
                return False, msg

        # BUG FIX: Smart Warp применялся ТОЛЬКО для MV/MZ (else-ветка).
        # _WARP_CONTEXTS уже содержит wolf, vxace, kirikiri, renpy, unity, godot —
        # но они никогда не получали warp. Применяем ДО инъекции для всех движков.
        #
        # Исключение: Wolf и VXAce используют БИНАРНЫЙ инжект — движок
        # переносит текст сам по ширине окна. Добавление \n сломает дисплей.
        # Safety gate: critical localization/token errors are never injected.
        try:
            from qa_report import run_qa_report
            qa = run_qa_report(extract_dir)
            if qa.get("unreadable_files"):
                # Fail-closed: файл, который QA не смог прочитать, НЕ проверен —
                # «критических нет» про него ничего не значит.
                names = ", ".join(u["file"] for u in qa["unreadable_files"][:5])
                msg = (
                    f"[Inject] ✗ Inject остановлен: QA не смог прочитать "
                    f"{len(qa['unreadable_files'])} файл(ов) с переводом ({names}) — "
                    "они не проверены. Исправьте или удалите повреждённые файлы."
                )
                self._log(msg)
                return False, msg
            if qa.get("critical_count", 0) > 0:
                msg = (
                    f"[Inject] ✗ Inject остановлен: QA обнаружил {qa['critical_count']} "
                    "критических проблем с техническими токенами/плейсхолдерами."
                )
                self._log(msg)
                return False, msg
            self._log(
                f"[Inject] QA перед Inject: {qa.get('checked', 0)} строк, "
                f"critical=0, warnings={qa.get('warning_count', 0)}"
            )
        except Exception as e:
            msg = f"[Inject] ✗ Обязательная QA-проверка не выполнена: {e}"
            self._log(msg)
            return False, msg

        # BUG FIX (внешний аудит, подтверждено чтением injector-кода):
        # Smart Warp вставляет РЕАЛЬНЫЙ символ переноса строки (line_separator="\n"
        # в _apply_warp). Ren'Py (_requote) и Godot (свой quote-хелпер) уже
        # экранируют \n → \\n перед записью — им ничего не грозит. Но
        # tyrano_injector.py и kirikiri_injector.py склеивают translated
        # НАПРЯМУЮ в одну строку массива lines[idx] без какого-либо
        # экранирования: `lines[idx] = new_line + eol` — сырой \n внутри
        # translated физически разрывает один .ks/.ks-подобный script line на
        # несколько, что может сломать построчный KAG-парсинг игры. Это было
        # менее вероятно, пока был жёсткий max_lines=4 (см. BUG FIX в
        # smart_warp.wrap_paragraph) — после его снятия длинные переводы
        # переносятся на много строк, то есть шанс вставить \n в такой файл
        # вырос, а не уменьшился. Пока их injector'ы не научатся сами
        # экранировать перенос (как Ren'Py/Godot), Warp для них выключен —
        # безопаснее не переносить вообще, чем рисковать порчей скрипта.
        _WARP_SKIP_ENGINES = {"wolf", "vxace", "tyrano", "kirikiri"}
        if apply_warp and game_type not in _WARP_SKIP_ENGINES:
            self._log(f"[Inject] Применение Smart Warp ({max_line_width}px)...")
            self._apply_warp(translated_files, max_line_width)
        elif apply_warp and game_type in _WARP_SKIP_ENGINES:
            self._log(
                f"[Inject] Smart Warp пропущен: для {game_type} формат построчный, "
                "перенос может разорвать script-синтаксис (нет escape-механизма "
                "для встроенного \\n, в отличие от Ren'Py/Godot)."
            )

        try:
            if game_type == "wolf":
                from engine.wolf_injector import inject_wolf
                summary = inject_wolf(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    backup=backup,
                    progress_callback=p_wrap,
                )
            elif game_type == "vxace":
                from engine.vxace_injector import inject_vxace
                summary = inject_vxace(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    backup=backup,
                    progress_callback=p_wrap,
                )
            elif game_type == "kirikiri":
                from engine.kirikiri_injector import inject_kirikiri
                summary = inject_kirikiri(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    backup=backup,
                    progress_callback=p_wrap,
                )
            elif game_type == "tyrano":
                from engine.tyrano_injector import inject_tyrano
                summary = inject_tyrano(
                    self.game_dir, self.output_dir,
                    log_callback=self.log_cb,
                    backup=backup,
                    progress_callback=p_wrap,
                )
            elif game_type == "renpy":
                from renpy_injector import inject_renpy
                # BUG FIX: was passing extract_dir; all other engines get output_dir.
                # renpy_injector now correctly resolves output_dir/"extracted" internally.
                summary = inject_renpy(
                    self.game_dir, self.output_dir, self.log_cb,
                    backup=backup, progress_callback=p_wrap,
                )
            elif game_type == "unity":
                from engine.unity_injector import inject_unity
                # BUG FIX: was passing extract_dir; standardize to output_dir
                summary = inject_unity(
                    self.game_dir, self.output_dir, self.log_cb,
                    backup=backup, progress_callback=p_wrap,
                )
            elif game_type == "godot":
                from engine.godot_injector import inject_godot
                summary = inject_godot(
                    self.game_dir, self.output_dir, self.log_cb,
                    backup=backup, progress_callback=p_wrap,
                )
            else:
                from engine.injector import inject_translations
                # inject_translations already handles both output_dir and output_dir/extracted
                summary = inject_translations(
                    self.game_dir, self.output_dir, self.log_cb,
                    backup=backup, progress_callback=p_wrap,
                )

            injected = summary.get("total_injected", 0)
            errors = summary.get("total_errors", 0)
            files_mod = summary.get("files_modified", 0)

            # Post-inject integrity gate: animation/media/binary assets must be byte-identical,
            # and JSON resources may differ only at paths explicitly extracted for translation.
            from engine.asset_safety import compare_protected_assets, compare_json_targets
            changed_assets = compare_protected_assets(self.game_dir, safety_before_assets)
            changed_json = compare_json_targets(self.game_dir, entries_by_source, safety_before_json)
            if changed_assets or changed_json:
                details = []
                if changed_assets:
                    details.append("ресурсы: " + ", ".join(changed_assets[:5]))
                if changed_json:
                    details.append("JSON вне разрешённых путей: " + ", ".join(changed_json[:5]))
                if rollback_dir:
                    ok, failed = self._rollback_inject(rollback_dir)
                    self._log_rollback_result(ok, failed, "после провала Safety Guard")
                msg = "[Inject] ✗ Safety Guard остановил результат и откатил изменения: обнаружены изменения вне переводимого текста (" + "; ".join(details) + ")."
                self._log(msg)
                return False, msg
            if errors:
                if rollback_dir:
                    ok, failed = self._rollback_inject(rollback_dir)
                    self._log_rollback_result(
                        ok, failed,
                        "— injector сообщил об ошибках, поэтому частичный Inject не оставлен",
                    )
                msg = f"[Inject] ✗ Вставка отменена: injector сообщил {errors} ошибок. Частичный результат откатан."
                self._log(msg)
                return False, msg
            self._log("[Safety] ✓ Анимации/медиа/бинарные ресурсы не изменены; JSON изменён только в разрешённых текстовых путях.")
            if rollback_dir:
                shutil.rmtree(rollback_dir, ignore_errors=True)
                rollback_dir = None

            if errors == 0:
                msg = f"[Inject] ✓ Вставлено {injected} строк в {files_mod} файлов."
            else:
                msg = f"[Inject] ⚠ Вставлено {injected} строк, ошибок: {errors}."
            self._log(msg)
            return True, msg
        except Exception as e:
            if rollback_dir:
                ok, failed = self._rollback_inject(rollback_dir)
                self._log_rollback_result(ok, failed, "после исключения во время Inject")
                shutil.rmtree(rollback_dir, ignore_errors=True)
            self._log(format_exception_details(e, context="Pipeline.inject"))
            msg = f"[Inject] ✗ Ошибка: {e}"
            self._log(msg)
            return False, msg

    # BUG FIX: старый _WARP_CONTEXTS содержал "kirikiri_text", "renpy_dialog" etc.,
    # но экстракторы НИКОГДА этих значений не производят.
    # Реальные значения: kirikiri → "dialog:Name", renpy → char_var/"dialog"/"narrator",
    # vxace → "dialog_text"/"actor:name", godot → "godot_{prop}".
    # Заменяем frozenset на функцию с prefix-matching.
    @staticmethod
    def _should_warp(ctx: str) -> bool:
        """Определить нужен ли Smart Warp для данного context-значения."""
        if not ctx:
            return False
        # Точные совпадения (MV/MZ, Wolf, VXAce dialog, Unity)
        _EXACT = frozenset({
            "dialog_text", "scroll_text", "choice",  # MV/MZ
            "wolf_dialog", "wolf_string",             # Wolf
            "unity_text",                             # Unity
            "dialog", "narrator",                    # RenPy generic
            "godot_dialog_choice", "godot_dialog_narration",  # Godot Dialogic
        })
        if ctx in _EXACT:
            return True
        # Prefix-based: "dialog:Alice" → Kirikiri/RenPy speaker
        # "say:Name" → Tyrano/Godot speaker
        # "godot_*" → Godot props (только text-типы)
        if ctx.startswith(("dialog:", "say:", "renpy_")):
            return True
        if ctx.startswith("godot_") and any(
            s in ctx for s in ("text", "dialog", "bbcode")
        ):
            return True
        # RenPy: char variable becomes context (e.g. "alice", "bob")
        # Heuristic: short lowercase alpha = speaker variable
        if ctx.isalpha() and ctx.islower() and len(ctx) <= 24:
            return True
        return False

    def _apply_warp(self, files: list[Path], max_width: int) -> None:
        try:
            from smart_warp import wrap_paragraph, DEFAULT_MAX_LINES
            from font_analyzer import load_font_table

            table = load_font_table()
            warped = 0
            skipped_ctx = 0
            overflow_examples: list[str] = []
            for fpath in files:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data: list[dict] = json.load(f)
                    changed = False
                    for entry in data:
                        t = entry.get("translated", "")
                        if not t:
                            continue
                        ctx = entry.get("context", "")
                        if not self._should_warp(ctx):
                            skipped_ctx += 1
                            continue
                        # BUG FIX: max_lines больше НЕ передаётся (раньше
                        # молча наследовался опасный дефолт 4 и обрезал
                        # длинные переводы с потерей текста — см. BUG FIX в
                        # smart_warp.wrap_paragraph). Перенос теперь никогда
                        # не удаляет контент; если результат длиннее, чем
                        # обычно помещается в окно сообщения, это только
                        # логируется как предупреждение для ручной проверки.
                        wrapped = wrap_paragraph(t, max_width_px=max_width,
                                                 table=table, line_separator="\n")
                        if wrapped != t:
                            entry["translated"] = wrapped
                            changed = True
                            warped += 1
                        line_count = wrapped.count("\n") + 1
                        if line_count > DEFAULT_MAX_LINES and len(overflow_examples) < 20:
                            overflow_examples.append(f"{fpath.name}: {line_count} строк")
                    if changed:
                        # BUG FIX: прямой open("w") при прерывании обнулял _translated.json
                        content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                        fd, tmp = tempfile.mkstemp(dir=fpath.parent, suffix=".tmp", prefix=".rpgmtl_")
                        try:
                            with os.fdopen(fd, "wb") as f:
                                f.write(content)
                            os.replace(tmp, fpath)
                        except Exception:
                            try: os.unlink(tmp)
                            except Exception: pass
                            raise
                except Exception as e:
                    self._log(format_exception_details(e, context=f"Pipeline.warp:{fpath}"))
            self._log(
                f"  [Warp] ✓ Перенос применён к {warped} диалоговым строкам "
                f"(не-диалоговых пропущено: {skipped_ctx})."
            )
            if overflow_examples:
                self._log(
                    f"  [Warp] ⚠ {len(overflow_examples)}+ строк(и) вышли за "
                    f"{DEFAULT_MAX_LINES} строки после переноса (текст НЕ обрезан, "
                    f"но может не поместиться в стандартное окно сообщения) — "
                    f"стоит проверить вручную: " + "; ".join(overflow_examples[:5])
                    + (" ..." if len(overflow_examples) > 5 else "")
                )
        except ImportError as e:
            self._log(format_exception_details(e, context="Pipeline.warp.import"))

    # ──────────────────────────────────────────────────────────
    # УТИЛИТЫ
    # ──────────────────────────────────────────────────────────
    def _detect_source_language(self, entries: list[dict]) -> str:
        """Определить язык источника по первым строкам."""
        sample = [e.get("original","") for e in entries[:50] if e.get("original")]
        text = " ".join(sample)
        total = len(text) or 1

        # Separate ranges: kana (Japanese-only), Hangul (Korean), CJK ideographs (Chinese+Japanese)
        kana = sum(1 for ch in text if 0x3040 <= ord(ch) <= 0x30FF)   # hiragana + katakana
        kor  = sum(1 for ch in text if 0xAC00 <= ord(ch) <= 0xD7AF)   # Hangul syllables
        cjk  = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)   # CJK ideographs

        if kor / total > 0.05:
            return "Korean"
        # If kana is present, it's Japanese (Chinese has no kana)
        if kana / total > 0.02:
            return "Japanese"
        if cjk / total > 0.1:
            return "Chinese"
        return "Japanese"  # дефолт для игр

    def _validate_translations(
        self,
        translated_map: dict,
        originals: list,
        source_lang: str,
        state=None,
        extract_dir: "Path | None" = None,
    ):
        """Проверить качество переводов, залогировать проблемы.
        BUG FIX: Persist repaired translations back to state and *_translated.json files."""
        src_lang = source_lang.lower()

        from ai_translator import _validate_codes, _repair_codes
        untranslated = 0
        code_broken  = 0
        source_left  = 0
        repaired_count = 0
        protected_broken = 0
        repaired_originals: list[str] = []

        check_source = "japanese" in src_lang or "ja" in src_lang

        for orig in originals:
            translated = translated_map.get(orig, "")
            if not translated:
                untranslated += 1
                continue
            if not _validate_codes(orig, translated):
                if state is not None and state.is_protected(orig):
                    # Текст человека автоматически не переписываем — только
                    # сообщаем (QA/inject всё равно его проверят).
                    code_broken += 1
                    protected_broken += 1
                else:
                    fixed = _repair_codes(orig, translated)
                    if _validate_codes(orig, fixed):
                        translated_map[orig] = fixed
                        translated = fixed            # Fix 141: keep local var in sync
                        repaired_originals.append(orig)
                        repaired_count += 1
                    else:
                        code_broken += 1
            if check_source and _has_cjk(translated):
                source_left += 1

        # BUG FIX: Persist repaired translations to state cache
        if repaired_originals and state is not None:
            for orig in repaired_originals:
                state.set(orig, translated_map[orig], source="system")
            state.save()

        # BUG FIX: Persist repaired translations to *_translated.json files
        if repaired_originals and extract_dir is not None and extract_dir.exists():
            repair_map = {orig: translated_map[orig] for orig in repaired_originals}
            repair_count_files = 0
            for jf in extract_dir.glob("*_translated.json"):
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        data: list[dict] = json.load(f)
                    changed = False
                    for entry in data:
                        orig = (entry.get("original") or "").strip()
                        if orig in repair_map:
                            entry["translated"] = repair_map[orig]
                            changed = True
                    if changed:
                        atomic_write_json(jf, data)
                        repair_count_files += 1
                except Exception:
                    pass
            if repair_count_files:
                self._log(f"[Validate] 🔧 Исправления сохранены в {repair_count_files} файлах")

        if untranslated:
            self._log(f"[Validate] ⚠ Непереведено: {untranslated} строк")
        if repaired_count:
            self._log(f"[Validate] 🔧 Авто-восстановлены escape-коды: {repaired_count} строк")
        if code_broken:
            self._log(f"[Validate] ⚠ Сломаны escape-коды: {code_broken} строк")
        if protected_broken:
            self._log(
                f"[Validate] ⚠ Из них ручные/одобренные/заблокированные правки "
                f"(НЕ изменены автоматически): {protected_broken} — проверьте вручную"
            )
        if source_left:
            self._log(f"[Validate] ⚠ Остался исходный язык: {source_left} строк")
        if not (untranslated or code_broken or source_left):
            self._log("[Validate] ✓ Все проверки пройдены")

    def _untranslated_report(self, all_entries: list[dict], state) -> int:
        """Посчитать непереведённые строки и залогировать."""
        untranslated = [e for e in all_entries
                        if e.get("original") and not state.has(e.get("original", ""))]
        if untranslated:
            self._log(f"[Report] Непереведённых строк: {len(untranslated)}")
        return len(untranslated)

    def get_stats(self) -> dict:
        """Получить статистику текущего состояния перевода.

        BUG FIX #1 (TOCTOU): ранее exists() + stat() не были атомарны.
        Между двумя вызовами директория могла исчезнуть → OSError.
        Теперь stat() обёрнут в try/except.

        BUG FIX #2 (кэш): UI каждые 3с создаёт новый Pipeline, у которого
        _stats_cache=None, поэтому кэш никогда не работал. Теперь кэш хранится
        на уровне класса, ключ — output_dir.

        BUG FIX #3: state._cache — закрытый атрибут. Используем state.items().
        """
        extract_dir = self.output_dir / "extracted"
        state_file = self.output_dir / "translation_state.json"

        def _safe_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        cache_key = (_safe_mtime(extract_dir), _safe_mtime(state_file))

        # Кэш на уровне класса, чтобы работал даже при каждом new Pipeline()
        _cls_cache = Pipeline._stats_class_cache  # type: ignore[attr-defined]
        cached = _cls_cache.get(str(self.output_dir))
        if cached and cached[0] == cache_key:
            # stale_extract троттлится ОТДЕЛЬНО от этого кэша (см. класс-атрибуты
            # выше) — иначе он бы никогда не обновлялся между Extract/Translate.
            stats = dict(cached[1])
            stats["stale_extract"] = self._check_stale_extract_throttled()
            return stats

        stats: dict = {
            "extract_done": False,
            "total_strings": 0,
            "unique_strings": 0,
            "translated_strings": 0,
            "cached_strings": 0,
            "protected_strings": 0,   # ручные правки / одобренные / заблокированные
            "files": 0,
            "stale_extract": False,
            "parse_errors": 0,
        }
        if not extract_dir.exists():
            _cls_cache[str(self.output_dir)] = (cache_key, stats)
            return stats
        stats["extract_done"] = True

        src_files = [f for f in extract_dir.glob("*.json")
                     if not f.name.endswith("_translated.json")]
        tr_files = list(extract_dir.glob("*_translated.json"))
        stats["files"] = len(src_files)

        unique_originals: set[str] = set()
        translated_originals: set[str] = set()
        # BUG FIX: раньше файлы, которые не распарсились (битый JSON,
        # проблема кодировки, гонка с одновременной записью), молча
        # пропускались здесь — total_strings/unique_strings оказывались
        # тихо занижены, без единого признака в интерфейсе, что что-то не
        # так. Пользователь мог решить, что перевод просто "неполный", хотя
        # реальная причина — файл, который вообще не удалось прочитать.
        # Считаем это явно и возвращаем как часть статистики.
        parse_errors = 0

        for jf in src_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stats["total_strings"] += len(data)
                for entry in data:
                    orig = entry.get("original", "").strip()
                    if orig:
                        unique_originals.add(orig)
            except Exception:
                parse_errors += 1
        stats["unique_strings"] = len(unique_originals)

        for jf in tr_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data:
                    orig = entry.get("original", "").strip()
                    if orig and entry.get("translated", "").strip():
                        translated_originals.add(orig)
            except Exception:
                parse_errors += 1
        stats["parse_errors"] = parse_errors

        state = load_state(self.output_dir, log_cb=lambda _: None)
        stats["cached_strings"] = state.count
        stats["protected_strings"] = len(state.protected_originals())
        # BUG FIX #3: публичный API вместо state._cache
        translated_originals.update(k for k, v in state.items() if v)
        stats["translated_strings"] = len(translated_originals)
        stats["remaining_strings"] = max(stats["unique_strings"] - stats["translated_strings"], 0)

        # ── Per-file quality: топ файлов с наибольшим количеством непереведённых ──
        per_file_stats: list[dict] = []
        for jf in src_files:
            tr_path = extract_dir / (jf.stem + "_translated.json")
            if not tr_path.exists():
                continue
            try:
                src_data = json.loads(jf.read_text(encoding="utf-8"))
                tr_data  = json.loads(tr_path.read_text(encoding="utf-8"))
                total_f = len(src_data)
                done_f  = sum(
                    1 for e in tr_data
                    if e.get("translated", "").strip()
                )
                pct = round(done_f / total_f * 100) if total_f else 0
                per_file_stats.append({
                    "file":  jf.stem,
                    "total": total_f,
                    "done":  done_f,
                    "pct":   pct,
                })
            except Exception:
                pass
        per_file_stats.sort(key=lambda x: x["pct"])  # наименее переведённые первые
        stats["per_file"] = per_file_stats[:20]       # только топ-20 для диагностики

        _cls_cache[str(self.output_dir)] = (cache_key, stats)
        # BUG FIX: trim cache to 20 most-recent entries to prevent unbounded growth
        # when the user processes many different output directories in one session.
        if len(_cls_cache) > 20:
            oldest_key = next(iter(_cls_cache))
            _cls_cache.pop(oldest_key, None)
        # Также обновляем instance-кэш для обратной совместимости
        self._stats_cache = (cache_key, stats)

        # stale_extract троттлится отдельно (см. _check_stale_extract_throttled) —
        # добавляем ПОСЛЕ записи в _cls_cache, чтобы кэш статистики не «протухал»
        # каждый раз когда троттл истекает, а stale_extract при этом обновлялся
        # независимо от него на каждый вызов get_stats().
        result = dict(stats)
        result["stale_extract"] = self._check_stale_extract_throttled()
        return result

    def qa_report(
        self,
        length_ratio: float = 3.0,
        progress_callback: Callable[[int, int, float, str], None] | None = None,
    ) -> dict:
        """
        Прогнать QA-проверки (коды/плейсхолдеры, числа, длина) по всем
        *_translated.json в output_dir/extracted. Не блокирует Inject —
        это отдельный, вызываемый вручную (или перед Inject) шаг, чтобы
        находить подозрительные переводы по отчёту, а не постфактум в игре.
        Сохраняет полный отчёт в output_dir/qa_report.json и логирует
        краткую сводку + до 50 находок.
        """
        from qa_report import run_qa_report, format_qa_report_text

        extract_dir = self.output_dir / "extracted"
        report = run_qa_report(extract_dir, length_ratio=length_ratio,
                               progress_callback=progress_callback)

        try:
            atomic_write_json(self.output_dir / "qa_report.json", report)
        except Exception as e:
            self._log(format_exception_details(e, context="Pipeline.qa_report.save"))

        self._log(format_qa_report_text(report))
        return report

    def _check_stale_extract_throttled(self) -> bool:
        """
        Троттлированная обёртка над _compute_stale_extract().

        Полное сканирование game_dir дорого для больших игр (тысячи ассетов),
        а get_stats() дёргается UI каждые 4с. Поэтому реальный скан происходит
        не чаще раза в _STALE_CHECK_THROTTLE_SEC секунд — между сканами
        возвращается последнее известное значение.
        """
        key = str(self.output_dir)
        now = time.monotonic()
        cached = Pipeline._stale_check_cache.get(key)
        if cached is not None and (now - cached[0]) < Pipeline._STALE_CHECK_THROTTLE_SEC:
            return cached[1]

        is_stale = self._compute_stale_extract()
        Pipeline._stale_check_cache[key] = (now, is_stale)
        if len(Pipeline._stale_check_cache) > 20:
            oldest_key = next(iter(Pipeline._stale_check_cache))
            Pipeline._stale_check_cache.pop(oldest_key, None)
        return is_stale

    def _compute_stale_extract(self) -> bool:
        """
        Проверить, изменились ли файлы игры после последнего Extract.

        Safety cap: если игра содержит аномально много файлов с релевантными
        расширениями (_STALE_CHECK_MAX_FILES), скан прерывается досрочно —
        лучше пропустить проверку, чем подвесить UI на минуту.
        """
        index_path = self.output_dir / "extract_index.json"
        try:
            if not index_path.exists():
                return False
            index = json.loads(index_path.read_text(encoding="utf-8"))
            game_dir_str = index.get("game_dir", "")
            extract_ts = index.get("timestamp", 0)
            if not game_dir_str or not extract_ts:
                return False
            gdir = Path(game_dir_str)
            if not gdir.exists():
                return False
            data_exts = {".json", ".rpy", ".ks", ".wolf", ".rvdata2", ".mps", ".dat"}
            # BUG FIX: this set predates Unity support. extract_unity() also reads
            # .csv/.tsv/.xml/.yaml/.yml/.po/.txt/.md as translatable sources (see
            # TEXT_EXTS in engine/unity_extractor.py), but none of those were ever
            # added here — editing a Unity YAML/PO/XML/CSV/TXT localization file
            # after Extract silently did NOT trigger the "stale extract" warning
            # (only .json did, since it already matched this list). Scoped to
            # game_type == "unity" specifically, so RPG Maker/Godot/etc. projects
            # don't start getting spurious staleness warnings from unrelated
            # .txt/.md files (READMEs, licenses...) that were never part of their
            # translatable data before.
            if index.get("game_type") == "unity":
                data_exts = data_exts | {".csv", ".tsv", ".xml", ".yaml", ".yml", ".po", ".txt", ".md"}
            checked = 0
            for gf in gdir.rglob("*"):
                if gf.suffix.lower() not in data_exts:
                    continue
                checked += 1
                if checked > Pipeline._STALE_CHECK_MAX_FILES:
                    break  # safety cap — не подвешиваем UI на огромных играх
                try:
                    if gf.stat().st_mtime > extract_ts:
                        return True
                except OSError:
                    pass
            return False
        except Exception:
            return False


    def clear_cache(self) -> tuple[bool, str]:
        """Сбросить кэш переводов и сохранённые *_translated.json (начать заново)."""
        state = load_state(self.output_dir)
        state.clear()

        extract_dir = self.output_dir / "extracted"
        removed = 0
        if extract_dir.exists():
            for jf in extract_dir.glob("*_translated.json"):
                try:
                    jf.unlink()
                    removed += 1
                except Exception:
                    pass

        # Invalidate both instance and class-level stats caches
        self._stats_cache = None
        Pipeline._stats_class_cache.pop(str(self.output_dir), None)
        Pipeline._stale_check_cache.pop(str(self.output_dir), None)

        msg = f"[Pipeline] Кэш переводов очищен. Удалено файлов перевода: {removed}."
        self._log(msg)
        return True, msg
