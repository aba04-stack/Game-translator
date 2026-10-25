"""
rpgmtl_desktop.py — улучшенный GUI для перевода RPG Maker MV/MZ.

Лучшее от топ-инструментов:
- Глоссарий с редактором (Translator++)
- Статистика дедупликации и прогресса (MTool)
- Resume: продолжить с места остановки
- Защита escape-кодов от порчи (SakuraTranslator)
- Автосохранение состояния
"""

import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ai_translator import DEFAULT_MODELS
from engine.file_utils import atomic_write_json

# Compiled once at module load — reused by _update_stats() on every file change
_HAS_JP_RE = re.compile(r"[぀-ヿ一-鿿]")

try:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QGroupBox, QPushButton, QLabel, QLineEdit, QTextEdit, QFileDialog,
        QComboBox, QSpinBox, QCheckBox, QProgressBar, QTabWidget,
        QFormLayout, QMessageBox, QSplitter, QFrame, QTableWidget,
        QTableWidgetItem, QHeaderView, QAbstractItemView,
    )
    from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
    from PyQt6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor
except ImportError:
    print("ОШИБКА: PyQt6 не установлен.\nУстановите: pip install PyQt6")
    sys.exit(1)

from pipeline import Pipeline
from config import load_config, save_config
from error_utils import append_log_to_file, format_exception_details, install_excepthook


# ═══════════════════════════════════════════════════════════
# SIGNALS & WORKER
# ═══════════════════════════════════════════════════════════

class WorkerSignals(QObject):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, float, str)   # cur, total, eta, current_file
    finished = pyqtSignal(bool, str)


class WorkerThread(QThread):
    def __init__(self, fn, signals: WorkerSignals):
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self):
        try:
            success, msg = self.fn()
            self.signals.finished.emit(success, msg)
        except Exception as e:
            self.signals.log.emit(format_exception_details(e, context="WorkerThread"))
            self.signals.finished.emit(False, str(e))


# ═══════════════════════════════════════════════════════════
# LOG WIDGET
# ═══════════════════════════════════════════════════════════

class LogWidget(QTextEdit):
    COLORS = {
        "[Extract]":         "#4FC3F7",
        "[Translate]":       "#A5D6A7",
        "[Inject]":          "#FFE082",
        "[Injector]":        "#FFE082",
        "[Dedup]":           "#CE93D8",
        "[Warp]":            "#CE93D8",
        "[Engine]":          "#90CAF9",
        "[Translator]":      "#A5D6A7",
        "[Glossary]":        "#FFCC80",
        "[State]":           "#B0BEC5",
        "[Pipeline]":        "#90CAF9",
        "[RenPy]":           "#FFB74D",
        "[RenPy Inject]":    "#FFB74D",
        "[Wolf]":            "#FFCC02",
        "[Wolf Inject]":     "#FFCC02",
        "[VXAce]":           "#81C784",
        "[VXAce Inject]":    "#81C784",
        "[Kirikiri]":        "#CE93D8",
        "[Kirikiri Inject]": "#CE93D8",
        "[Tyrano]":          "#E57373",
        "[Tyrano Inject]":   "#E57373",
        "[Unity]":           "#9575CD",
        "[Unity Inject]":    "#9575CD",
        "[Godot]":           "#478CBF",
        "[Godot Inject]":    "#478CBF",
        "[Restore]":         "#FF8A65",
        "ОШИБКА":            "#EF5350",
        "ERROR":             "#EF5350",
        "✓":                 "#66BB6A",
        "✗":                 "#EF5350",
        "⚠":                 "#FFA726",
        "ℹ":                 "#90CAF9",
        "═":            "#555555",
    }

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(5000) # Ограничение памяти
        self._base_font_size = 9
        self.setFont(QFont("Consolas", self._base_font_size))
        self.setStyleSheet("""
            QTextEdit {
                background-color: #1A1A1A;
                color: #D4D4D4;
                border: 1px solid #3C3C3C;
                border-radius: 4px;
                padding: 4px;
            }
        """)

    def set_font_size(self, size: int):
        size = max(7, min(18, int(size)))
        self._base_font_size = size
        self.setFont(QFont("Consolas", size))

    def zoom(self, delta: int):
        self.set_font_size(self._base_font_size + delta)

    def append_msg(self, text: str):
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        color = "#D4D4D4"
        for key, col in self.COLORS.items():
            if key in text:
                color = col
                break
        fmt.setForeground(QColor(color))
        cursor.insertText(text + "\n", fmt)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def sep(self):
        self.append_msg("─" * 60)


# ═══════════════════════════════════════════════════════════
# STATS PANEL
# ═══════════════════════════════════════════════════════════

class StatsPanel(QWidget):
    def __init__(self):
        super().__init__()
        self._last_stats: dict = {}
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("Статистика перевода")
        title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        layout.addWidget(title)

        grid = QFormLayout()
        grid.setSpacing(4)

        self.lbl_total    = QLabel("—")
        self.lbl_unique   = QLabel("—")
        self.lbl_done     = QLabel("—")
        self.lbl_remaining = QLabel("—")
        self.lbl_cached   = QLabel("—")
        self.lbl_savings  = QLabel("—")
        self.lbl_files    = QLabel("—")

        grid.addRow("Строк всего:",      self.lbl_total)
        grid.addRow("Уникальных:",       self.lbl_unique)
        grid.addRow("Переведено:",       self.lbl_done)
        grid.addRow("Осталось:",         self.lbl_remaining)
        grid.addRow("В кэше:",           self.lbl_cached)
        grid.addRow("Экономия API:",     self.lbl_savings)
        grid.addRow("Файлов данных:",    self.lbl_files)

        layout.addLayout(grid)

        # ── Предупреждение об устаревшем экстракте ──
        self.lbl_stale = QLabel("")
        self.lbl_stale.setStyleSheet("color: #FFA726; font-size: 8pt;")
        self.lbl_stale.setWordWrap(True)
        self.lbl_stale.setVisible(False)
        layout.addWidget(self.lbl_stale)

        # BUG FIX: get_stats() раньше молча пропускал файлы, которые не
        # распарсились (битый JSON, проблема кодировки) — total_strings/
        # unique_strings оказывались тихо занижены без единого признака в
        # интерфейсе. Теперь parse_errors явно виден пользователю.
        self.lbl_parse_errors = QLabel("")
        self.lbl_parse_errors.setStyleSheet("color: #EF5350; font-size: 8pt;")
        self.lbl_parse_errors.setWordWrap(True)
        self.lbl_parse_errors.setVisible(False)
        layout.addWidget(self.lbl_parse_errors)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m строк (%p%)")
        layout.addWidget(self.progress_bar)

        # ── Кнопка экспорта статистики ──
        btn_row = QHBoxLayout()
        self.btn_export_stats = QPushButton("📊 Статистика CSV")
        self.btn_export_stats.setFixedHeight(28)
        self.btn_export_stats.setToolTip("Сохранить статистику перевода в CSV файл")
        self.btn_export_stats.setEnabled(False)
        self.btn_export_stats.clicked.connect(self._export_stats_csv)
        btn_row.addWidget(self.btn_export_stats)

        self.btn_tm_manage = QPushButton("🧠 TM")
        self.btn_tm_manage.setFixedHeight(28)
        self.btn_tm_manage.setToolTip(
            "Управление глобальной Translation Memory:\n"
            "статистика, экспорт CSV, очистка"
        )
        self.btn_tm_manage.clicked.connect(self._tm_manage_dialog)
        btn_row.addWidget(self.btn_tm_manage)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addStretch()

    def refresh_stats(self, stats: dict):
        self._last_stats = dict(stats)   # cache for CSV export
        total  = stats.get("total_strings", 0)
        unique = stats.get("unique_strings", 0)
        done   = stats.get("translated_strings", 0)
        remaining = stats.get("remaining_strings", max(unique - done, 0))
        cached = stats.get("cached_strings", 0)
        files  = stats.get("files", 0)

        self.lbl_total.setText(str(total))
        self.lbl_unique.setText(str(unique))
        denom = unique or total
        self.lbl_done.setText(f"{done} / {denom} ({round(done/denom*100)}%)" if denom else "0")
        self.lbl_remaining.setText(str(remaining))
        self.lbl_cached.setText(str(cached))
        self.lbl_files.setText(str(files))

        if total > 0:
            self.btn_export_stats.setEnabled(True)

        # Предупреждение об устаревшем экстракте
        stale = stats.get("stale_extract", False)
        if stale:
            self.lbl_stale.setText(
                "⚠ Файлы игры изменились после последнего Extract.\n"
                "Рекомендуется повторить Extract чтобы подхватить новые строки."
            )
            self.lbl_stale.setVisible(True)
        else:
            self.lbl_stale.setVisible(False)

        # BUG FIX: раньше файлы, которые не распарсились, молча пропускались
        # при подсчёте статистики — числа занижались без объяснения. Теперь
        # явно предупреждаем, если такие файлы обнаружены.
        parse_errors = stats.get("parse_errors", 0)
        if parse_errors:
            self.lbl_parse_errors.setText(
                f"✗ Не удалось прочитать {parse_errors} файл(ов) при подсчёте статистики — "
                f"числа выше могут быть занижены. Проверьте extracted/*.json на повреждение."
            )
            self.lbl_parse_errors.setVisible(True)
        else:
            self.lbl_parse_errors.setVisible(False)

        if unique:
            self.progress_bar.setRange(0, unique)
            self.progress_bar.setValue(min(done, unique))
            self.progress_bar.setFormat("%v / %m уникальных строк (%p%)")
        elif total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(done, total))
            self.progress_bar.setFormat("%v / %m строк (%p%)")
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("0 / 0 строк")

    def set_dedup(self, unique: int, total: int):
        self.lbl_unique.setText(str(unique))
        savings = round((1 - unique / total) * 100, 1) if total else 0
        self.lbl_savings.setText(f"{savings}% (~{total - unique} строк)")
        self._last_stats["unique_strings"]  = unique
        self._last_stats["savings_percent"] = savings
        self._last_stats["savings_strings"] = total - unique

    def _export_stats_csv(self):
        """Сохранить статистику перевода в CSV файл."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить статистику", "stats.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            import datetime as _dt
            rows = [
                ["Параметр", "Значение"],
                ["Дата",                _dt.datetime.now().strftime("%Y-%m-%d %H:%M")],
                ["Строк всего",         self._last_stats.get("total_strings", 0)],
                ["Уникальных строк",    self._last_stats.get("unique_strings", 0)],
                ["Переведено",          self._last_stats.get("translated_strings", 0)],
                ["Осталось",            self._last_stats.get("remaining_strings", 0)],
                ["В кэше",              self._last_stats.get("cached_strings", 0)],
                ["Файлов данных",       self._last_stats.get("files", 0)],
                ["Экономия API %",      self._last_stats.get("savings_percent", "—")],
                ["Экономия строк",      self._last_stats.get("savings_strings", "—")],
            ]
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=";")
            writer.writerows(rows)
            content = buf.getvalue().encode("utf-8-sig")
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=Path(path).parent, suffix=".tmp", prefix=".rpgmtl_stats_"
            )
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(content)
            os.replace(tmp_path, path)
            QMessageBox.information(self, "Готово", f"Статистика сохранена:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить:\n{e}")

    def _tm_manage_dialog(self):
        """Диалог управления глобальной Translation Memory."""
        try:
            from global_tm import GlobalTM
            with GlobalTM() as tm:
                s = tm.get_stats()
        except Exception as e:
            QMessageBox.warning(self, "TM недоступна", f"Не удалось открыть TM:\n{e}")
            return

        # BUG FIX: get_stats() возвращает ключ "total" (см. global_tm.py), а
        # не "total_entries" — .get("total_entries", 0) молча проглатывал
        # несовпадение и ВСЕГДА возвращал 0, независимо от реального
        # содержимого базы. Диалог показывал "Записей: 0", даже если в TM
        # были тысячи записей. (Другое место в этом же файле, экспорт
        # диагностики, делает re-key "total" → "total_entries" для СВОЕГО
        # JSON — похоже, отсюда и пришла путаница.)
        total   = s.get("total", 0)
        langs   = ", ".join(s.get("languages", [])) or "(пусто)"
        db_path = str(GlobalTM.DB_PATH)

        msg = QMessageBox(self)
        msg.setWindowTitle("🧠 Глобальная Translation Memory")
        msg.setText(
            f"<b>Записей:</b> {total:,}<br>"
            f"<b>Языки:</b> {langs}<br>"
            f"<b>Файл БД:</b> <small>{db_path}</small>"
        )
        btn_export = msg.addButton("📤 Экспорт CSV", QMessageBox.ButtonRole.ActionRole)
        btn_clear  = msg.addButton("🗑 Очистить", QMessageBox.ButtonRole.DestructiveRole)
        msg.addButton("Закрыть", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == btn_export:
            path, _ = QFileDialog.getSaveFileName(
                self, "Экспорт TM", "global_tm_export.csv", "CSV (*.csv)"
            )
            if path:
                try:
                    from global_tm import GlobalTM
                    with GlobalTM() as tm2:
                        n = tm2.export_csv(Path(path))
                    QMessageBox.information(self, "Готово", f"Экспортировано {n} записей в:\n{path}")
                except Exception as e:
                    QMessageBox.critical(self, "Ошибка", str(e))

        elif clicked == btn_clear:
            ans = QMessageBox.question(
                self, "Подтверждение",
                f"Удалить все {total:,} записей из глобальной TM?\n"
                "Это действие необратимо.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Yes:
                try:
                    import sqlite3
                    from global_tm import GlobalTM
                    conn = sqlite3.connect(str(GlobalTM.DB_PATH))
                    conn.execute("DELETE FROM tm")
                    conn.commit()
                    conn.close()
                    QMessageBox.information(self, "Готово", "Глобальная TM очищена.")
                except Exception as e:
                    QMessageBox.critical(self, "Ошибка", str(e))


# ═══════════════════════════════════════════════════════════
# GLOSSARY EDITOR
# ═══════════════════════════════════════════════════════════

class GlossaryEditor(QWidget):
    def __init__(self):
        super().__init__()
        self._glossary_path: Path | None = None
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        hdr = QHBoxLayout()
        title = QLabel("Глоссарий терминов")
        title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        hdr.addWidget(title)

        hint = QLabel("Оригинал → Перевод (фиксирует имена и термины)")
        hint.setStyleSheet("color: #888; font-size: 8pt;")
        hdr.addWidget(hint, 1)
        layout.addLayout(hdr)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Оригинал", "Перевод"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #252526;
                color: #CCCCCC;
                gridline-color: #3C3C3C;
                border: 1px solid #3C3C3C;
            }
            QTableWidget::item:selected { background-color: #094771; }
            QHeaderView::section {
                background-color: #2D2D2D;
                color: #9CDCFE;
                border: 1px solid #3C3C3C;
                padding: 4px;
            }
        """)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        btn_add    = QPushButton("+ Добавить")
        btn_del    = QPushButton("− Удалить")
        btn_save   = QPushButton("💾 Сохранить")
        btn_csv_ex = QPushButton("📤 Экспорт CSV")
        btn_csv_im = QPushButton("📥 Импорт CSV")
        btn_add.clicked.connect(self._add_row)
        btn_del.clicked.connect(self._del_row)
        btn_save.clicked.connect(self.save)
        btn_csv_ex.clicked.connect(self._export_csv)
        btn_csv_im.clicked.connect(self._import_csv)
        btn_csv_ex.setToolTip("Экспортировать глоссарий в CSV для редактирования в Excel")
        btn_csv_im.setToolTip("Импортировать переводы из CSV файла")
        for b in [btn_add, btn_del, btn_save, btn_csv_ex, btn_csv_im]:
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        info = QLabel(
            "Совет: добавьте имена персонажей и специальные термины.\n"
            "Они будут защищены от изменения во время перевода."
        )
        info.setStyleSheet("color: #888; font-size: 8pt;")
        info.setWordWrap(True)
        layout.addWidget(info)

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт CSV", "", "CSV (*.csv)")
        if not path:
            return
        try:
            from engine.glossary import export_glossary_csv, Glossary
            g = Glossary()
            for row in range(self.table.rowCount()):
                orig  = self.table.item(row, 0)
                trans = self.table.item(row, 1)
                if orig and orig.text().strip():
                    g.terms[orig.text().strip()] = (trans.text().strip() if trans else "")
            count = export_glossary_csv(g, Path(path))
            QMessageBox.information(self, "✓ Экспорт", f"Экспортировано {count} терминов в:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "✗ Ошибка", f"Не удалось экспортировать CSV:\n{e}")

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Импорт CSV", "", "CSV (*.csv)")
        if not path:
            return
        try:
            from engine.glossary import import_glossary_csv, Glossary
            g = Glossary()
            # Загружаем текущее состояние
            for row in range(self.table.rowCount()):
                orig  = self.table.item(row, 0)
                trans = self.table.item(row, 1)
                if orig and orig.text().strip():
                    g.terms[orig.text().strip()] = (trans.text().strip() if trans else "")
            count = import_glossary_csv(g, Path(path), overwrite=True)
            # Обновляем таблицу
            self.table.setRowCount(0)
            for orig, trans in g.terms.items():
                r = self.table.rowCount()
                self.table.insertRow(r)
                self.table.setItem(r, 0, QTableWidgetItem(orig))
                self.table.setItem(r, 1, QTableWidgetItem(trans))
            QMessageBox.information(self, "✓ Импорт", f"Импортировано {count} терминов")
        except Exception as e:
            QMessageBox.critical(self, "✗ Ошибка", f"Не удалось импортировать CSV:\n{e}")

    def load_from_path(self, path: Path):
        self._glossary_path = path
        self.table.setRowCount(0)
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data: dict = json.load(f)
            for orig, trans in data.items():
                r = self.table.rowCount()
                self.table.insertRow(r)
                self.table.setItem(r, 0, QTableWidgetItem(str(orig)))
                self.table.setItem(r, 1, QTableWidgetItem(str(trans)))
        except Exception as e:
            print(format_exception_details(e, context="UI.glossary_load"))

    def save(self):
        if not self._glossary_path:
            return
        data = {}
        for r in range(self.table.rowCount()):
            orig  = (self.table.item(r, 0) or QTableWidgetItem("")).text().strip()
            trans = (self.table.item(r, 1) or QTableWidgetItem("")).text().strip()
            if orig:
                data[orig] = trans
        try:
            atomic_write_json(self._glossary_path, data)
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить глоссарий:\n{e}")

    def _add_row(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(""))
        self.table.setItem(r, 1, QTableWidgetItem(""))
        self.table.editItem(self.table.item(r, 0))

    def _del_row(self):
        rows = set(i.row() for i in self.table.selectedItems())
        if not rows:
            return
        ans = QMessageBox.question(
            self, "Удалить термины",
            f"Удалить {len(rows)} выбранных термин(ов) из глоссария?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        for r in sorted(rows, reverse=True):
            self.table.removeRow(r)


# ═══════════════════════════════════════════════════════════
# SETTINGS PANEL
# ═══════════════════════════════════════════════════════════


class _RetranslateThread(QThread):
    """
    Фоновый поток для повторного перевода выбранных строк из StringsViewer.

    BUG FIX: раньше translate_texts() вызывался напрямую в _retranslate_rows()
    на UI-потоке — это блокировало ВЕСЬ интерфейс на время сетевых вызовов API
    (включая retry-паузы при rate limit, которые могут доходить до 300с при
    server-specified Retry-After). QApplication.processEvents() перед вызовом
    обрабатывает очередь событий только один раз и не спасает от последующего
    блокирующего вызова. Теперь перевод выполняется в QThread, а результат
    приходит через сигнал — Qt сам маршалит его обратно в UI-поток.
    """
    finished_result = pyqtSignal(object, str)  # (TranslationResult | None, error_message)

    def __init__(self, texts: list, cfg):
        super().__init__()
        self._texts = texts
        self._cfg = cfg

    def run(self):
        try:
            from ai_translator import translate_texts
            result = translate_texts(self._texts, self._cfg, log_callback=None)
            self.finished_result.emit(result, "")
        except Exception as e:
            self.finished_result.emit(None, str(e))


class _PolishThread(QThread):
    """
    Фоновый поток для AI-полировки выбранных строк из StringsViewer —
    тот же паттерн, что и _RetranslateThread (не блокировать UI на время
    сетевых вызовов/retry-пауз).
    """
    finished_result = pyqtSignal(object, str)  # (PolishBatchResult | None, error_message)

    def __init__(self, pairs: list[tuple[str, str]], cfg):
        super().__init__()
        self._pairs = pairs
        self._cfg = cfg

    def run(self):
        try:
            from ai_translator import polish_texts
            result = polish_texts(self._pairs, self._cfg, log_callback=None)
            self.finished_result.emit(result, "")
        except Exception as e:
            self.finished_result.emit(None, str(e))


class StringsViewer(QWidget):
    """Вкладка просмотра и ручного редактирования переводов."""

    COLS = ["Файл", "Контекст", "Оригинал", "Перевод"]

    def __init__(self):
        super().__init__()
        self._output_dir = None
        self._file_map: dict[str, list[dict]] = {}  # fname → entries
        self._all_rows: list[tuple[int, str]] = []   # (table_row, file_name)
        self._dirty_files: set[str] = set()          # BUG FIX: track which files changed
        self._settings_provider = None  # callable() -> dict (TranslatorConfig values)
        self._retranslate_thread = None  # активный _RetranslateThread, если есть
        self._polish_thread = None       # активный _PolishThread, если есть
        self._retranslate_targets = []
        # P0.1: как возникла правка → применяется к состоянию при Save.
        # fname -> {original: (kind, text)}, kind ∈ manual | ai | sync
        self._pending_state_edits: dict[str, dict[str, tuple[str, str]]] = {}
        self._row_status: dict[str, str] = {}   # original -> явный статус (engine/state.py)
        self._setup_ui()

    def set_settings_provider(self, fn):
        """fn: callable() -> dict с настройками перевода (provider, api_key, model, ...)."""
        self._settings_provider = fn

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Верхняя панель
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 Поиск по тексту...")
        self.search.textChanged.connect(self._filter)
        self.lbl_count = QLabel("0 строк")
        self.lbl_count.setStyleSheet("color: #90A4AE; font-size: 11px;")
        
        self.btn_load = QPushButton("📂 Загрузить")
        self.btn_load.setFixedHeight(28)
        self.btn_load.clicked.connect(self.load)
        
        self.btn_export = QPushButton("📤 Экспорт CSV")
        self.btn_export.setFixedHeight(28)
        self.btn_export.setToolTip("Выгрузить в CSV (для переводчиков/Excel)")
        self.btn_export.clicked.connect(self._export_csv)
        self.btn_export.setEnabled(False)

        self.btn_export_xlsx = QPushButton("📊 Экспорт XLSX")
        self.btn_export_xlsx.setFixedHeight(28)
        self.btn_export_xlsx.setToolTip(
            "Выгрузить в Excel (.xlsx) для пост-редактуры:\n"
            "три колонки — Оригинал | Машинный перевод | Правка переводчика.\n"
            "После правки импортируйте через «Импорт CSV»."
        )
        self.btn_export_xlsx.clicked.connect(self._export_xlsx)
        self.btn_export_xlsx.setEnabled(False)

        self.btn_import = QPushButton("📥 Импорт CSV")
        self.btn_import.setFixedHeight(28)
        self.btn_import.setToolTip("Загрузить переводы из CSV")
        self.btn_import.clicked.connect(self._import_csv)
        self.btn_import.setEnabled(False)

        self.btn_qa = QPushButton("🚨 QA Отчёт")
        self.btn_qa.setFixedHeight(28)
        self.btn_qa.setToolTip("Проверить качество перевода (пустые, без изменений)")
        self.btn_qa.clicked.connect(self._qa_check)
        self.btn_qa.setEnabled(False)

        self.btn_replace = QPushButton("🔄 Заменить всё")
        self.btn_replace.setFixedHeight(28)
        self.btn_replace.setToolTip("Массовая автозамена текста в переводах")
        self.btn_replace.clicked.connect(self._bulk_replace)
        self.btn_replace.setEnabled(False)

        self.btn_save = QPushButton("💾 Сохранить правки")
        self.btn_save.setFixedHeight(28)
        self.btn_save.clicked.connect(self.save)
        self.btn_save.setEnabled(False)
        
        top.addWidget(self.search, 1)
        top.addWidget(self.lbl_count)
        top.addWidget(self.btn_load)
        top.addWidget(self.btn_export)
        top.addWidget(self.btn_export_xlsx)
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_qa)
        top.addWidget(self.btn_replace)
        top.addWidget(self.btn_save)
        layout.addLayout(top)

        # Таблица
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.SelectedClicked)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.table, 1)

        # Нижняя подсказка
        hint = QLabel(
            "Двойной клик по колонке «Перевод» — редактирование. Правый клик — AI-перевод, "
            "полировка, одобрить / заблокировать. Синий текст — ручная правка: AI её не перезапишет."
        )
        hint.setStyleSheet("color: #546E7A; font-size: 10px;")
        layout.addWidget(hint)

    def set_output_dir(self, output_dir):
        self._output_dir = output_dir
        from pathlib import Path
        self._source_dir = Path(output_dir) / "extracted" if output_dir else None

    def load(self):
        """Загрузить строки из extracted/ папки и показать все, включая непереведённые."""
        if not self._output_dir:
            QMessageBox.warning(self, "Загрузка", "Сначала укажите папку вывода.")
            return
        from pathlib import Path
        extract_dir = Path(self._output_dir) / "extracted"
        if not extract_dir.exists():
            QMessageBox.warning(self, "Загрузка", f"Папка не найдена:\n{extract_dir}")
            return

        self._file_map.clear()
        self._pending_state_edits.clear()   # таблица пересобирается с диска
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._all_rows.clear()

        source_files = {}
        translated_files = {}

        for jf in sorted(extract_dir.glob("*.json")):
            if jf.name.endswith("_translated.json"):
                continue
            try:
                source_files[jf.name] = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue

        for jf in sorted(extract_dir.glob("*_translated.json")):
            try:
                # BUG FIX: str.replace("_translated.json", ".json") replaces ALL occurrences
                # in the filename. Use stem-based suffix strip (consistent with injector).
                stem = jf.stem  # e.g. "Actors_translated"
                base = (stem[: -len("_translated")] + jf.suffix
                        if stem.endswith("_translated") else jf.name)
                translated_files[base] = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue

        def _index_by_path(entries):
            path_map = {}
            orig_map = {}
            for idx, entry in enumerate(entries):
                path = (entry.get("path") or "").strip()
                if path:
                    path_map[path] = entry
                orig = (entry.get("original") or "").strip()
                if orig:
                    orig_map.setdefault(orig, []).append(entry)
            return path_map, orig_map

        for fname, entries in source_files.items():
            merged = []
            tr_entries = translated_files.get(fname, [])
            tr_by_path, tr_by_orig = _index_by_path(tr_entries)
            used_ids = set()

            for entry in entries:
                merged_entry = dict(entry)
                path = (entry.get("path") or "").strip()
                orig = (entry.get("original") or "").strip()
                match = None
                if path and path in tr_by_path:
                    match = tr_by_path[path]
                elif orig and tr_by_orig.get(orig):
                    # Берём первую ещё не использованную переводную запись с тем же оригиналом
                    for cand in tr_by_orig[orig]:
                        cid = id(cand)
                        if cid not in used_ids:
                            match = cand
                            used_ids.add(cid)
                            break
                if match:
                    merged_entry["translated"] = match.get("translated", merged_entry.get("translated", ""))
                merged.append(merged_entry)

            # Если переведённый файл содержит дополнительные записи — тоже покажем их
            source_paths = {(e.get("path") or "").strip() for e in entries if (e.get("path") or "").strip()}
            for tr_entry in tr_entries:
                path = (tr_entry.get("path") or "").strip()
                orig = (tr_entry.get("original") or "").strip()
                if path and path in source_paths:
                    continue
                if any((e.get("original") or "").strip() == orig for e in entries if orig):
                    continue
                merged.append(dict(tr_entry))

            self._file_map[fname] = merged

        row = 0
        untranslated = 0
        for fname, entries in self._file_map.items():
            for entry in entries:
                self.table.insertRow(row)
                # Файл
                fi = QTableWidgetItem(entry.get("file", fname))
                fi.setData(Qt.ItemDataRole.UserRole, entry.get("path", ""))
                fi.setFlags(fi.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # Контекст
                ci = QTableWidgetItem(entry.get("context", ""))
                ci.setFlags(ci.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # Оригинал
                oi = QTableWidgetItem(entry.get("original", ""))
                oi.setFlags(oi.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # Перевод (редактируемый)
                translated = entry.get("translated", "")
                ti = QTableWidgetItem(translated)
                # Цвет: пусто = подсветка
                if not translated:
                    ti.setForeground(QColor("#EF9A9A"))
                    untranslated += 1
                else:
                    ti.setForeground(QColor("#A5D6A7"))
                for col, item in enumerate([fi, ci, oi, ti]):
                    self.table.setItem(row, col, item)
                self._all_rows.append((row, fname))
                row += 1

        self.table.blockSignals(False)
        self._refresh_row_statuses()
        self._update_count()
        self.btn_export.setEnabled(bool(self._file_map))
        self.btn_export_xlsx.setEnabled(bool(self._file_map))
        self.btn_import.setEnabled(bool(self._file_map))
        self.btn_qa.setEnabled(bool(self._file_map))
        self.btn_replace.setEnabled(bool(self._file_map))
        self.btn_save.setEnabled(bool(self._file_map))
        self.search.clear()
        if row:
            self.lbl_count.setText(f"{row} строк · непереведено: {untranslated}")

    def _export_csv(self):
        if not self._file_map:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт перевода", "translations.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            # BUG FIX: ранее данные писались в StringIO, который никогда не
            # сбрасывался в файл — файл по пути `path` не создавался.
            # Теперь пишем через атомарный temp→rename.
            _buf = io.StringIO()
            writer = csv.writer(_buf, delimiter=";")
            writer.writerow(["Файл", "Контекст", "Оригинал", "Перевод", "Путь"])
            for fname, entries in self._file_map.items():
                for e in entries:
                    writer.writerow([
                        fname,
                        e.get("context", ""),
                        e.get("original", ""),
                        e.get("translated", ""),
                        e.get("path", "")
                    ])
            content = _buf.getvalue().encode("utf-8-sig")  # BOM для корректного открытия в Excel
            out = Path(path)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=out.parent, suffix=".tmp", prefix=".rpgmtl_")
            try:
                with os.fdopen(tmp_fd, "wb") as fout:
                    fout.write(content)
                os.replace(tmp_path, out)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise
            QMessageBox.information(self, "Готово", f"Таблица успешно экспортирована в:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Ошибка экспорта:\n{e}")

    def _export_xlsx(self):
        """
        Экспорт в Excel (.xlsx) для пост-редактуры переводчиком-человеком.

        Структура листа:
          A — Файл (серый, только для чтения-ориентира)
          B — Оригинал (японский, жёлтый фон)
          C — Машинный перевод (зелёный фон)
          D — Правка переводчика (белый, редактируемый)
          E — Контекст (серый, вспомогательный)
          F — Путь (скрытый внутренний ключ, нужен при импорте)

        Импорт правок — через существующий «Импорт CSV»:
        сохраните лист как CSV (разделитель «;») с колонками
        Файл;Контекст;Оригинал;Перевод;Путь, где Перевод = колонка D.
        """
        if not self._file_map:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт для переводчика", "translations_for_review.xlsx",
            "Excel (*.xlsx)"
        )
        if not path:
            return
        try:
            import openpyxl
            from openpyxl.styles import (
                PatternFill, Font, Alignment, Border, Side
            )
            from openpyxl.utils import get_column_letter
        except ImportError:
            QMessageBox.warning(
                self, "openpyxl не установлен",
                "Для экспорта в Excel установите библиотеку:\n\n"
                "    pip install openpyxl\n\n"
                "После установки нажмите «Экспорт XLSX» снова."
            )
            return

        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Перевод"

            # ── Цвета ──
            fill_header  = PatternFill("solid", fgColor="1F4E79")  # тёмно-синий
            fill_orig    = PatternFill("solid", fgColor="FFF2CC")  # светло-жёлтый
            fill_machine = PatternFill("solid", fgColor="E2EFDA")  # светло-зелёный
            fill_edit    = PatternFill("solid", fgColor="FFFFFF")  # белый (редактируется)
            fill_meta    = PatternFill("solid", fgColor="F2F2F2")  # серый (служебный)

            font_header  = Font(bold=True, color="FFFFFF", size=10)
            font_normal  = Font(size=9)
            font_hint    = Font(size=9, color="888888")

            # ── Заголовок ──
            headers = ["Файл", "Оригинал (JP)", "Машинный перевод", "✏ Правка переводчика", "Контекст", "_path"]
            fills   = [fill_meta, fill_orig, fill_machine, fill_edit, fill_meta, fill_meta]
            ws.append(headers)
            for col_idx, (hdr, fill) in enumerate(zip(headers, fills), start=1):
                cell = ws.cell(row=1, column=col_idx)
                cell.font      = font_header
                cell.fill      = fill_header
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

            ws.row_dimensions[1].height = 30

            # ── Данные ──
            thin = Border(
                left=Side(style="thin", color="CCCCCC"),
                right=Side(style="thin", color="CCCCCC"),
                top=Side(style="thin", color="CCCCCC"),
                bottom=Side(style="thin", color="CCCCCC"),
            )
            row_fills = [fill_meta, fill_orig, fill_machine, fill_edit, fill_meta, fill_meta]

            data_row = 2
            for fname, entries in self._file_map.items():
                for e in entries:
                    orig    = e.get("original",    "")
                    trans   = e.get("translated",   "")
                    ctx     = e.get("context",      "")
                    epath   = e.get("path",         "")
                    values  = [fname, orig, trans, "",   ctx, epath]

                    ws.append(values)
                    ws.row_dimensions[data_row].height = 40
                    for col_idx, (val, fill) in enumerate(zip(values, row_fills), start=1):
                        cell = ws.cell(row=data_row, column=col_idx)
                        cell.fill      = fill
                        cell.border    = thin
                        cell.font      = font_hint if col_idx in (1, 5, 6) else font_normal
                        cell.alignment = Alignment(vertical="top", wrap_text=True)
                    # Колонка D — подсказка для редактора
                    hint_cell = ws.cell(row=data_row, column=4)
                    hint_cell.font = Font(size=9, color="AAAAAA", italic=True)
                    data_row += 1

            # ── Ширины колонок ──
            widths = [22, 55, 55, 55, 25, 0]  # px; _path = скрыта
            for col_idx, w in enumerate(widths, start=1):
                ws.column_dimensions[get_column_letter(col_idx)].width = w
            ws.column_dimensions["F"].hidden = True  # _path column

            # ── Заморозить заголовок ──
            ws.freeze_panes = "A2"

            # ── Инструкция на отдельном листе ──
            info = wb.create_sheet("📖 Инструкция")
            info.column_dimensions["A"].width = 80
            info_text = [
                ("Инструкция для переводчика", True),
                ("", False),
                ("1. Работайте только с колонкой D «✏ Правка переводчика».", False),
                ("2. Колонка B — оригинал (японский). Колонка C — машинный перевод (черновик).", False),
                ("3. Если машинный перевод хорош — скопируйте его в D. Иначе — напишите свой.", False),
                ("4. Не меняйте колонки A, B, C, E, F — они нужны для импорта.", False),
                ("5. RPG-коды вида \\C[2], \\N[1], [icon:35] должны остаться на тех же местах.", False),
                ("", False),
                ("Импорт правок:", True),
                ("  Файл → Сохранить как → CSV (разделитель «;»)", False),
                ("  В программе: вкладка «📝 Строки» → Импорт CSV", False),
            ]
            for text, bold in info_text:
                info.append([text])
                if text:
                    info.cell(row=info.max_row, column=1).font = Font(bold=bold, size=10)

            wb.save(path)
            count = data_row - 2
            QMessageBox.information(
                self, "Готово",
                f"Экспортировано {count} строк в:\n{path}\n\n"
                "Лист «📖 Инструкция» содержит инструкцию для переводчика.\n"
                "Импорт правок — через «Импорт CSV» после сохранения как CSV."
            )
        except Exception as e:
            from error_utils import format_exception_details
            QMessageBox.critical(self, "Ошибка экспорта XLSX", format_exception_details(e, context="StringsViewer.export_xlsx"))

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Импорт перевода", "", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f, delimiter=";")
                # BUG FIX: `if fname and path_val and trans` treated an EMPTY "Путь"
                # as falsy, so ANY row without a path (legitimate — not every entry
                # type has a unique path) was silently dropped from import_map,
                # causing "Импортировано 0 строк" with no error even though the CSV
                # clearly had valid translations filled in. Now path_val being ""
                # is accepted; only missing fname/trans skip a row.
                #
                # For rows that share the same (fname, path) — most commonly several
                # entries with path="" and identical original text (e.g. a repeated
                # UI string "はい") — a plain dict key would still collide. We use a
                # deque per key so duplicates are paired off IN FILE ORDER instead of
                # the last one silently overwriting all earlier ones.
                from collections import deque, defaultdict
                import_map: dict[tuple, deque] = defaultdict(deque)
                for row in reader:
                    fname = row.get("Файл")
                    path_val = row.get("Путь", "")
                    orig_val = row.get("Оригинал", "")
                    trans = row.get("Перевод", "").strip()
                    if not fname or not trans:
                        continue
                    key = (fname, path_val, orig_val)
                    import_map[key].append(trans)

                count = 0
                for row in range(self.table.rowCount()):
                    file_item = self.table.item(row, 0)
                    orig_item = self.table.item(row, 2)
                    if not file_item or not orig_item:
                        continue
                    fname = file_item.text()
                    # file_item.data хранит путь
                    path_val = file_item.data(Qt.ItemDataRole.UserRole) or ""
                    orig_val = orig_item.text()

                    key = (fname, path_val, orig_val)
                    bucket = import_map.get(key)
                    if bucket:
                        new_trans = bucket.popleft()  # ordinal pairing for duplicates
                        trans_item = self.table.item(row, 3)
                        if trans_item and trans_item.text() != new_trans:
                            trans_item.setText(new_trans)
                            count += 1
            
            QMessageBox.information(self, "Готово", f"Импортировано {count} строк. Нажмите 'Сохранить правки', чтобы записать изменения в файлы.")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Ошибка импорта:\n{e}")

    # ── Re-translate выбранных строк через AI ───────────────────────────────
    def _show_context_menu(self, pos):
        if self.table.rowCount() == 0:
            return
        selected_rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not selected_rows:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        label = (
            f"🔁 Перевести через AI ({len(selected_rows)} строк)"
            if len(selected_rows) > 1 else
            "🔁 Перевести через AI"
        )
        act_retranslate = menu.addAction(label)
        act_retranslate.triggered.connect(lambda: self._retranslate_rows(selected_rows))

        polish_label = (
            f"✨ Улучшить перевод (AI Polish, {len(selected_rows)} строк)"
            if len(selected_rows) > 1 else
            "✨ Улучшить перевод (AI Polish)"
        )
        act_polish = menu.addAction(polish_label)
        act_polish.triggered.connect(lambda: self._polish_rows(selected_rows))

        menu.addSeparator()
        suffix = f" ({len(selected_rows)} строк)" if len(selected_rows) > 1 else ""
        act_ok = menu.addAction("✔ Одобрить" + suffix)
        act_ok.setToolTip("Одобренный перевод ИИ не перезаписывает без подтверждения")
        act_ok.triggered.connect(lambda: self._mark_rows(selected_rows, "approved"))
        act_lock = menu.addAction("🔒 Заблокировать" + suffix)
        act_lock.setToolTip("Заблокированную строку ИИ не может изменить вообще")
        act_lock.triggered.connect(lambda: self._mark_rows(selected_rows, "locked"))
        act_free = menu.addAction("🔓 Снять защиту" + suffix)
        act_free.triggered.connect(lambda: self._mark_rows(selected_rows, "ai_translated"))

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _retranslate_rows(self, rows: list[int]):
        """Повторно перевести выбранные строки через AI, используя текущие настройки."""
        if getattr(self, "_retranslate_thread", None) is not None:
            QMessageBox.information(self, "Занято", "Перевод уже выполняется, дождитесь завершения.")
            return
        # BUG FIX: раньше здесь проверялся только _retranslate_thread — если
        # уже была запущена AI Polish (_polish_thread), можно было тут же
        # запустить ЕЩЁ и Retranslate поверх нею, в т.ч. на пересекающихся
        # строках. Оба фоновых потока пишут в self.table и self._file_map
        # независимо, и результат того, что завершится ПОЗЖЕ, молча
        # перезаписывал бы результат другого — без предупреждения и без
        # возможности понять, какой из двух AI-вызовов победил. Симметрично
        # с защитой, которая уже есть в _polish_rows.
        if getattr(self, "_polish_thread", None) is not None:
            QMessageBox.information(self, "Занято", "Полировка уже выполняется, дождитесь завершения.")
            return
        if not self._settings_provider:
            QMessageBox.warning(self, "Недоступно", "Настройки переводчика не подключены.")
            return
        s = self._settings_provider()
        if not s or not s.get("api_key") and s.get("provider") != "custom":
            QMessageBox.warning(self, "Нет ключа", "Укажите API ключ во вкладке «⚙ Настройки».")
            return

        # Собираем оригиналы и позиции ячеек для записи результата.
        # fname берём из self._all_rows (как в save()) — UserRole на колонке 0
        # хранит entry "path" (для матчинга внутри файла), а НЕ имя файла.
        targets = []  # (table_row, fname, entry_path, original, trans_item)
        for row in rows:
            if row >= len(self._all_rows):
                continue
            _, fname = self._all_rows[row]
            orig_item = self.table.item(row, 2)
            trans_item = self.table.item(row, 3)
            file_item = self.table.item(row, 0)
            if not orig_item or not trans_item or not file_item:
                continue
            original = orig_item.text()
            if not original.strip():
                continue
            entry_path = file_item.data(Qt.ItemDataRole.UserRole) or ""
            targets.append((row, fname, entry_path, original, trans_item))

        if not targets:
            return

        targets = self._filter_protected_targets(targets, "перевести")
        if not targets:
            return

        from ai_translator import TranslatorConfig

        # Подключаем глоссарий — та же логика что и в Pipeline.translate(),
        # чтобы одиночный re-translate не противоречил основному переводу.
        glossary_block = ""
        if self._output_dir:
            try:
                from engine.glossary import load_or_create_glossary
                glossary = load_or_create_glossary(Path(self._output_dir), None, None)
                glossary_block = glossary.to_prompt_block(
                    "auto", s.get("target_lang", "Russian")
                ) if len(glossary) > 0 else ""
            except Exception:
                pass

        cfg = TranslatorConfig(
            provider=s.get("provider", "openai"),
            api_key=s.get("api_key", ""),
            model=s.get("model", ""),
            base_url=s.get("base_url", ""),
            target_lang=s.get("target_lang", "Russian"),
            batch_size=min(len(targets), 10),
            max_workers=1,
            glossary_block=glossary_block,
            extra_instructions=s.get("extra_instructions", ""),
            use_context=False,       # ручной re-translate — без скользящего контекста
            use_global_tm=False,     # принудительный свежий перевод, не из TM
        )
        texts = [t[3] for t in targets]

        # ── Прогресс-индикатор (немодальный, не блокирует событийный цикл) ──
        self._retranslate_progress = QMessageBox(self)
        self._retranslate_progress.setWindowTitle("Перевод...")
        self._retranslate_progress.setText(
            f"Перевожу {len(targets)} строк через {s.get('provider', '?')}..."
        )
        self._retranslate_progress.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self._retranslate_progress.show()

        # ── Запуск в фоновом потоке — BUG FIX: раньше translate_texts() вызывался
        # синхронно здесь же и замораживал весь UI на время сетевых вызовов
        # (включая паузы retry при rate limit, до 300с). ──
        self._retranslate_targets = targets
        self._retranslate_thread = _RetranslateThread(texts, cfg)
        self._retranslate_thread.finished_result.connect(self._on_retranslate_finished)
        self._retranslate_thread.start()

    def _on_retranslate_finished(self, result, error: str):
        """Обработчик завершения фонового re-translate — вызывается в UI-потоке."""
        if hasattr(self, "_retranslate_progress"):
            self._retranslate_progress.close()
            del self._retranslate_progress

        targets = getattr(self, "_retranslate_targets", [])
        self._retranslate_thread = None
        self._retranslate_targets = []

        if error:
            QMessageBox.critical(self, "Ошибка перевода", error)
            return
        if result is None:
            return

        updated = 0
        self.table.blockSignals(True)
        for (row, fname, entry_path, original, trans_item), tr in zip(targets, result.results):
            if tr.success and tr.translated:
                trans_item.setText(tr.translated)
                trans_item.setForeground(QColor("#A5D6A7"))
                trans_item.setToolTip("")
                self._dirty_files.add(fname)
                self._note_edit(fname, original, tr.translated, "ai")
                # Обновляем _file_map: сначала по entry_path (точное совпадение),
                # иначе по original (как делает save() при отсутствии path)
                entries = self._file_map.get(fname, [])
                matched = False
                for e in entries:
                    same_path = bool(entry_path) and e.get("path") == entry_path
                    same_orig = e.get("original") == original
                    if same_path or (same_orig and not matched):
                        e["translated"] = tr.translated
                        matched = True
                        if same_path:
                            break
                updated += 1
        self.table.blockSignals(False)

        if updated:
            self.btn_save.setEnabled(True)
            self.btn_save.setText("💾 Сохранить правки ●")
        QMessageBox.information(
            self, "Готово",
            f"Переведено: {updated}/{len(targets)} строк.\n"
            f"Нажмите «Сохранить правки» для записи на диск.",
        )

    # ── AI Polish выбранных строк ────────────────────────────────────────
    def _polish_rows(self, rows: list[int]):
        """Улучшить формулировку уже переведённых строк через AI, не
        переводя их заново с нуля (см. ai_translator.polish_texts)."""
        if getattr(self, "_polish_thread", None) is not None:
            QMessageBox.information(self, "Занято", "Полировка уже выполняется, дождитесь завершения.")
            return
        if getattr(self, "_retranslate_thread", None) is not None:
            QMessageBox.information(self, "Занято", "Перевод уже выполняется, дождитесь завершения.")
            return
        if not self._settings_provider:
            QMessageBox.warning(self, "Недоступно", "Настройки переводчика не подключены.")
            return
        s = self._settings_provider()
        if not s or not s.get("api_key") and s.get("provider") != "custom":
            QMessageBox.warning(self, "Нет ключа", "Укажите API ключ во вкладке «⚙ Настройки».")
            return

        # В отличие от _retranslate_rows нам нужен ТЕКУЩИЙ перевод, а не
        # только оригинал — полировка улучшает уже существующий черновик.
        # Строки без перевода пропускаем — полировать нечего.
        targets = []  # (row, fname, entry_path, original, current_translation, trans_item)
        for row in rows:
            if row >= len(self._all_rows):
                continue
            _, fname = self._all_rows[row]
            orig_item = self.table.item(row, 2)
            trans_item = self.table.item(row, 3)
            file_item = self.table.item(row, 0)
            if not orig_item or not trans_item or not file_item:
                continue
            original = orig_item.text()
            current = trans_item.text()
            if not original.strip() or not current.strip():
                continue
            entry_path = file_item.data(Qt.ItemDataRole.UserRole) or ""
            targets.append((row, fname, entry_path, original, current, trans_item))

        if not targets:
            QMessageBox.information(
                self, "Нечего полировать",
                "Среди выбранных строк нет уже переведённых — сначала переведите их."
            )
            return

        targets = self._filter_protected_targets(targets, "улучшить")
        if not targets:
            return

        from ai_translator import TranslatorConfig

        glossary_block = ""
        if self._output_dir:
            try:
                from engine.glossary import load_or_create_glossary
                glossary = load_or_create_glossary(Path(self._output_dir), None, None)
                glossary_block = glossary.to_prompt_block(
                    "auto", s.get("target_lang", "Russian")
                ) if len(glossary) > 0 else ""
            except Exception:
                pass

        cfg = TranslatorConfig(
            provider=s.get("provider", "openai"),
            api_key=s.get("api_key", ""),
            model=s.get("model", ""),
            base_url=s.get("base_url", ""),
            target_lang=s.get("target_lang", "Russian"),
            batch_size=min(len(targets), 10),
            glossary_block=glossary_block,
            extra_instructions=s.get("extra_instructions", ""),
            # BUG FIX: раньше use_global_tm не передавался вовсе, поэтому
            # полировка ВСЕГДА писала в Global TM независимо от чекбокса в
            # настройках — выключение "Глобальная память переводов" там
            # тихо игнорировалось именно для AI Polish.
            use_global_tm=s.get("use_global_tm", True),
        )
        pairs = [(t[3], t[4]) for t in targets]

        self._polish_progress = QMessageBox(self)
        self._polish_progress.setWindowTitle("Полировка...")
        self._polish_progress.setText(
            f"Улучшаю формулировку {len(targets)} строк через {s.get('provider', '?')}..."
        )
        self._polish_progress.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self._polish_progress.show()

        self._polish_targets = targets
        self._polish_thread = _PolishThread(pairs, cfg)
        self._polish_thread.finished_result.connect(self._on_polish_finished)
        self._polish_thread.start()

    def _on_polish_finished(self, result, error: str):
        """Обработчик завершения фоновой полировки — вызывается в UI-потоке."""
        if hasattr(self, "_polish_progress"):
            self._polish_progress.close()
            del self._polish_progress

        targets = getattr(self, "_polish_targets", [])
        self._polish_thread = None
        self._polish_targets = []

        if error:
            QMessageBox.critical(self, "Ошибка полировки", error)
            return
        if result is None:
            return

        updated = 0
        skipped = 0
        self.table.blockSignals(True)
        for (row, fname, entry_path, original, current, trans_item), r in zip(targets, result.results):
            if not r.success:
                skipped += 1
                continue
            if r.success and r.changed and r.after:
                trans_item.setText(r.after)
                trans_item.setForeground(QColor("#A5D6A7"))
                trans_item.setToolTip("")
                self._dirty_files.add(fname)
                self._note_edit(fname, original, r.after, "ai")
                entries = self._file_map.get(fname, [])
                matched = False
                for e in entries:
                    same_path = bool(entry_path) and e.get("path") == entry_path
                    same_orig = e.get("original") == original
                    if same_path or (same_orig and not matched):
                        e["translated"] = r.after
                        matched = True
                        if same_path:
                            break
                updated += 1
        self.table.blockSignals(False)

        if updated:
            self.btn_save.setEnabled(True)
            self.btn_save.setText("💾 Сохранить правки ●")
        QMessageBox.information(
            self, "Готово",
            f"Улучшено: {updated}/{len(targets)} строк "
            f"(без изменений или без API-ключа: {len(targets) - updated - skipped}, "
            f"ошибок: {skipped}).\n"
            f"Нажмите «Сохранить правки» для записи на диск.",
        )

    def _bulk_replace(self):
        if not self._file_map:
            return
            
        from PyQt6.QtWidgets import QInputDialog
        search_str, ok = QInputDialog.getText(self, "Заменить всё", "Найти текст:")
        if not ok or not search_str:
            return
            
        replace_str, ok = QInputDialog.getText(self, "Заменить всё", f"Заменить '{search_str}' на:")
        if not ok:
            return
            
        is_regex = False
        pattern = None
        try:
            # Используем регулярку для точной замены без учета регистра,
            # или простую замену если введены спецсимволы
            # BUG FIX: "/" или "//" (без текста между слэшами) технически
            # проходят startswith("/")+endswith("/"), и search_str[1:-1] даёт
            # ПУСТУЮ строку — валидный regex, совпадающий на КАЖДОЙ позиции
            # (нулевой ширины). re.sub с таким паттерном вставляет replace_str
            # между КАЖДЫМ символом во всех переводах разом ("Герой" →
            # "XГXеXрXоXйX"), молча и без предупреждения. Легко набрать по
            # опечатке, ища буквально "/". Требуем непустое тело между
            # слэшами, прежде чем включать режим regex.
            if (search_str.startswith("/") and search_str.endswith("/")
                    and search_str[1:-1]):
                pattern = re.compile(search_str[1:-1], re.IGNORECASE)
                is_regex = True
        except (re.error, Exception):
            is_regex = False
            
        count = 0
        for fname, entries in self._file_map.items():
            for e in entries:
                trans = e.get("translated", "")
                if not trans:
                    continue
                    
                new_trans = trans
                if is_regex:
                    new_trans = pattern.sub(replace_str, trans)
                else:
                    new_trans = trans.replace(search_str, replace_str)
                    
                if new_trans != trans:
                    e["translated"] = new_trans
                    self._dirty_files.add(fname)
                    self._note_edit(fname, e.get("original", ""), new_trans, "sync")
                    count += 1
                    
        if count > 0:
            QMessageBox.information(self, "Успех", f"Заменено строк: {count}. Нажмите 'Сохранить правки' для применения.")
            # BUG FIX: self.load() reloaded from disk, silently discarding the
            # in-memory changes made above — the "Save" button then saved the
            # original (unmodified) data. Instead, refresh only the table cells
            # in-place from the updated _file_map without touching the disk.
            self.table.blockSignals(True)
            for table_row, fname in self._all_rows:
                if table_row >= self.table.rowCount():
                    continue
                orig_item  = self.table.item(table_row, 2)
                trans_item = self.table.item(table_row, 3)
                if not orig_item or not trans_item:
                    continue
                orig = orig_item.text()
                for e in self._file_map.get(fname, []):
                    if e.get("original") == orig:
                        new_t = e.get("translated", "")
                        if trans_item.text() != new_t:
                            trans_item.setText(new_t)
                        color = "#A5D6A7" if new_t else "#EF9A9A"
                        trans_item.setForeground(QColor(color))
                        break
            self.table.blockSignals(False)
            self._update_count()
        else:
            QMessageBox.information(self, "Результат", "Совпадений не найдено.")

    def _qa_check(self):
        if not self._file_map:
            return
        
        empty_count = 0
        same_count = 0
        jp_chars_left = 0
        
        has_jp = _HAS_JP_RE
        
        for fname, entries in self._file_map.items():
            for e in entries:
                orig = e.get("original", "").strip()
                trans = e.get("translated", "").strip()
                if not orig:
                    continue
                if not trans:
                    empty_count += 1
                elif orig == trans:
                    same_count += 1
                elif has_jp.search(trans):
                    jp_chars_left += 1

        total = sum(len(entries) for entries in self._file_map.values())
        report = (
            f"Всего строк: {total}\n"
            f"--------------------------\n"
            f"Пустые переводы: {empty_count}\n"
            f"Одинаковые с оригиналом: {same_count}\n"
            f"Японские символы в переводе: {jp_chars_left}\n"
        )
        
        if empty_count == 0 and same_count == 0 and jp_chars_left == 0:
            QMessageBox.information(self, "QA Проверка", report + "\nВсё выглядит отлично!")
        else:
            QMessageBox.warning(self, "QA Проверка (Найдены проблемы)", report)

    def save(self):
        """Сохранить отредактированные переводы обратно в файлы.

        BUG FIX: ранее перезаписывались ВСЕ файлы даже нетронутые.
        Если между Load и Save отработал Translate — его переводы стирались.
        Теперь пишем только файлы, которые реально редактировались.
        """
        if not self._output_dir or not self._file_map:
            return
        source_dir = getattr(self, "_source_dir", Path(self._output_dir) / "extracted")
        source_dir.mkdir(parents=True, exist_ok=True)

        # Собираем правки из таблицы обратно в _file_map
        for table_row, fname in self._all_rows:
            if table_row >= self.table.rowCount():
                continue
            file_item = self.table.item(table_row, 0)
            orig_item = self.table.item(table_row, 2)
            trans_item = self.table.item(table_row, 3)
            if not file_item or not orig_item or not trans_item:
                continue
            original = orig_item.text()
            translated = trans_item.text()
            entry_path = file_item.data(Qt.ItemDataRole.UserRole) or ""
            entries = self._file_map.get(fname, [])
            matched = False
            for entry in entries:
                same_path = bool(entry_path) and entry.get("path") == entry_path
                same_orig = entry.get("original") == original
                if same_path or (same_orig and not matched):
                    entry["translated"] = translated
                    matched = True
                    if same_path:
                        break

        # Сохраняем только изменённые файлы. Если правок нет — говорим об этом
        # явно вместо перезаписи ВСЕХ файлов (что могло стереть результаты Translate).
        if not self._dirty_files:
            # Файлы уже на диске, но пометки «ручная правка» могли не дойти до
            # состояния при прошлом сохранении (сбой записи) — повторяем только их,
            # иначе подсказка «повторите Сохранить» в предупреждении не работала бы.
            if self._pending_state_edits:
                self._persist_pending_state_edits(list(self._pending_state_edits))
                if not self._pending_state_edits:
                    self.lbl_count.setText("Пометки правок сохранены")
                    return
            self.lbl_count.setText("Нет изменений для сохранения")
            return
        files_to_save = set(self._dirty_files)
        saved = 0
        saved_files: list[str] = []
        failed: list[tuple[str, str]] = []
        for fname in files_to_save:
            entries = self._file_map.get(fname)
            if entries is None:
                continue
            # BUG FIX: double str.replace was fragile for filenames containing
            # "_translated" (e.g. "Map_translated.json" → "Map_.json").
            # fname keys in _file_map are original .json names (no _translated suffix),
            # so just strip the extension and append _translated.json.
            stem = Path(fname).stem  # "Actors" (never ends in _translated)
            out_path = source_dir / f"{stem}_translated.json"
            try:
                atomic_write_json(out_path, entries)
                saved += 1
                saved_files.append(fname)
            except Exception as e:
                failed.append((fname, str(e)))

        # P0.1: теперь, когда файлы на диске, фиксируем в состоянии, КАК возникли
        # правки (ручные → защищены от перезаписи ИИ).
        self._persist_pending_state_edits(saved_files)

        # BUG FIX: раньше ошибка записи файла (диск заполнен, нет прав на
        # запись, путь недоступен и т.п.) молча проглатывалась ("except
        # Exception: pass"), а self._dirty_files.clear() выполнялся
        # безусловно — пользователь видел "Сохранено файлов: N" без единого
        # намёка, что часть его РУЧНЫХ правок не долетела до диска, и
        # кнопка "Сохранить правки" переставала показывать несохранённые
        # изменения, хотя они были. Для ручного редактирования (самого
        # дорогого, что может потеряться) такая тишина недопустима.
        # Файлы, которые реально сохранились, больше не dirty; те, что
        # провалились, остаются dirty — повторное сохранение их подхватит.
        self._dirty_files.clear()
        self._dirty_files.update(fname for fname, _ in failed)

        self.lbl_count.setText(f"Сохранено файлов: {saved}")
        if failed:
            details = "\n".join(f"• {fname}: {err}" for fname, err in failed)
            QMessageBox.warning(
                self, "Не всё сохранено",
                f"Сохранено {saved} файлов, но {len(failed)} НЕ удалось записать:\n\n"
                f"{details}\n\n"
                "Правки для этих файлов остались только в интерфейсе — "
                "попробуйте сохранить ещё раз.",
            )
            self.btn_save.setText("💾 Сохранить правки ●")
        else:
            self.btn_save.setText("💾 Сохранить правки")

    def _on_item_changed(self, item):
        """Обновить цвет ячейки при редактировании перевода."""
        if item.column() != 3:
            return
        row = item.row()
        orig_item = self.table.item(row, 2)
        original = orig_item.text() if orig_item else ""
        # BUG FIX: помечаем файл изменённым для выборочного сохранения
        if row < len(self._all_rows):
            _, fname = self._all_rows[row]
            self._dirty_files.add(fname)
            # Сигналы таблицы блокируются при программных обновлениях (AI-перевод,
            # полировка, массовая замена), поэтому сюда попадает именно ПРАВКА
            # ЧЕЛОВЕКА: ввод в ячейку или импорт CSV. Пустой текст не фиксируем —
            # состояние не хранит пустых переводов.
            if item.text().strip():
                self._note_edit(fname, original, item.text(), "manual")
        self._style_translation_cell(item, original)

    # ── Статусы строк / происхождение правок (P0.1) ─────────────────────
    _STATUS_STYLE = {
        "manual_edited": ("✎ Ручная правка — ИИ её не перезапишет", "#90CAF9"),
        "approved":      ("✔ Одобрено — ИИ не перезапишет без вашего подтверждения", "#80CBC4"),
        "locked":        ("🔒 Заблокировано — ИИ не может изменить, пока не снята защита", "#B0BEC5"),
        "needs_review":  ("⚠ Требует проверки", "#FFCC80"),
        "stale":         ("⚠ Оригинал изменился после перевода", "#FFCC80"),
    }

    def _status_of(self, original: str) -> str:
        return self._row_status.get((original or "").strip(), "ai_translated")

    def _note_edit(self, fname: str, original: str, text: str, kind: str) -> None:
        """Запомнить, КАК возникла правка; в состояние уходит при Save().

        kind: "manual" — набрал человек, "ai" — результат явного ИИ-действия
        над выбранными строками, "sync" — механическая замена (статус не меняем).
        """
        key = (original or "").strip()
        if not key:
            return
        self._pending_state_edits.setdefault(fname, {})[key] = (kind, text)
        # Оптимистично обновляем отображаемый статус (на диск — при Save).
        if kind == "manual":
            if self._row_status.get(key) != "locked":
                self._row_status[key] = "manual_edited"
        elif kind == "ai":
            if self._row_status.get(key) in ("manual_edited", "approved", "needs_review", "stale"):
                self._row_status.pop(key, None)

    def _style_translation_cell(self, item, original: str) -> None:
        """Цвет и подсказка ячейки перевода: пусто → красный, защищено → цвет статуса."""
        if not item.text().strip():
            item.setForeground(QColor("#EF9A9A"))
            item.setToolTip("")
            return
        info = self._STATUS_STYLE.get(self._status_of(original))
        if info:
            tip, color = info
            item.setForeground(QColor(color))
            item.setToolTip(tip)
        else:
            item.setForeground(QColor("#A5D6A7"))
            item.setToolTip("")

    def _refresh_row_statuses(self) -> None:
        """Прочитать статусы из translation_state.json и подсветить строки."""
        self._row_status = {}
        if self._output_dir:
            try:
                from engine.state import load_state
                st = load_state(Path(self._output_dir), log_cb=lambda _m: None)
                self._row_status = st.explicit_statuses()
            except Exception:
                self._row_status = {}
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            oi, ti = self.table.item(r, 2), self.table.item(r, 3)
            if oi and ti:
                self._style_translation_cell(ti, oi.text())
        self.table.blockSignals(False)

    def _filter_protected_targets(self, targets: list, what: str):
        """Для явных ИИ-действий над выбранными строками (индекс 3 = оригинал).

        Заблокированные строки пропускаются всегда; для ручных правок и
        одобренных спрашиваем, чтобы случайное «выделить всё → перевести» не
        стёрло работу человека. Возвращает новый список или None при отмене.
        """
        locked = [t for t in targets if self._status_of(t[3]) == "locked"]
        if locked:
            targets = [t for t in targets if self._status_of(t[3]) != "locked"]
            QMessageBox.information(
                self, "Заблокированные строки",
                f"Пропущено заблокированных строк: {len(locked)}.\n"
                "Чтобы менять их через AI, сначала снимите защиту "
                "(правый клик → «Снять защиту»).",
            )
        guarded = [t for t in targets if self._status_of(t[3]) in ("manual_edited", "approved")]
        if guarded:
            ans = QMessageBox.question(
                self, "Защищённые строки",
                f"Среди выбранных строк {len(guarded)} с ручной правкой или одобренных.\n\n"
                f"Да — {what} и их тоже (текст будет заменён).\n"
                "Нет — пропустить защищённые строки.\n"
                "Отмена — ничего не делать.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return None
            if ans == QMessageBox.StandardButton.No:
                targets = [t for t in targets if t not in guarded]
        return targets

    def _persist_pending_state_edits(self, fnames) -> None:
        """После успешной записи файлов перенести происхождение правок в состояние."""
        manual: dict[str, str] = {}
        ai: dict[str, str] = {}
        sync: dict[str, str] = {}
        taken: dict[str, dict[str, tuple[str, str]]] = {}
        for fname in fnames:
            pending = self._pending_state_edits.pop(fname, None)
            if not pending:
                continue
            taken[fname] = pending
            for orig, (kind, text) in pending.items():
                {"manual": manual, "ai": ai}.get(kind, sync)[orig] = text
        if not (manual or ai or sync) or not self._output_dir:
            return
        try:
            from engine.state import record_edits
            record_edits(Path(self._output_dir), manual=manual, ai=ai, sync=sync)
        except Exception as e:
            # Не теряем информацию о происхождении правок: вернём в очередь.
            for fname, pending in taken.items():
                self._pending_state_edits.setdefault(fname, {}).update(pending)
            QMessageBox.warning(
                self, "Статусы строк не сохранены",
                "Переводы записаны в файлы, но не удалось сохранить пометки "
                f"«ручная правка» в translation_state.json:\n{e}\n\n"
                "Пока они не сохранены, AI-полировка может перезаписать эти строки. "
                "Повторите «Сохранить правки».",
            )

    def _mark_rows(self, rows: list[int], status: str) -> None:
        """Выставить статус (approved / locked / ai_translated=снять защиту) строкам."""
        if not self._output_dir:
            return
        items: list[tuple[str, str]] = []
        for row in rows:
            oi, ti = self.table.item(row, 2), self.table.item(row, 3)
            if oi and ti and oi.text().strip() and ti.text().strip():
                items.append((oi.text().strip(), ti.text()))
        if not items:
            QMessageBox.information(self, "Нет переведённых строк",
                                    "Статус можно задать только уже переведённым строкам.")
            return
        # Несохранённый текст этих строк запишется при Save, но статус задаёт
        # пользователь прямо сейчас — не даём ему перетереться пометкой «ручная правка».
        wanted = {o for o, _ in items}
        for pending in self._pending_state_edits.values():
            for orig in wanted & set(pending):
                pending[orig] = ("sync", pending[orig][1])
        try:
            from engine.state import record_status
            done = record_status(Path(self._output_dir), items, status)
        except Exception as e:
            QMessageBox.warning(self, "Статус не сохранён", f"Не удалось записать статус:\n{e}")
            return
        if status == "ai_translated":
            for o in wanted:
                self._row_status.pop(o, None)
        else:
            for o in wanted:
                self._row_status[o] = status
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            oi, ti = self.table.item(r, 2), self.table.item(r, 3)
            if oi and ti and oi.text().strip() in wanted:
                self._style_translation_cell(ti, oi.text())
        self.table.blockSignals(False)
        self.lbl_count.setText(f"Статус «{status}» выставлен: {done} строк")

    def _filter(self, text: str):
        """Скрыть строки не совпадающие с поиском."""
        text = text.lower()
        visible = 0
        for row in range(self.table.rowCount()):
            match = False
            if not text:
                match = True
            else:
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    if item and text in item.text().lower():
                        match = True
                        break
            self.table.setRowHidden(row, not match)
            if match:
                visible += 1
        self._update_count(visible if text else None)

    def _update_count(self, count=None):
        total = self.table.rowCount()
        if count is None:
            count = total
        t = f"{count} строк" if count == total else f"{count} / {total} строк"
        self.lbl_count.setText(t)


class SettingsPanel(QWidget):
    _key_status_signal = pyqtSignal(bool, str)  # ok, message

    def __init__(self):
        super().__init__()
        self._key_status_signal.connect(self._apply_key_status)
        self._is_dirty  = False   # несохранённые изменения
        self._loading   = False   # подавляет _on_setting_changed во время load_from_config
        layout = QFormLayout(self)
        layout.setSpacing(8)

        self.provider = QComboBox()
        self.provider.addItems(["gemini", "openai", "claude", "groq", "deepseek", "custom"])
        self.provider.currentTextChanged.connect(self._update_hint)
        # BUG FIX: _validate_api_key_format was only wired to api_key.textChanged.
        # If the user pastes a key (auto-detect correctly sets provider, hint hidden),
        # then manually switches the provider dropdown afterward (key text unchanged),
        # the now-mismatched key/provider pair was never re-checked — hint stayed
        # hidden even though it was now genuinely wrong. Re-validate on provider change too.
        self.provider.currentTextChanged.connect(
            lambda _: self._validate_api_key_format(self.api_key.text())
        )
        layout.addRow("Провайдер:", self.provider)

        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("Например: http://localhost:1234/v1 (для LM Studio/Ollama)")
        layout.addRow("Custom URL:", self.base_url)

        self.quality_mode = QComboBox()
        self.quality_mode.addItems(["balanced", "fast", "quality"])
        self.quality_mode.setCurrentText("balanced")
        self.quality_mode.currentTextChanged.connect(self._apply_quality_mode)
        self.quality_mode.setToolTip(
            "fast — быстро и дёшево\n"
            "balanced — рекомендуется (качество + скорость)\n"
            "quality — максимальное качество, медленнее и дороже"
        )
        layout.addRow("Режим:", self.quality_mode)

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Введите API ключ — провайдер определится автоматически")
        self.api_key.textChanged.connect(self._auto_detect_provider)
        self.api_key.textChanged.connect(self._validate_api_key_format)

        self.lbl_key_hint = QLabel("")
        self.lbl_key_hint.setStyleSheet("color: #FFA726; font-size: 8pt;")
        self.lbl_key_hint.setVisible(False)
        layout.addRow("API ключ:", self.api_key)
        layout.addRow("", self.lbl_key_hint)

        # Кнопка проверки + статус
        key_row = QHBoxLayout()
        self.btn_test_key = QPushButton("🔑 Проверить ключ")
        self.btn_test_key.setFixedHeight(28)
        self.btn_test_key.clicked.connect(self._test_api_key)
        self.lbl_key_status = QLabel("")
        self.lbl_key_status.setWordWrap(True)
        key_row.addWidget(self.btn_test_key)
        key_row.addWidget(self.lbl_key_status, 1)
        layout.addRow("", key_row)

        self.model = QLineEdit()
        self.model.setPlaceholderText("gpt-4o-mini (оставьте пустым)")
        layout.addRow("Модель:", self.model)

        self.source_lang = QLabel("Авто")
        self.source_lang.setStyleSheet("color: #90CAF9; font-weight: bold;")
        self.source_lang.setToolTip("Источник определяется автоматически по тексту и игре")
        layout.addRow("Язык оригинала:", self.source_lang)

        self.target_lang = QComboBox()
        self.target_lang.addItems(["Russian", "English"])
        self.target_lang.setToolTip("Выберите язык перевода")
        layout.addRow("Язык перевода:", self.target_lang)

        self.batch_size = QSpinBox()
        self.batch_size.setRange(1, 100)
        self.batch_size.setValue(25)
        layout.addRow("Размер батча:", self.batch_size)

        self.max_workers = QSpinBox()
        self.max_workers.setRange(1, 10)
        self.max_workers.setValue(3)
        self.max_workers.setToolTip("Количество параллельных потоков. Больше — быстрее перевод, но выше риск ошибки 'Rate Limit'.")
        layout.addRow("Потоки (Threads):", self.max_workers)

        self.max_retries = QSpinBox()
        self.max_retries.setRange(1, 10)
        self.max_retries.setValue(3)
        layout.addRow("Макс. повторов:", self.max_retries)

        self.request_delay = QSpinBox()
        self.request_delay.setRange(0, 30)
        self.request_delay.setValue(0)
        self.request_delay.setSuffix(" сек")
        self.request_delay.setToolTip("Пауза между батчами (предотвращает ошибку 429 Rate Limit)")
        layout.addRow("Задержка батча:", self.request_delay)

        self.auto_game_profile = QCheckBox("Автоподбор параметров по игре")
        self.auto_game_profile.setChecked(True)
        self.auto_game_profile.setToolTip("Автоматически подстраивает батч и задержку под выбранный движок")
        self.auto_game_profile.toggled.connect(self._sync_parameter_state)
        layout.addRow("", self.auto_game_profile)

        self.use_global_tm = QCheckBox("Глобальная память переводов (между играми)")
        self.use_global_tm.setChecked(True)
        self.use_global_tm.setToolTip(
            "Сохраняет переводы общих фраз (без RPG-кодов) в ~/.rpgmtl/global_tm.db\n"
            "и переиспользует их в следующих играх — экономит вызовы API."
        )
        layout.addRow("", self.use_global_tm)

        self.profile_label = QLabel("Профиль: авто")
        self.profile_label.setStyleSheet("color: #90CAF9; font-size: 8pt;")
        layout.addRow("Профиль игры:", self.profile_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addRow(sep)

        self.apply_warp = QCheckBox("Smart Warp (перенос строк)")
        self.apply_warp.setChecked(True)
        layout.addRow("Форматирование:", self.apply_warp)

        self.warp_width = QSpinBox()
        self.warp_width.setRange(100, 1200)
        self.warp_width.setValue(400)
        self.warp_width.setSuffix(" px")
        layout.addRow("Ширина диалога:", self.warp_width)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        layout.addRow(sep2)

        self.extra = QTextEdit()
        self.extra.setMaximumHeight(70)
        self.extra.setPlaceholderText(
            "Доп. инструкции переводчику (необязательно):\n"
            "Например: 'Это игра в жанре хоррор, используй мрачный стиль.'"
        )
        layout.addRow("Доп. промпт:", self.extra)

        self._user_changed_params = False
        self._current_game_profile = {"name": "auto", "batch_size": 25, "request_delay": 0, "model": ""}
        self._update_hint(self.provider.currentText())
        self._sync_parameter_state(self.auto_game_profile.isChecked())
        # После авто-выставления — следим за ручными изменениями
        self.batch_size.valueChanged.connect(self._on_params_changed)
        self.request_delay.valueChanged.connect(self._on_params_changed)

        # ── Кнопки Save / Revert ────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._btn_apply  = QPushButton("💾 Применить")
        self._btn_revert = QPushButton("↺ Сбросить")
        for b in (self._btn_apply, self._btn_revert):
            b.setFixedHeight(28)
            b.setEnabled(False)
            btn_row.addWidget(b)
        self._btn_apply.setToolTip(
            "Сохранить настройки в файл конфигурации (~/.rpgmtl_config.json)"
        )
        self._btn_revert.setToolTip("Откатить к последним сохранённым настройкам")
        self._btn_apply.clicked.connect(self.apply_and_save)
        self._btn_revert.clicked.connect(self.revert)
        layout.addRow("", btn_row)

        # Подключаем слежение за изменениями (после инициализации всех виджетов)
        self._connect_change_signals()

    # Оптимальные параметры для каждого провайдера
    # Имя модели берём из ai_translator.DEFAULT_MODELS — единого источника правды
    # (раньше оно дублировалось здесь и расходилось с настоящим дефолтом).
    _PROVIDER_PARAMS = {
        #                     model                         batch delay
        "openai":   (DEFAULT_MODELS["openai"],              25,   0),
        "gemini":   (DEFAULT_MODELS["gemini"],              15,   2),  # free tier: ограничение по RPM
        "claude":   (DEFAULT_MODELS["claude"],              20,   1),
        "groq":     (DEFAULT_MODELS["groq"],                20,   1),  # free tier: ограничение по RPM
        "deepseek": (DEFAULT_MODELS["deepseek"],            25,   0),
    }

    def _update_hint(self, provider: str):
        params = self._PROVIDER_PARAMS.get(provider, ("default", 25, 0))
        model_hint, batch, delay = params
        self.model.setPlaceholderText(f"{model_hint} (оставьте пустым)")
        # Авто-выставляем параметры только если пользователь не включил авто-профиль по игре.
        # _loading guard: setValue в авто-режиме — не ручная правка, не должна ставить ●.
        if not self._user_changed_params and not self.auto_game_profile.isChecked():
            self._loading = True
            self.batch_size.setValue(batch)
            self.request_delay.setValue(delay)
            self._loading = False

    def _on_params_changed(self):
        """Пользователь вручную изменил параметры — не перезаписываем."""
        self._user_changed_params = True

    def _apply_quality_mode(self, mode: str):
        """Применить пресет качества."""
        from ai_translator import QUALITY_MODES
        preset = QUALITY_MODES.get(mode)
        if not preset:
            return
        provider = self.provider.currentText()
        model = preset["models"].get(provider, "")
        # _loading guard: качество-пресет — авто-изменение, не требует явного Save.
        self._loading = True
        self._user_changed_params = False
        if not self.auto_game_profile.isChecked():
            batch = preset["batch_size"]
            self.batch_size.setValue(batch)
        if model:
            self.model.setText(model)
        self._user_changed_params = True
        self._loading = False

    def _auto_detect_provider(self, key: str):
        """Авто-определение провайдера по формату ключа."""
        key = key.strip()
        if key.startswith("sk-ant-"):
            detected = "claude"
        elif key.startswith("AIza") or key.startswith("ya29."):
            detected = "gemini"
        elif key.startswith("gsk_"):
            detected = "groq"
        elif key.startswith("sk-") and len(key) > 15:
            detected = "openai"
        elif "deepseek" in key.lower():
            detected = "deepseek"
        else:
            return  # не определили — не меняем
        idx = self.provider.findText(detected)
        if idx >= 0:
            self.provider.setCurrentIndex(idx)

    def _validate_api_key_format(self, key: str):
        """Показать подсказку если формат ключа не соответствует выбранному провайдеру."""
        key = key.strip()
        if not key:
            self.lbl_key_hint.setVisible(False)
            return

        provider = self.provider.currentText()
        EXPECTED = {
            "openai":   ("sk-",),
            "claude":   ("sk-ant-",),
            "gemini":   ("AIza", "ya29."),
            "groq":     ("gsk_",),
            "deepseek": ("sk-",),
        }
        prefixes = EXPECTED.get(provider)
        if not prefixes:  # custom — не проверяем
            self.lbl_key_hint.setVisible(False)
            return

        if any(key.startswith(p) for p in prefixes):
            self.lbl_key_hint.setVisible(False)
        else:
            expected_str = " или ".join(f'"{p}…"' for p in prefixes)
            self.lbl_key_hint.setText(
                f"⚠ Ключ для «{provider}» обычно начинается с {expected_str}. "
                "Проверьте правильность ключа."
            )
            self.lbl_key_hint.setVisible(True)

    def _sync_parameter_state(self, enabled: bool):
        """Блокировать или разблокировать ручные параметры."""
        for widget in (self.batch_size, self.request_delay, self.model):
            widget.setEnabled(not enabled)
        self.profile_label.setText(
            "Профиль: авто" if enabled else "Профиль: ручной режим"
        )

    def apply_game_profile(self, game_type: str):
        """Подобрать параметры под конкретный движок."""
        game_type = (game_type or "unknown").lower()
        profiles = {
            "rpgmv":    {"name": "RPG Maker MV",   "batch_size": 25, "request_delay": 0},
            "rpgmz":    {"name": "RPG Maker MZ",   "batch_size": 25, "request_delay": 0},
            "vxace":    {"name": "RPG Maker VX/Ace", "batch_size": 20, "request_delay": 0},
            "wolf":     {"name": "Wolf RPG Editor", "batch_size": 20, "request_delay": 0},
            "kirikiri": {"name": "Kirikiri/KAG",    "batch_size": 18, "request_delay": 1},
            "tyrano":   {"name": "TyranoBuilder",   "batch_size": 18, "request_delay": 0},
            "renpy":    {"name": "Ren'Py",          "batch_size": 18, "request_delay": 0},
            "godot":    {"name": "Godot Engine",    "batch_size": 25, "request_delay": 0},
            "unity":    {"name": "Unity",           "batch_size": 12, "request_delay": 1},
            "unknown":  {"name": "Авто",            "batch_size": 20, "request_delay": 0},
        }
        profile = profiles.get(game_type, profiles["unknown"])
        self._current_game_profile = profile
        self.profile_label.setText(
            f"Профиль: {profile['name']} · batch {profile['batch_size']} · delay {profile['request_delay']}с"
        )
        if self.auto_game_profile.isChecked():
            # BUG FIX: setValue triggers _on_setting_changed → _is_dirty=True.
            # Авто-выставление профиля — не действие пользователя, не должно
            # показывать ● и требовать явного сохранения.
            self._loading = True
            self.batch_size.setValue(profile["batch_size"])
            self.request_delay.setValue(profile["request_delay"])
            self._loading = False
        return profile

    def _test_api_key(self):
        """Проверить API ключ в отдельном потоке."""
        vals = self.get_values()
        provider = vals["provider"]
        api_key  = vals["api_key"]
        if not api_key:
            self.lbl_key_status.setStyleSheet("color: #EF9A9A;")
            self.lbl_key_status.setText("✗ Введите ключ")
            return

        self.btn_test_key.setEnabled(False)
        self.lbl_key_status.setStyleSheet("color: #B0BEC5;")
        self.lbl_key_status.setText("⏳ Проверяю...")

        timeout = min(vals.get("timeout", 30), 15)  # cap at 15s for key validation

        import threading

        def _run():
            try:
                if provider == "openai":
                    from openai import OpenAI
                    OpenAI(api_key=api_key, timeout=timeout).models.list()
                elif provider == "gemini":
                    try:
                        from google import genai
                        client = genai.Client(api_key=api_key)
                        list(client.models.list())
                    except Exception:
                        import google.generativeai as genai
                        genai.configure(api_key=api_key)
                        list(genai.list_models())
                elif provider == "claude":
                    import anthropic
                    anthropic.Anthropic(api_key=api_key, timeout=timeout).models.list()
                elif provider == "groq":
                    from openai import OpenAI
                    OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1",
                           timeout=timeout).models.list()
                elif provider == "deepseek":
                    from openai import OpenAI
                    OpenAI(api_key=api_key, base_url="https://api.deepseek.com",
                           timeout=timeout).models.list()
                elif provider == "custom":
                    base_url_val = self.base_url.text().strip()
                    if not base_url_val:
                        _set_status(False, "✗ Укажите Custom URL для провайдера 'custom'")
                        return
                    try:
                        from openai import OpenAI
                        OpenAI(api_key=api_key or "sk-local",
                               base_url=base_url_val, timeout=timeout).models.list()
                        _set_status(True, "✓ Custom endpoint отвечает")
                    except Exception as _ce:
                        _cm = str(_ce)
                        if "404" in _cm or "not found" in _cm.lower():
                            _set_status(True, "✓ Custom URL достижим (список моделей: 404)")
                        else:
                            _set_status(False, f"✗ Custom endpoint: {_cm[:60]}")
                    return
                _set_status(True, f"✓ {provider} — ключ действителен")
            except Exception as e:
                msg = str(e)
                if "401" in msg or "invalid" in msg.lower() or "incorrect" in msg.lower():
                    _set_status(False, "✗ Неверный ключ")
                elif "429" in msg:
                    _set_status(False, "✗ Rate limit — попробуйте позже")
                elif "not installed" in msg.lower() or "No module" in msg:
                    _set_status(False, f"✗ Пакет не установлен: pip install {provider}")
                else:
                    _set_status(False, f"✗ Ошибка: {msg[:60]}")

        def _set_status(ok: bool, text: str):
            self._key_status_signal.emit(ok, text)

        threading.Thread(target=_run, daemon=True).start()

    def _apply_key_status(self, ok: bool, text: str):
        """Вызывается из главного потока через сигнал."""
        color = "#66BB6A" if ok else "#EF9A9A"
        self.lbl_key_status.setStyleSheet(f"color: {color};")
        self.lbl_key_status.setText(text)
        self.btn_test_key.setEnabled(True)

    # ── Отслеживание несохранённых изменений ────────────────────────────────
    settings_changed = pyqtSignal()   # эмитируется при любом изменении поля

    def _connect_change_signals(self):
        """Подключить сигналы всех полей к маркеру «несохранённых изменений»."""
        for widget in (
            self.provider, self.quality_mode, self.target_lang,
        ):
            widget.currentIndexChanged.connect(self._on_setting_changed)
        for widget in (
            self.api_key, self.base_url, self.model,
        ):
            widget.textChanged.connect(self._on_setting_changed)
        for widget in (
            self.batch_size, self.max_workers, self.max_retries,
            self.request_delay, self.warp_width,
        ):
            widget.valueChanged.connect(self._on_setting_changed)
        self.apply_warp.stateChanged.connect(self._on_setting_changed)
        self.auto_game_profile.stateChanged.connect(self._on_setting_changed)
        self.use_global_tm.stateChanged.connect(self._on_setting_changed)
        self.extra.textChanged.connect(self._on_setting_changed)

    def _on_setting_changed(self):
        if not self._loading:
            self._is_dirty = True
            self._update_save_buttons()
            self.settings_changed.emit()

    def _update_save_buttons(self):
        dirty = self._is_dirty
        self._btn_apply.setEnabled(dirty)
        self._btn_revert.setEnabled(dirty)
        self._btn_apply.setText("💾 Применить *" if dirty else "💾 Применить")

    def apply_and_save(self):
        """Сохранить текущие настройки в файл конфигурации."""
        from config import load_config, save_config
        try:
            s = self.get_values()
            cfg = load_config()
            cfg.update(s)
            save_config(cfg)
            self._is_dirty = False
            self._update_save_buttons()
            # Снимаем маркер «●» с заголовка вкладки
            self.settings_changed.emit()
        except Exception as e:
            from error_utils import format_exception_details
            print(format_exception_details(e, context="SettingsPanel.apply_and_save"))

    def revert(self):
        """Откатить к последним сохранённым настройкам."""
        from config import load_config
        try:
            cfg = load_config()
            self.load_from_config(cfg)
            self._is_dirty = False
            self._update_save_buttons()
            self.settings_changed.emit()
        except Exception as e:
            from error_utils import format_exception_details
            print(format_exception_details(e, context="SettingsPanel.revert"))

    def get_values(self) -> dict:
        from ai_translator import QUALITY_MODES
        mode = self.quality_mode.currentText()
        preset = QUALITY_MODES.get(mode, QUALITY_MODES["balanced"])
        return {
            "provider":           self.provider.currentText(),
            "api_key":            self.api_key.text().strip(),
            "base_url":           self.base_url.text().strip(),
            "model":              self.model.text().strip() or preset["models"].get(self.provider.currentText(), ""),
            "source_lang":        "auto",
            "target_lang":        self.target_lang.currentText().strip() or "Russian",
            "batch_size":         self.batch_size.value(),
            "max_workers":        self.max_workers.value(),
            "max_retries":        self.max_retries.value(),
            "request_delay":      self.request_delay.value(),
            "apply_warp":         self.apply_warp.isChecked(),
            "warp_width":         self.warp_width.value(),
            "extra_instructions": self.extra.toPlainText().strip(),
            "quality_mode":       mode,
            "use_context":        preset["use_context"],
            "auto_game_profile":  self.auto_game_profile.isChecked(),
            "use_global_tm":      self.use_global_tm.isChecked(),
            "timeout":            60,   # TranslatorConfig.timeout default; not user-configurable yet
        }

    def load_from_config(self, cfg: dict):
        self._loading = True          # подавляем _on_setting_changed во время загрузки
        self._user_changed_params = True  # загрузка = пользователь задал параметры
        self.provider.setCurrentText(cfg.get("provider", "openai"))
        self.api_key.setText(cfg.get("api_key", ""))
        self.base_url.setText(cfg.get("base_url", ""))
        self.model.setText(cfg.get("model", ""))
        target = cfg.get("target_lang", "Russian")
        idx = self.target_lang.findText(target)
        if idx >= 0:
            self.target_lang.setCurrentIndex(idx)
        self.batch_size.setValue(int(cfg.get("batch_size", 25)))
        self.max_workers.setValue(int(cfg.get("max_workers", 3)))  # BUG FIX: was missing
        self.max_retries.setValue(int(cfg.get("max_retries", 3)))
        self.request_delay.setValue(int(cfg.get("request_delay", 0)))
        self.apply_warp.setChecked(bool(cfg.get("apply_warp", True)))
        self.warp_width.setValue(int(cfg.get("warp_width", 400)))
        self.extra.setPlainText(cfg.get("extra_instructions", ""))
        self.quality_mode.setCurrentText(cfg.get("quality_mode", "balanced"))
        self.auto_game_profile.setChecked(bool(cfg.get("auto_game_profile", True)))
        self.use_global_tm.setChecked(bool(cfg.get("use_global_tm", True)))
        self._sync_parameter_state(self.auto_game_profile.isChecked())
        self._loading  = False
        self._is_dirty = False
        self._update_save_buttons()
        self.settings_changed.emit()


# ═══════════════════════════════════════════════════════════
# PATH PANEL
# ═══════════════════════════════════════════════════════════

class PathPanel(QWidget):
    paths_changed = pyqtSignal()

    _TYPE_COLOR = {
        "unity": "#9575CD",
        "rpgmv": "#66BB6A",
        "rpgmz": "#4FC3F7",
        "renpy": "#FFB74D",
        "kirikiri": "#CE93D8",
        "tyrano": "#E57373",
        "vxace": "#81C784",
        "wolf": "#FFCC02",
        "godot": "#478CBF",
        "unknown": "#EF5350",
    }
    _TYPE_ICON = {
        "unity": "🎲 Unity",
        "rpgmv": "🎮 RPG Maker MV",
        "rpgmz": "🎮 RPG Maker MZ",
        "renpy": "📖 Ren'Py",
        "kirikiri": "🌸 Kirikiri/KAG",
        "tyrano": "🦖 TyranoBuilder",
        "vxace": "🎮 RPG Maker VX/Ace",
        "wolf": "🐺 Wolf RPG",
        "godot": "🤖 Godot Engine",
        "unknown": "❓ Тип не определён",
    }

    def __init__(self):
        super().__init__()
        self._detected_type = "unknown"
        self.setAcceptDrops(True)   # drag & drop папок

        layout = QFormLayout(self)
        layout.setSpacing(8)

        # ── Папка игры ────────────────────────────────────────
        game_row = QHBoxLayout()
        self.game_dir = QLineEdit()
        self.game_dir.setPlaceholderText(
            "Корневая папка игры (Unity: Assets/ProjectSettings или *_Data, RPG Maker: data/, Ren'Py: game/)..."
        )
        self.game_dir.textChanged.connect(self._on_game_dir_changed)
        btn_g = QPushButton("📂")
        btn_g.setFixedWidth(36)
        btn_g.setToolTip("Выбрать папку игры")
        btn_g.clicked.connect(self._browse_game)
        btn_hist = QPushButton("🕒")
        btn_hist.setFixedWidth(36)
        btn_hist.setToolTip("Недавние игры")
        btn_hist.clicked.connect(self._show_history)
        btn_detect = QPushButton("🔍")
        btn_detect.setFixedWidth(36)
        btn_detect.setToolTip("Автоопределить тип игры")
        btn_detect.clicked.connect(self._detect_and_update)
        game_row.addWidget(self.game_dir)
        game_row.addWidget(btn_g)
        game_row.addWidget(btn_hist)
        game_row.addWidget(btn_detect)
        layout.addRow("Папка игры:", game_row)

        # ── Папка вывода ─────────────────────────────────────
        out_row = QHBoxLayout()
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Папка для извлечённых и переведённых файлов...")
        self.output_dir.textChanged.connect(lambda: self.paths_changed.emit())
        btn_o = QPushButton("📂")
        btn_o.setFixedWidth(36)
        btn_o.clicked.connect(self._browse_output)
        out_row.addWidget(self.output_dir)
        out_row.addWidget(btn_o)
        layout.addRow("Папка вывода:", out_row)

        # ── Тип игры ──────────────────────────────────────────
        self.engine_label = QLabel("Выберите папку игры")
        self.engine_label.setStyleSheet("color: #888; font-size: 9pt;")
        layout.addRow("Тип игры:", self.engine_label)

        self.status_label = QLabel("Выберите папки выше для начала работы")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #888; font-size: 8pt;")
        layout.addRow("Статус:", self.status_label)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if Path(path).is_dir():
            self.game_dir.setText(path)
            # Авто-выставить output рядом с игрой если не задан
            if not self.output_dir.text().strip():
                out = str(Path(path).parent / (Path(path).name + "_translated"))
                self.output_dir.setText(out)

    def _on_game_dir_changed(self):
        self._detect_and_update()
        self.paths_changed.emit()

    def _detect_and_update(self):
        game = self.game_dir.text().strip()
        if not game or not Path(game).exists():
            self.engine_label.setText("Выберите папку игры")
            self.engine_label.setStyleSheet("color: #888; font-size: 9pt;")
            self._detected_type = "unknown"
            return
        try:
            from game_detector import detect_game_type
            t = detect_game_type(Path(game))
            self._detected_type = t
            label = self._TYPE_ICON.get(t, t)
            color = self._TYPE_COLOR.get(t, "#888")
            self.engine_label.setText(label)
            self.engine_label.setStyleSheet(
                f"color: {color}; font-size: 9pt; font-weight: bold;"
            )
        except Exception:
            self.engine_label.setText("Встроенный движок")
            self.engine_label.setStyleSheet("color: #888; font-size: 9pt;")

    def _show_history(self):
        """Показать меню недавних игр."""
        from config import load_config
        from PyQt6.QtWidgets import QMenu
        cfg = load_config()
        history = cfg.get("recent_games", [])
        if not history:
            QMessageBox.information(self, "История", "История пуста.")
            return
        menu = QMenu(self)
        for entry in history:
            game = entry.get("game", "")
            name = Path(game).name if game else "?"
            action = menu.addAction(f"🎮 {name}")
            action.setData(entry)
            action.setToolTip(game)
        chosen = menu.exec(self.mapToGlobal(
            self.rect().bottomLeft()
        ))
        if chosen and chosen.data():
            entry = chosen.data()
            self.game_dir.setText(entry.get("game", ""))
            if entry.get("output"):
                self.output_dir.setText(entry.get("output", ""))

    def _browse_game(self):
        p = QFileDialog.getExistingDirectory(self, "Выберите папку с игрой")
        if p:
            self.game_dir.setText(p)

    def _browse_output(self):
        p = QFileDialog.getExistingDirectory(self, "Выберите папку для вывода")
        if p:
            self.output_dir.setText(p)

    def get_paths(self) -> tuple[str, str]:
        return self.game_dir.text().strip(), self.output_dir.text().strip()

    def get_game_type(self) -> str:
        return self._detected_type

    def load_from_config(self, cfg: dict):
        if cfg.get("game_dir"):
            self.game_dir.setText(cfg["game_dir"])
        if cfg.get("output_dir"):
            self.output_dir.setText(cfg["output_dir"])
        self.paths_changed.emit()

    def validate(self) -> tuple[bool, str]:
        game, output = self.get_paths()
        if not game:
            return False, "Укажите папку с игрой."
        if not output:
            return False, "Укажите папку для вывода."
        if not Path(game).exists():
            return False, f"Папка игры не найдена:\n{game}"
        return True, ""

    def set_status(self, text: str, color: str = "#888"):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 8pt;")


# ═══════════════════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# ЗАЩИТА ДИАГНОСТИКИ ОТ УТЕЧКИ API-КЛЮЧА
# ═══════════════════════════════════════════════════════════

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),    # Anthropic (перед общим sk-)
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),   # OpenAI project keys
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),        # OpenAI / DeepSeek
    re.compile(r"gsk_[A-Za-z0-9_-]{20,}"),       # Groq
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),       # Gemini
    re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),     # Gemini OAuth
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"), # общий Authorization header
]


def _redact_secrets(text: str, *known_secrets: str) -> str:
    """Убирает похожие на API-ключи подстроки из текста лога перед экспортом
    диагностики.

    BUG FIX: _export_diagnostics() редактирует api_key в settings ("***"),
    но текст лога (full_log_widget) экспортировался как есть. Если провайдер
    вернул ошибку авторизации, чей str(e) эхом содержит ключ (частая картина
    для 401/invalid-key ответов у части провайдеров и почти гарантированная
    для нестандартных custom-эндпоинтов), _do_translate_batch() в
    ai_translator.py логирует этот текст через `_log(f"...: {e}")` без
    какой-либо редакции — и он verbatim попадал в экспортируемый JSON, хотя
    файл прямым текстом документирован как "без ключа" и предназначен для
    отправки наружу. Редактируем известное текущее значение ключа (точное
    совпадение) плюс типовые префиксы ключей известных провайдеров (на
    случай другого/старого ключа в более ранних строках лога той же сессии).
    """
    if not text:
        return text
    for secret in known_secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PatchLingo — RPG Maker / Unity — AI-переводчик")
        self.setMinimumSize(960, 700)
        self.resize(1200, 800)
        self._worker: WorkerThread | None = None
        self._cancel_event = threading.Event()
        self._log_buffer: list[str] = []   # накапливает строки текущей сессии для записи в файл
        self._last_log_path: Path | None = None  # путь к последнему сохранённому логу

        self._setup_ui()
        self._apply_theme()
        self._current_op = ""
        self._setup_stats_timer()
        self._load_config()
        self._setup_shortcuts()

    # ─────────── UI ───────────
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        # Title
        title_row = QHBoxLayout()
        title = QLabel("PatchLingo — RPG Maker / Unity — AI-переводчик")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        title_row.addWidget(title)
        badge = QLabel("Встроенный движок · Dedup · Glossary · Resume")
        badge.setStyleSheet("color: #4FC3F7; font-size: 8pt; padding: 2px 8px; "
                            "border: 1px solid #4FC3F7; border-radius: 10px;")
        title_row.addWidget(badge)
        title_row.addStretch()
        root.addLayout(title_row)

        # Main splitter: top controls / bottom log
        splitter = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(splitter, 1)

        # ── TOP ──
        top = QWidget()
        top_h = QHBoxLayout(top)
        top_h.setSpacing(8)

        # Left: tabs
        self.tabs = QTabWidget()

        self.path_panel = PathPanel()
        self.path_panel.paths_changed.connect(self._on_paths_changed)
        self.tabs.addTab(self.path_panel, "📁 Пути")

        self.settings_panel = SettingsPanel()
        self.tabs.addTab(self.settings_panel, "⚙ Настройки")
        self.settings_panel.settings_changed.connect(self._on_settings_dirty_changed)

        self.glossary_editor = GlossaryEditor()
        self.tabs.addTab(self.glossary_editor, "📖 Глоссарий")

        self.strings_viewer = StringsViewer()
        self.strings_viewer.set_settings_provider(self.settings_panel.get_values)
        self.tabs.addTab(self.strings_viewer, "📝 Строки")

        self.stats_panel = StatsPanel()
        self.tabs.addTab(self.stats_panel, "📊 Прогресс")

        self.full_log_widget = LogWidget()
        self.tabs.addTab(self.full_log_widget, "🪵 Логи")

        top_h.addWidget(self.tabs, 1)

        # Right: pipeline buttons
        right = QWidget()
        right.setFixedWidth(200)
        right_v = QVBoxLayout(right)
        right_v.setSpacing(8)

        pipeline_box = QGroupBox("Pipeline")
        pl = QVBoxLayout(pipeline_box)
        pl.setSpacing(8)

        self.btn_extract = QPushButton("1.  Extract")
        self.btn_extract.setMinimumHeight(52)
        self.btn_extract.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.btn_extract.setToolTip(
            "Извлечь текст из data/*.json\n"
            "Создаёт glossary.json с именами актёров"
        )
        self.btn_extract.clicked.connect(self._run_extract)
        pl.addWidget(self.btn_extract)

        self.btn_translate = QPushButton("2.  Translate")
        self.btn_translate.setMinimumHeight(52)
        self.btn_translate.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.btn_translate.setToolTip(
            "Перевести через AI\n"
            "• Дедупликация строк\n"
            "• Глоссарий в промпте\n"
            "• Resume — продолжит с места остановки"
        )
        self.btn_translate.clicked.connect(self._run_translate)
        pl.addWidget(self.btn_translate)

        self.btn_insert = QPushButton("3.  Insert")
        self.btn_insert.setMinimumHeight(52)
        self.btn_insert.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.btn_insert.setToolTip(
            "Вставить перевод в игровые файлы\n"
            "Бэкап создаётся автоматически"
        )
        self.btn_insert.clicked.connect(self._run_inject)
        pl.addWidget(self.btn_insert)

        self.btn_qa_report = QPushButton("🔍  QA-отчёт")
        self.btn_qa_report.setMinimumHeight(36)
        self.btn_qa_report.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_qa_report.setToolTip(
            "Проверить переведённые строки перед Insert:\n"
            "• Потерянные коды/плейсхолдеры (critical)\n"
            "• Расхождение чисел, слишком длинный перевод (warning)\n"
            "Отчёт сохраняется в output/qa_report.json"
        )
        self.btn_qa_report.clicked.connect(self._run_qa_report)
        pl.addWidget(self.btn_qa_report)

        self.btn_stop = QPushButton("⏹  Стоп")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_stop.setToolTip("Прервать текущую операцию (после текущего батча)")
        self.btn_stop.setVisible(False)
        self.btn_stop.clicked.connect(self._request_cancel)
        pl.addWidget(self.btn_stop)

        right_v.addWidget(pipeline_box)

        tools_box = QGroupBox("Инструменты")
        tl = QVBoxLayout(tools_box)
        tl.setSpacing(5)

        btn_clear_cache = QPushButton("♻  Сброс кэша")
        btn_clear_cache.setToolTip("Сбросить кэш — перевод начнётся с нуля")
        btn_clear_cache.clicked.connect(self._clear_cache)
        tl.addWidget(btn_clear_cache)

        btn_font = QPushButton("🔤  Font таблица")
        btn_font.clicked.connect(self._gen_font)
        tl.addWidget(btn_font)

        btn_info = QPushButton("ℹ  О программе")
        btn_info.clicked.connect(self._show_info)
        tl.addWidget(btn_info)

        btn_open_dir = QPushButton("📂  Открыть папку")
        btn_open_dir.setToolTip("Открыть папку вывода в проводнике")
        btn_open_dir.clicked.connect(self._open_output_dir)
        tl.addWidget(btn_open_dir)

        btn_restore = QPushButton("↩  Восстановить")
        btn_restore.setToolTip("Восстановить оригинальные файлы из резервной копии (_backup_original/)")
        btn_restore.clicked.connect(self._restore_backup)
        tl.addWidget(btn_restore)

        btn_clear_log = QPushButton("🗑  Очистить лог")
        btn_clear_log.clicked.connect(self._clear_both_logs)
        tl.addWidget(btn_clear_log)

        self.btn_open_log = QPushButton("📄  Открыть лог")
        self.btn_open_log.setToolTip("Открыть лог последней операции в текстовом редакторе")
        self.btn_open_log.clicked.connect(self._open_last_log)
        self.btn_open_log.setEnabled(False)   # активируется после первой операции
        tl.addWidget(self.btn_open_log)

        btn_diag = QPushButton("📋  Диагностика")
        btn_diag.setToolTip(
            "Экспорт диагностической информации:\n"
            "настройки, лог, статистика, ошибки.\n"
            "Отправьте файл для анализа и улучшений."
        )
        btn_diag.clicked.connect(self._export_diagnostics)
        tl.addWidget(btn_diag)

        right_v.addWidget(tools_box)
        right_v.addStretch()

        top_h.addWidget(right)
        splitter.addWidget(top)

        # ── BOTTOM: log ──
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setSpacing(4)
        bl.setContentsMargins(0, 0, 0, 0)

        hdr = QHBoxLayout()
        lbl = QLabel("Лог выполнения")
        lbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        hdr.addWidget(lbl)
        hdr.addStretch()
        self.btn_small_log_copy = QPushButton("📋")
        self.btn_small_log_copy.setToolTip("Скопировать весь лог")
        self.btn_small_log_copy.setFixedWidth(34)
        self.btn_small_log_copy.clicked.connect(self._copy_full_log)
        self.btn_small_log_clear = QPushButton("🗑")
        self.btn_small_log_clear.setToolTip("Очистить лог")
        self.btn_small_log_clear.setFixedWidth(34)
        self.btn_small_log_clear.clicked.connect(self._clear_both_logs)
        self.btn_small_log_zoom_in = QPushButton("A+")
        self.btn_small_log_zoom_in.setToolTip("Увеличить лог")
        self.btn_small_log_zoom_in.setFixedWidth(40)
        self.btn_small_log_zoom_in.clicked.connect(lambda: self._zoom_logs(1))
        self.btn_small_log_zoom_out = QPushButton("A-")
        self.btn_small_log_zoom_out.setToolTip("Уменьшить лог")
        self.btn_small_log_zoom_out.setFixedWidth(40)
        self.btn_small_log_zoom_out.clicked.connect(lambda: self._zoom_logs(-1))
        self.btn_small_log_reset = QPushButton("100%")
        self.btn_small_log_reset.setFixedWidth(48)
        self.btn_small_log_reset.clicked.connect(self._reset_log_zoom)
        for b in [self.btn_small_log_copy, self.btn_small_log_clear, self.btn_small_log_zoom_in, self.btn_small_log_zoom_out, self.btn_small_log_reset]:
            hdr.addWidget(b)
        self.status_label = QLabel("Готов")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        hdr.addWidget(self.status_label)
        bl.addLayout(hdr)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFixedHeight(14)
        bl.addWidget(self.progress)

        self.log_widget = LogWidget()
        bl.addWidget(self.log_widget, 1)

        splitter.addWidget(bottom)
        splitter.setSizes([320, 400])

    def _setup_stats_timer(self):
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.setInterval(4000)   # 4 s — only fires when not busy
        self._is_busy = False                 # guard: never scan files during a worker run

    def _setup_shortcuts(self):
        """
        Горячие клавиши:
          F5            — Extract (извлечь тексты)
          F6            — Translate (запустить перевод)
          F7            — Inject (вставить переводы)
          Ctrl+S        — Сохранить правки (если активна вкладка «Строки»)
          Ctrl+Shift+S  — Применить настройки (если активна вкладка «Настройки»)
          Esc           — Остановить текущую операцию (если запущена)

        Restore-from-backup намеренно НЕ повешен на Ctrl+Z: это деструктивное
        действие (удаляет все переводы из игровых файлов), а Ctrl+Z — почти
        универсальный шорткат "отменить правку текста". Как WindowShortcut он
        сработал бы даже когда фокус в текстовом поле (термин глоссария,
        ячейка таблицы Строк, поле настроек), неожиданно всплывая поверх
        обычного редактирования. Функция уже доступна через явную кнопку
        «Восстановить оригинал» с диалогом подтверждения.
        """
        from PyQt6.QtGui import QShortcut, QKeySequence
        from PyQt6.QtCore import Qt

        def _make(seq, slot):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)
            return sc

        _make("F5", self._run_extract)
        _make("F6", self._run_translate)
        _make("F7", self._run_inject)

        def _ctrl_s():
            idx = self.tabs.currentIndex()
            strings_idx  = self.tabs.indexOf(self.strings_viewer)
            settings_idx = self.tabs.indexOf(self.settings_panel)
            if idx == strings_idx:
                if self.strings_viewer._dirty_files:
                    self.strings_viewer.save()
            elif idx == settings_idx:
                if self.settings_panel._is_dirty:
                    self.settings_panel.apply_and_save()

        _make("Ctrl+S", _ctrl_s)
        _make("Ctrl+Shift+S", self.settings_panel.apply_and_save)
        _make("Esc", lambda: self._request_cancel() if self._is_busy else None)



    # ─────────── THEME ───────────
    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1E1E1E;
                color: #CCCCCC;
            }
            QGroupBox {
                border: 1px solid #3C3C3C;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 8px;
                font-weight: bold;
                color: #9CDCFE;
                font-size: 10pt;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QTabWidget::pane {
                border: 1px solid #3C3C3C;
                border-radius: 4px;
                background-color: #252526;
            }
            QTabBar::tab {
                background: #2D2D2D;
                color: #AAAAAA;
                padding: 6px 14px;
                border: 1px solid #3C3C3C;
                border-bottom: none;
                border-radius: 4px 4px 0 0;
                font-size: 9pt;
            }
            QTabBar::tab:selected {
                background: #252526;
                color: #4FC3F7;
                font-weight: bold;
            }
            QPushButton {
                background-color: #0E639C;
                color: #FFFFFF;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 9pt;
            }
            QPushButton:hover  { background-color: #1177BB; }
            QPushButton:pressed { background-color: #0A4D7A; }
            QPushButton:disabled { background-color: #3C3C3C; color: #666; }

            QPushButton#btn_extract  { background-color: #1B5E20; }
            QPushButton#btn_extract:hover  { background-color: #2E7D32; }
            QPushButton#btn_translate { background-color: #0D47A1; }
            QPushButton#btn_translate:hover { background-color: #1565C0; }
            QPushButton#btn_insert   { background-color: #4A148C; }
            QPushButton#btn_insert:hover   { background-color: #6A1B9A; }
            QPushButton#btn_stop { background-color: #B71C1C; }
            QPushButton#btn_stop:hover { background-color: #D32F2F; }
            QPushButton#btn_stop:disabled { background-color: #4A1C1C; color: #888; }

            QLineEdit, QComboBox, QSpinBox, QTextEdit {
                background-color: #3C3C3C;
                color: #CCCCCC;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 4px 8px;
            }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #007ACC; }
            QProgressBar {
                background-color: #3C3C3C;
                border: 1px solid #555;
                border-radius: 3px;
                text-align: center;
                color: #CCC;
                font-size: 8pt;
            }
            QProgressBar::chunk { background-color: #007ACC; border-radius: 2px; }
            QSplitter::handle { background-color: #3C3C3C; }
            QLabel { color: #CCCCCC; }
            QCheckBox { color: #CCCCCC; }
            QCheckBox::indicator:checked {
                background-color: #007ACC;
                border: 1px solid #007ACC;
                border-radius: 2px;
            }
            QFormLayout { }
        """)
        self.btn_extract.setObjectName("btn_extract")
        self.btn_translate.setObjectName("btn_translate")
        self.btn_insert.setObjectName("btn_insert")
        self.btn_stop.setObjectName("btn_stop")

    # ─────────── HELPERS ───────────
    def _log(self, msg: str):
        self.log_widget.append_msg(msg)
        self.full_log_widget.append_msg(msg)
        append_log_to_file(msg)
        self._log_buffer.append(msg)

    def _copy_full_log(self):
        try:
            QApplication.clipboard().setText(self.full_log_widget.toPlainText())
            self._log("[Log] ✓ Лог скопирован в буфер обмена")
        except Exception as e:
            self._log(format_exception_details(e, context="UI.copy_log"))

    def _flush_session_log(self):
        """Записывает лог-буфер текущей сессии в output/logs/*.log атомарно."""
        if not self._last_log_path or not self._log_buffer:
            return
        try:
            self._last_log_path.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(self._log_buffer) + "\n"
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=self._last_log_path.parent,
                suffix=".tmp", prefix=".rpgmtl_log_",
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, self._last_log_path)
            if hasattr(self, "btn_open_log"):
                self.btn_open_log.setEnabled(True)
        except Exception:
            pass  # лог не критичен — не мешаем работе

    def _open_last_log(self):
        """Открывает последний сохранённый лог-файл в системном редакторе."""
        if not self._last_log_path or not self._last_log_path.exists():
            # Попробуем найти любой лог в папке вывода
            _, output = self.path_panel.get_paths()
            if output:
                logs_dir = Path(output) / "logs"
                logs = sorted(logs_dir.glob("rpgmtl_*.log")) if logs_dir.exists() else []
                if logs:
                    self._last_log_path = logs[-1]
                else:
                    QMessageBox.information(self, "Лог", "Нет сохранённых логов для этого проекта.")
                    return
            else:
                QMessageBox.information(self, "Лог", "Нет сохранённых логов.")
                return
        try:
            if sys.platform == "win32":
                os.startfile(str(self._last_log_path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self._last_log_path)])
            else:
                subprocess.Popen(["xdg-open", str(self._last_log_path)])
        except Exception as e:
            self._log(f"[Log] ✗ Не удалось открыть лог: {e}")

    def _clear_both_logs(self):
        self.log_widget.clear()
        self.full_log_widget.clear()

    def _zoom_logs(self, delta: int):
        self.log_widget.zoom(delta)
        self.full_log_widget.zoom(delta)

    def _reset_log_zoom(self):
        self.log_widget.set_font_size(9)
        self.full_log_widget.set_font_size(10)

    def _validate_paths(self) -> tuple[bool, str, str]:
        """Validate and return (ok, game, output). Shows warning on failure."""
        ok, err = self.path_panel.validate()
        if not ok:
            QMessageBox.warning(self, "Ошибка", err)
            return False, "", ""
        game, output = self.path_panel.get_paths()
        return True, game, output

    def _set_busy(self, busy: bool, op: str = ""):
        self._is_busy = busy
        for w in [self.btn_extract, self.btn_translate, self.btn_insert, self.btn_qa_report]:
            w.setEnabled(not busy)
        self.btn_stop.setVisible(busy)
        if busy:
            self._stats_timer.stop()          # no file scans while worker runs
            self.status_label.setText(f"⏳ {op}...")
            self.status_label.setStyleSheet("color: #FFA726; font-weight: bold;")
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)
        else:
            self.progress.setVisible(False)
            self._stats_timer.start()         # resume background stats refresh

    def _request_cancel(self):
        self._cancel_event.set()
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("⏹  Останавливаю...")
        self._log("[Pipeline] ⏹ Запрос на остановку отправлен — ждём конца текущего батча...")

    def _set_status(self, success: bool, msg: str):
        if success:
            self.status_label.setText("✓ Готово")
            self.status_label.setStyleSheet("color: #66BB6A; font-weight: bold;")
        else:
            self.status_label.setText("✗ Ошибка")
            self.status_label.setStyleSheet("color: #EF5350; font-weight: bold;")
        self.path_panel.set_status(
            msg[:100] + ("..." if len(msg) > 100 else ""),
            "#66BB6A" if success else "#EF5350",
        )

    def _run_in_thread(self, fn_factory, op: str):
        """
        fn_factory(log_cb, progress_cb, cancel_event) → callable() → (bool, str)
        All callbacks use Qt signal emission — thread-safe.
        """
        if self._worker and self._worker.isRunning():
            return
        self._cancel_event.clear()
        self._current_op = op.lower()
        self.btn_stop.setEnabled(True)
        self.btn_stop.setText("⏹  Стоп")
        self._set_busy(True, op)
        self.log_widget.sep()
        self._log(f">>> {op}")

        # Начинаем новый лог-файл для этой сессии
        self._log_buffer = []
        _, output = self.path_panel.get_paths()
        if output:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._last_log_path = Path(output) / "logs" / f"rpgmtl_{op.lower()}_{ts}.log"
        else:
            self._last_log_path = None

        signals = WorkerSignals()
        signals.log.connect(self._log)
        signals.progress.connect(self._on_progress)
        signals.finished.connect(self._on_finished)

        fn = fn_factory(signals.log.emit, signals.progress.emit, self._cancel_event)
        self._worker = WorkerThread(fn, signals)
        self._worker.start()

    def _on_progress(self, cur: int, total: int, eta: float, fname: str = ""):
        self.progress.setRange(0, total)
        self.progress.setValue(cur)
        pct = round(cur / total * 100) if total else 0
        if eta >= 0:
            mins, secs = divmod(int(eta), 60)
            hrs, mins = divmod(mins, 60)
            eta_str = f" ETA {hrs}ч {mins}м" if hrs > 0 else f" ETA {mins}м {secs}с"
            self.progress.setFormat(f"{cur}/{total} ({pct}%) {eta_str}")
        else:
            self.progress.setFormat(f"{cur}/{total} ({pct}%)")
        if fname:
            self.status_label.setText(f"⏳ {Path(fname).name}")
        if not self._is_busy:
            self._refresh_stats()

    def _on_finished(self, success: bool, msg: str):
        self._set_busy(False)
        if self._cancel_event.is_set():
            self.status_label.setText("⏹ Прервано")
            self.status_label.setStyleSheet("color: #FFA726; font-weight: bold;")
            short = msg[:100] + ("..." if len(msg) > 100 else "")
            self.path_panel.set_status(short, "#FFA726")
            if getattr(self, "_current_op", "") == "translate":
                self._log("[Translate] Перевод остановлен — повторный Translate доберёт непереведённые строки из кэша.")
        else:
            self._set_status(success, msg)
        self._log(f"<<< {'✓' if success else ('⏹' if self._cancel_event.is_set() else '✗')} {msg}")
        self.log_widget.sep()
        # ── Сохраняем лог сессии на диск ──
        self._flush_session_log()
        self._refresh_stats()
        self._refresh_glossary()
        # После Extract — автоматически загружаем строки во вкладку
        if success and getattr(self, "_current_op", "") == "extract":
            _, out = self.path_panel.get_paths()
            if out:
                self.strings_viewer.set_output_dir(out)
                self.strings_viewer.load()
                # Авто-подсказка новых глоссарных терминов
                QTimer.singleShot(500, lambda: self._suggest_glossary_terms(out))
        # Уведомление о завершении Translate
        if success and getattr(self, "_current_op", "") == "translate":
            self._notify_complete()
        # Restore: результат важен всегда (в т.ч. при частичном сбое) — показываем диалогом
        if getattr(self, "_current_op", "") == "restore":
            self._on_restore_finished(success, msg)
        # После inject — открыть папку + показать статистику непереведённых
        if success and getattr(self, "_current_op", "") == "inject":
            self._on_inject_complete()
        # Сохраняем в историю
        self._save_to_history()

    def _notify_complete(self):
        """Показать уведомление и мигнуть в таскбаре когда перевод готов."""
        try:
            from PyQt6.QtWidgets import QApplication
            # Мигание в таскбаре
            app = QApplication.instance()
            if app:
                app.alert(self, 5000)
        except Exception:
            pass
        # Звуковой сигнал (системный)
        try:
            from PyQt6.QtGui import QGuiApplication
            QGuiApplication.beep()
        except Exception:
            pass
        self.setWindowTitle("✓ Перевод завершён — PatchLingo")
        # BUG FIX: таймер сбрасывал заголовок в "PatchLingo" (другой),
        # а не в оригинальный "PatchLingo — RPG Maker / Unity — AI-переводчик"
        QTimer.singleShot(5000, lambda: self.setWindowTitle("PatchLingo — RPG Maker / Unity — AI-переводчик"))

    def _on_inject_complete(self):
        """Показать кнопку открытия папки и статистику после inject."""
        game, output = self.path_panel.get_paths()
        if not game:
            return
        # Показываем диалог с результатами
        try:
            ext_dir = Path(output) / "extracted"
            total = 0
            untranslated = 0
            for jf in ext_dir.glob("*_translated.json"):
                try:
                    entries = json.loads(jf.read_text(encoding="utf-8"))
                    for e in entries:
                        total += 1
                        if not e.get("translated", "").strip():
                            untranslated += 1
                except Exception:
                    pass
            lines = ["✓ Перевод вставлен в игру"]
            if total > 0:
                translated_pct = int((total - untranslated) / total * 100)
                lines.append(f"Переведено: {total - untranslated}/{total} ({translated_pct}%)")
            if untranslated > 0:
                lines.append(f"⚠ Без перевода: {untranslated} строк")
            lines.append(f"\nПапка игры:\n{game}")
        except Exception:
            lines = ["✓ Перевод вставлен в игру", f"\n{game}"]

        from PyQt6.QtWidgets import QMessageBox as _QMB
        box = _QMB(self)
        box.setWindowTitle("Inject завершён")
        box.setText("\n".join(lines))
        btn_open = box.addButton("📂 Открыть папку", _QMB.ButtonRole.ActionRole)
        box.addButton("OK", _QMB.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() == btn_open:
            import subprocess, platform
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", game])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", game])
            else:
                subprocess.Popen(["xdg-open", game])

    def _save_to_history(self):
        """Сохранить текущую игру в историю последних."""
        try:
            game, output = self.path_panel.get_paths()
            if not game or not Path(game).exists():
                return
            from config import load_config, save_config
            cfg = load_config()
            history = cfg.get("recent_games", [])
            entry = {"game": game, "output": output or ""}
            # Убираем дубли
            history = [h for h in history if h.get("game") != game]
            history.insert(0, entry)
            history = history[:10]  # максимум 10
            cfg["recent_games"] = history
            save_config(cfg)
        except Exception:
            pass

    def _on_paths_changed(self):
        game, output = self.path_panel.get_paths()
        self.strings_viewer.set_output_dir(output or None)
        if game:
            self.settings_panel.apply_game_profile(self.path_panel.get_game_type())
        if game and output and output != "." and Path(output).is_dir():
            self._refresh_stats()
            self._refresh_glossary()

    def _refresh_stats(self):
        if self._is_busy:           # never scan files while worker is active
            return
        ok, _ = self.path_panel.validate()
        if not ok:
            return
        game, output = self.path_panel.get_paths()
        p = Pipeline(game, output)
        try:
            stats = p.get_stats()
            self.stats_panel.refresh_stats(stats)
            unique = stats.get("unique_strings", 0)
            total = stats.get("total_strings", 0)
            if unique and total:
                self.stats_panel.set_dedup(unique, total)
        except Exception:
            pass

    def _refresh_glossary(self):
        _, output = self.path_panel.get_paths()
        if not output:
            return
        gpath = Path(output) / "glossary.json"
        if gpath.exists():
            self.glossary_editor.load_from_path(gpath)

    def _suggest_glossary_terms(self, output: str):
        """
        После Extract: ищет потенциальные термины для глоссария
        (имена собственные, часто встречающиеся непереведённые слова)
        и предлагает добавить их.

        Запускается с небольшой задержкой через QTimer.singleShot чтобы
        не блокировать обновление UI сразу после Extract.
        """
        try:
            from engine.glossary import (
                load_or_create_glossary, extract_game_terms
            )

            ext_dir = Path(output) / "extracted"
            if not ext_dir.exists():
                return

            # Собираем ПОЛНЫЕ записи (не только original) — extract_game_terms
            # ожидает list[dict] с ключами "original" и "context" для приоритизации
            # спикеров/имён. BUG FIX: раньше передавался list[str] (голые original),
            # extract_game_terms делал entry.get("original") на строке и падал с
            # AttributeError на КАЖДОМ вызове — исключение тихо гасилось внешним
            # try/except, так что фича никогда не срабатывала.
            all_entries: list[dict] = []
            for jf in ext_dir.glob("*.json"):
                if jf.name.endswith("_translated.json"):
                    continue
                try:
                    import json as _json
                    entries = _json.loads(jf.read_text(encoding="utf-8"))
                    all_entries.extend(entries)
                except Exception:
                    continue

            if not all_entries:
                return

            glossary = load_or_create_glossary(Path(output), None, None)
            suggestions = extract_game_terms(all_entries, max_terms=100)

            # Убираем термины уже в глоссарии
            new_terms = {
                k: v for k, v in suggestions.items()
                if k not in glossary.terms
            }

            if not new_terms:
                return

            # Показываем предложение только если > 3 новых термина
            if len(new_terms) < 3:
                return

            sample = list(new_terms.keys())[:8]
            sample_str = "\n  • ".join(sample)
            if len(new_terms) > 8:
                sample_str += f"\n  … и ещё {len(new_terms) - 8}"

            ans = QMessageBox.question(
                self,
                "Новые термины для глоссария",
                f"Найдено {len(new_terms)} потенциальных терминов "
                f"(имена, повторяющиеся слова):\n\n"
                f"  • {sample_str}\n\n"
                f"Добавить заготовки в глоссарий?\n"
                f"(Переводы можно указать позднее во вкладке «📖 Глоссарий»)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Yes:
                for term in new_terms:
                    glossary.terms.setdefault(term, "")  # пустой перевод-заготовка
                glossary.save(Path(output) / "glossary.json")
                self._refresh_glossary()
                self._log(
                    f"[Glossary] Добавлено {len(new_terms)} заготовок терминов. "
                    f"Заполните переводы во вкладке «📖 Глоссарий»."
                )
        except Exception as e:
            # Не критично — просто логируем
            from error_utils import format_exception_details
            self._log(format_exception_details(e, context="UI.suggest_glossary_terms"))



    # ─────────── PIPELINE ACTIONS ───────────
    def _run_extract(self):
        ok, game, output = self._validate_paths()
        if not ok:
            return

        # ── Ren'Py: предупреждение если нет .rpy, но есть .rpyc и нет unrpyc ──
        game_type = self.path_panel.get_game_type()
        if game_type == "renpy":
            game_path = Path(game)
            has_rpy  = any(game_path.rglob("*.rpy"))
            has_rpyc = any(game_path.rglob("*.rpyc"))
            if has_rpyc and not has_rpy:
                import importlib.util as _ilu
                has_unrpyc = _ilu.find_spec("unrpyc") is not None
                if not has_unrpyc:
                    QMessageBox.warning(
                        self, "Ren'Py: требуется unrpyc",
                        "В игре найдены только скомпилированные файлы (.rpyc),\n"
                        "но не найдены исходные файлы (.rpy).\n\n"
                        "Для перевода нужно декомпилировать .rpyc → .rpy:\n\n"
                        "1. Скачайте unrpyc:\n"
                        "   https://github.com/CensoredUsername/unrpyc\n\n"
                        "2. ИЛИ установите через pip:\n"
                        "   pip install unrpyc\n\n"
                        "3. После этого повторите Extract.\n\n"
                        "Extract будет запущен, но может извлечь 0 строк."
                    )

        def make_task(log_cb, progress_cb, cancel_event):
            def task():
                return Pipeline(game, output, log_callback=log_cb).extract(
                    progress_callback=progress_cb,
                )
            return task

        self._run_in_thread(make_task, "Extract")

    def _run_qa_report(self):
        ok, game, output = self._validate_paths()
        if not ok:
            return

        def make_task(log_cb, progress_cb, cancel_event):
            def task():
                report = Pipeline(game, output, log_callback=log_cb).qa_report(
                    progress_callback=progress_cb)
                msg = (
                    f"Проверено: {report['checked']}, чисто: {report['clean']}, "
                    f"critical: {report['critical_count']}, warning: {report['warning_count']}"
                )
                unreadable = report.get("unreadable_files") or []
                if unreadable:
                    # Не «✓ Готово»: часть данных не проверена.
                    return False, f"{msg}; НЕ проверено файлов: {len(unreadable)}"
                return True, msg
            return task

        self._run_in_thread(make_task, "QA-отчёт")

    def _run_translate(self):
        ok, game, output = self._validate_paths()
        if not ok:
            return
        s = self.settings_panel.get_values()
        if not s["api_key"] and s["provider"] != "custom":
            QMessageBox.warning(self, "Ошибка",
                "Укажите API ключ во вкладке '⚙ Настройки'.")
            return

        # ── Pre-flight: проверяем что Extract был запущен ──
        ext_dir = Path(output) / "extracted"
        extracted = list(ext_dir.glob("*.json")) if ext_dir.exists() else []
        untranslated = [f for f in extracted if not f.name.endswith("_translated.json")]
        if not untranslated:
            ans = QMessageBox.question(
                self, "Нет файлов для перевода",
                "Папка extracted не содержит файлов для перевода.\n\n"
                "Сначала выполните Extract, чтобы извлечь текст из игры.\n\n"
                "Продолжить перевод всё равно?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        # ── Pre-flight: базовая валидация имени модели ──
        model = s["model"].strip()
        if model:
            warn = ""
            if " " in model:
                warn = f"Имя модели содержит пробел: «{model}»\nВозможно, это опечатка."
            elif len(model) > 80:
                warn = f"Имя модели подозрительно длинное ({len(model)} символов)."
            if warn:
                ans = QMessageBox.question(
                    self, "Проверьте имя модели",
                    f"{warn}\n\nПродолжить перевод?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return

        # Сохранить глоссарий и настройки перед переводом
        self.glossary_editor.save()
        self._save_current_config()

        # Pre-flight: показать оценку стоимости/токенов/времени и попросить подтверждение
        if not self._confirm_translation_estimate(output, s):
            return

        def make_task(log_cb, progress_cb, cancel_event):
            def task():
                return Pipeline(game, output, log_callback=log_cb).translate(
                    provider=s["provider"],
                    api_key=s["api_key"],
                    model=s["model"],
                    source_lang=s["source_lang"],
                    target_lang=s["target_lang"],
                    batch_size=s["batch_size"],
                    max_workers=s["max_workers"],
                    max_retries=s["max_retries"],
                    request_delay=s["request_delay"],
                    extra_instructions=s["extra_instructions"],
                    use_context=s["use_context"],
                    base_url=s["base_url"],
                    use_global_tm=s.get("use_global_tm", True),
                    progress_callback=progress_cb,
                    cancel_event=cancel_event,
                )
            return task

        self._run_in_thread(make_task, "Translate")

    def _confirm_translation_estimate(self, output: str, settings: dict) -> bool:
        """
        Pre-flight: посчитать сколько строк осталось перевести, оценить токены,
        стоимость и примерное время — и спросить подтверждение у пользователя
        ПЕРЕД запуском (а не молча писать в лог после старта).

        Возвращает True если пользователь подтвердил (или оценивать нечего —
        например, всё уже переведено), False если пользователь отменил.
        """
        try:
            from ai_translator import estimate_cost
            from engine.state import load_state

            ext_dir = Path(output) / "extracted"
            if not ext_dir.exists():
                return True  # нечего оценивать — Pipeline сам разберётся / предупредит

            # Собираем все уникальные строки, но оцениваем только те, что ещё не в кэше.
            unique_texts = []
            seen = set()
            for jf in ext_dir.glob("*.json"):
                if jf.name.endswith("_translated.json"):
                    continue
                try:
                    entries = json.loads(jf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for e in entries:
                    orig = (e.get("original") or "").strip()
                    if orig and orig not in seen:
                        seen.add(orig)
                        unique_texts.append(orig)

            if not unique_texts:
                return True  # пусто — пусть Pipeline сам сообщит об этом

            state = load_state(Path(output), log_cb=lambda _: None)
            translated_set = set(k for k, v in state.items() if v)
            for jf in ext_dir.glob("*_translated.json"):
                try:
                    entries = json.loads(jf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for e in entries:
                    orig = (e.get("original") or "").strip()
                    translated = (e.get("translated") or "").strip()
                    if orig and translated:
                        translated_set.add(orig)

            remaining_texts = [t for t in unique_texts if t not in translated_set]
            already_done = len(unique_texts) - len(remaining_texts)

            if not remaining_texts:
                # Всё уже переведено — не дёргаем пользователя подтверждением,
                # Pipeline просто пройдёт по кэшу без вызовов API.
                return True

            model = settings.get("model") or ""
            provider = settings.get("provider") or ""
            if not model:
                from ai_translator import QUALITY_MODES
                mode = getattr(self.settings_panel, "quality_mode", None)
                mode_key = mode.currentText() if mode else "balanced"
                preset = QUALITY_MODES.get(mode_key, QUALITY_MODES["balanced"])
                model = preset["models"].get(provider, "")

            tokens, cost = estimate_cost(remaining_texts, model)

            # ── Грубая оценка времени по текущим параметрам ──
            batch_size   = max(int(settings.get("batch_size", 20)), 1)
            max_workers  = max(int(settings.get("max_workers", 1)), 1)
            request_delay = float(settings.get("request_delay", 0))
            num_batches  = -(-len(remaining_texts) // batch_size)  # ceil div
            # Грубая средняя длительность одного batch-вызова API (сеть + генерация).
            # Не точная величина — у разных провайдеров/моделей сильно разнится,
            # поэтому это ориентир, а не гарантия.
            avg_call_seconds = 3.0
            total_seconds = (num_batches * (avg_call_seconds + request_delay)) / max_workers
            eta_min = total_seconds / 60.0
            if eta_min < 1:
                eta_str = "< 1 мин"
            elif eta_min < 60:
                eta_str = f"~{eta_min:.0f} мин"
            else:
                eta_str = f"~{eta_min/60:.1f} ч"

            # BUG FIX: {cost:.4f} печатал "~$0.0000" для дешёвых моделей на маленьких
            # батчах (например 20 токенов × gpt-4o-mini = $0.000006) — это выглядело
            # как "бесплатно" и было неотличимо от реально бесплатных провайдеров
            # (Gemini Flash, Groq Llama), хотя деньги там всё-таки спишутся.
            if cost <= 0:
                cost_str = "бесплатно (по текущему провайдеру)"
            elif cost < 0.0001:
                cost_str = f"~${cost:.6f} (менее $0.0001)"
            elif cost < 0.01:
                cost_str = f"~${cost:.4f}"
            else:
                cost_str = f"~${cost:.2f}"

            # Лог — для истории, дублирует то что покажем в диалоге
            self._log(
                f"[Cost] Уникальных строк: {len(unique_texts)}, "
                f"к переводу: {len(remaining_texts)}, уже переведено: {already_done}"
            )
            self._log(f"[Cost] 💰 Оценка: {cost_str} (~{tokens:,} токенов, модель: {model or '?'})")

            msg = (
                f"Будет переведено: {len(remaining_texts)} строк "
                f"(из {len(unique_texts)} уникальных, {already_done} уже готовы)\n\n"
                f"Модель: {model or '(по умолчанию для ' + provider + ')'}\n"
                f"Токенов (вход+выход): ~{tokens:,}\n"
                f"Стоимость: {cost_str}\n"
                f"Примерное время: {eta_str}\n\n"
                f"Продолжить перевод?"
            )
            ans = QMessageBox.question(
                self, "Подтверждение перевода", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            return ans == QMessageBox.StandardButton.Yes
        except Exception as e:
            # Если оценка сломалась — не блокируем перевод, просто логируем.
            from error_utils import format_exception_details
            self._log(format_exception_details(e, context="UI.confirm_translation_estimate"))
            return True

    def _run_inject(self):
        ok, game, output = self._validate_paths()
        if not ok:
            return
        s = self.settings_panel.get_values()

        # ── Pre-flight: проверяем наличие файлов с переводом ──
        ext_dir = Path(output) / "extracted"
        translated = list(ext_dir.glob("*_translated.json")) if ext_dir.exists() else []
        if not translated:
            QMessageBox.warning(
                self, "Нет переведённых файлов",
                "Папка extracted не содержит *_translated.json файлов.\n\n"
                "Сначала выполните Extract → Translate."
            )
            return

        # ── Preview: предложить посмотреть строки перед вставкой ──
        total_tr = sum(
            len([e for e in __import__("json").loads(f.read_text(encoding="utf-8"))
                 if e.get("translated", "").strip()])
            for f in translated
            if f.exists()
        )
        total_all = sum(
            len(__import__("json").loads(f.read_text(encoding="utf-8")))
            for f in translated
            if f.exists()
        )
        pct = int(total_tr / total_all * 100) if total_all else 0

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Готово к вставке")
        msg_box.setText(
            f"Переведено: {total_tr}/{total_all} строк ({pct}%)\n"
            f"Файлов с переводом: {len(translated)}\n\n"
            "Вставить перевод в файлы игры?"
        )
        msg_box.setIcon(QMessageBox.Icon.Question)
        btn_inject  = msg_box.addButton("▶  Вставить", QMessageBox.ButtonRole.AcceptRole)
        btn_preview = msg_box.addButton("👁  Просмотр строк", QMessageBox.ButtonRole.ActionRole)
        msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()

        clicked = msg_box.clickedButton()
        if clicked == btn_preview:
            # Показываем вкладку Строки и загружаем данные
            self.strings_viewer.set_output_dir(output)
            self.strings_viewer.load()
            strings_tab_idx = self.tabs.indexOf(self.strings_viewer)
            if strings_tab_idx >= 0:
                self.tabs.setCurrentIndex(strings_tab_idx)
            return   # пользователь сам нажмёт Inject после проверки
        elif clicked != btn_inject:
            return   # Отмена

        def make_task(log_cb, progress_cb, cancel_event):
            def task():
                return Pipeline(game, output, log_callback=log_cb).inject(
                    apply_warp=s["apply_warp"],
                    max_line_width=s["warp_width"],
                    progress_callback=progress_cb,
                )
            return task

        self._run_in_thread(make_task, "Inject")

    # ─────────── SETTINGS DIRTY INDICATOR ───────────
    def _on_settings_dirty_changed(self):
        """Обновляет заголовок вкладки «⚙ Настройки» — добавляет ● при несохранённых изменениях."""
        idx = self.tabs.indexOf(self.settings_panel)
        if idx < 0:
            return
        if self.settings_panel._is_dirty:
            self.tabs.setTabText(idx, "⚙ Настройки ●")
            self.tabs.setTabToolTip(idx, "Есть несохранённые изменения — нажмите «Применить»")
        else:
            self.tabs.setTabText(idx, "⚙ Настройки")
            self.tabs.setTabToolTip(idx, "")

    # ─────────── DIAGNOSTIC EXPORT ───────────
    def _export_diagnostics(self):
        """
        Экспортирует диагностический файл для анализа и улучшений.
        Содержит: настройки (без ключа), лог сессии, статистику, ошибки.
        Сохраняет в папку вывода или в домашнюю директорию.
        """
        import datetime, platform
        try:
            s = self.settings_panel.get_values()
            game, output = self.path_panel.get_paths()

            # ── Безопасные настройки (без API ключа) ──
            safe_settings = {k: v for k, v in s.items() if k != "api_key"}
            safe_settings["api_key"] = "***" if s.get("api_key") else ""

            # ── Лог сессии ──
            log_text = ""
            if hasattr(self, "full_log_widget"):
                try:
                    log_text = self.full_log_widget.toPlainText()
                except Exception:
                    pass
            log_text = _redact_secrets(log_text, s.get("api_key", ""))

            # ── Статистика ──
            stats = {}
            if game and output:
                try:
                    from pipeline import Pipeline
                    stats = Pipeline(game, output).get_stats()
                except Exception:
                    pass

            # ── Глобальная Translation Memory (статистика для диагностики) ──
            tm_stats = {}
            try:
                from global_tm import GlobalTM
                with GlobalTM(target_lang=s.get("target_lang", "Russian")) as tm:
                    full = tm.get_stats()
                    # Без top_entries — может содержать текст игры, не нужен в диагностике
                    tm_stats = {
                        "total_entries": full.get("total", 0),
                        "languages":     full.get("languages", []),
                    }
            except Exception:
                pass

            # ── Системная информация ──
            sys_info = {
                "platform":     platform.platform(),
                "python":       platform.python_version(),
                "rpgmtl":       "v50",
                "timestamp":    datetime.datetime.now().isoformat(timespec="seconds"),
                "game_type":    self.path_panel.get_game_type() if game else "unknown",
                "game_dir":     game or "",
                "output_dir":   output or "",
            }

            # ── Сборка диагностики ──
            diag = {
                "system":   sys_info,
                "settings": safe_settings,
                "stats":    stats,
                "global_tm": tm_stats,
                "log":      log_text[-8000:] if log_text else "",  # последние 8 КБ
                "errors":   [
                    line for line in log_text.splitlines()
                    if any(kw in line for kw in ("✗", "ERROR", "Ошибка", "WARN", "⚠"))
                ][-100:],  # не более 100 строк с ошибками
            }

            # ── Путь для сохранения ──
            save_dir = Path(output) if output and Path(output).is_dir() else Path.home()
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = save_dir / f"rpgmtl_diagnostics_{ts}.json"

            from engine.file_utils import atomic_write_json
            atomic_write_json(out_path, diag, indent=2)

            QMessageBox.information(
                self, "Диагностика экспортирована",
                f"Файл сохранён:\n{out_path}\n\n"
                f"Размер: {out_path.stat().st_size // 1024} КБ\n\n"
                "Отправьте этот файл для анализа и улучшений.",
            )
            self._log(f"[Diagnostics] Экспортировано → {out_path}")
        except Exception as e:
            from error_utils import format_exception_details
            QMessageBox.warning(self, "Ошибка", str(e))
            self._log(format_exception_details(e, context="UI.export_diagnostics"))

    # ─────────── TOOLS ───────────
    def _clear_cache(self):
        ok, _ = self.path_panel.validate()
        if not ok:
            QMessageBox.warning(self, "Ошибка", "Сначала выберите папки.")
            return
        reply = QMessageBox.question(
            self, "Сброс кэша",
            "Сбросить кэш переводов?\n"
            "Следующий Translate начнётся с нуля (API-запросы повторятся).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            game, output = self.path_panel.get_paths()
            p = Pipeline(game, output, self._log)
            ok2, msg = p.clear_cache()
            self._log(msg)
            self._refresh_stats()
            try:
                self.strings_viewer.load()
            except Exception:
                pass

    def _open_output_dir(self):
        _, output = self.path_panel.get_paths()
        if not output or not Path(output).is_dir():
            QMessageBox.warning(self, "Ошибка", "Папка вывода не задана или не существует.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(output)
            elif sys.platform == "darwin":
                subprocess.run(["open", output], check=False)
            else:
                subprocess.run(["xdg-open", output], check=False)
        except Exception as e:
            self._log(format_exception_details(e, context="UI.open_output_dir"))

    def _restore_backup(self):
        ok, game, _ = self._validate_paths()
        if not ok:
            return
        from engine.restore import find_backups, restore_game_backups
        game_path = Path(game)

        # Ищем все папки бэкапов (для разных движков)
        found = find_backups(game_path)
        if not found:
            QMessageBox.warning(
                self, "Нет резервной копии",
                "Папка бэкапа не найдена.\n"
                "Резервная копия создаётся автоматически при первом Insert."
            )
            return

        backup_names = "\n".join(str(d.name) for d in found)
        reply = QMessageBox.question(
            self, "Восстановить оригинал",
            f"Найдены резервные копии:\n{backup_names}\n\n"
            "ПЕРЕЗАПИШЕТ изменённые файлы игры.\n"
            "Все переводы будут удалены из игровых файлов.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Копирование — в фоновом потоке (раньше окно «зависало» на больших играх),
        # с прогрессом и честным отчётом об ошибках: см. engine/restore.py.
        def factory(log_cb, progress_cb, cancel_event):
            def run():
                result = restore_game_backups(
                    game_path, log_cb=log_cb, progress_callback=progress_cb,
                    cancel_event=cancel_event,
                )
                return result.ok, result.summary()
            return run

        self._run_in_thread(factory, "Restore")

    def _on_restore_finished(self, success: bool, msg: str):
        if success:
            QMessageBox.information(
                self, "✓ Готово", f"{msg}\nОригинальные тексты возвращены."
            )
        else:
            QMessageBox.warning(
                self, "Восстановление не завершено",
                f"{msg}\n\nЗакройте программы, использующие файлы игры, и повторите "
                "«Восстановить оригинал»: операция безопасна для повтора."
            )

    def _gen_font(self):
        self.log_widget.sep()
        self._log("[FontAnalyzer] Генерация font_widths.json...")
        try:
            from font_analyzer import generate_default_table, DEFAULT_FONT_TABLE_PATH
            table = generate_default_table()
            self._log(f"  ✓ Создано: {DEFAULT_FONT_TABLE_PATH}")
            self._log(f"  Символов: {len(table)}")
        except Exception as e:
            self._log(format_exception_details(e, context="UI.gen_font"))
        self.log_widget.sep()

    def _show_info(self):
        QMessageBox.information(self, "О программе",
            "PatchLingo — RPG Maker / Unity — AI-переводчик\n\n"
            "Лучшие практики от топ-инструментов:\n\n"
            "• Дедупликация строк (как MTool)\n"
            "  Переводит каждую уникальную строку один раз.\n"
            "  Экономия API: обычно 60–80%.\n\n"
            "• Глоссарий (как Translator++)\n"
            "  Фиксирует имена персонажей и термины.\n"
            "  Автодетект из Actors.json.\n\n"
            "• Защита escape-кодов (как SakuraTranslator)\n"
            r"  \N[1], \C[2], \B и др. заменяются плейсхолдерами"
            "\n  перед отправкой и восстанавливаются после.\n\n"
            "• Resume — продолжение прерванного перевода\n"
            "  Кэш сохраняется в translation_state.json.\n\n"
            "• Авто-бэкап перед Insert\n"
            "  Сохраняет оригиналы в _backup_*/.\n\n"
            "Поддерживаемые провайдеры:\n"
            "  OpenAI · Gemini · Claude · Groq · DeepSeek · Custom\n\n"
            "Поддерживаемые движки:\n"
            "  RPG Maker MV/MZ · VX/Ace · Ren'Py · Unity\n"
            "  Godot · Wolf RPG · TyranoBuilder · Kirikiri/KAG"
        )

    # ─────────── CONFIG ───────────
    def _load_config(self):
        try:
            cfg = load_config()
            self.settings_panel.load_from_config(cfg)
            self.path_panel.load_from_config(cfg)
            self.strings_viewer.set_output_dir(cfg.get("output_dir", ""))
            self._on_paths_changed()
            self._log(f"[Config] Настройки загружены из {Path.home() / '.rpgmtl_config.json'}")
        except Exception as e:
            self._log(format_exception_details(e, context="UI.load_config"))

    def _save_current_config(self):
        try:
            s = self.settings_panel.get_values()
            game, output = self.path_panel.get_paths()
            cfg = {**s, "game_dir": game, "output_dir": output}
            save_config(cfg)
        except Exception:
            pass

    def closeEvent(self, event):
        # BUG FIX: unsaved manual edits in the Strings tab were silently
        # discarded on close with zero warning. Settings changes are safe
        # (self._save_current_config() below saves unconditionally regardless
        # of dirty state) — but self.strings_viewer._dirty_files only ever
        # reaches disk through an explicit Save action (button or Ctrl+S,
        # see _ctrl_s() in _setup_shortcuts). Closing the window bypassed
        # both of those, so a manual edit made and never explicitly saved
        # vanished the moment the window closed.
        if self.strings_viewer._dirty_files:
            n = len(self.strings_viewer._dirty_files)
            r = QMessageBox.question(
                self, "Несохранённые изменения",
                f"В вкладке «Строки» есть несохранённые правки ({n} файл(ов)).\n\n"
                "Сохранить перед выходом?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if r == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if r == QMessageBox.StandardButton.Save:
                self.strings_viewer.save()

        self._save_current_config()
        if self._worker and self._worker.isRunning():
            r = QMessageBox.question(
                self, "Подтверждение",
                "Операция выполняется. Прервать и закрыть?\n\n"
                "Текущий батч будет дожидан, затем прогресс сохранится.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r == QMessageBox.StandardButton.Yes:
                self._cancel_event.set()
                # BUG FIX: terminate() убивает поток без flush файлов — может
                # оставить JSON наполовину записанными. Даём 5 секунд на завершение.
                if not self._worker.wait(5000):
                    # Если за 5с не завершился — только тогда принудительно
                    self._worker.terminate()
                    self._worker.wait(1000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# ═══════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("RPG Maker Translator")

    w = MainWindow()
    install_excepthook(w._log)
    # Restore a normal, visible window on first launch and request foreground
    # activation so fresh Windows installs do not leave a background-only process.
    w.showNormal()
    w.raise_()
    w.activateWindow()

    w._log("═" * 60)
    w._log("  PatchLingo — RPG Maker / Unity — AI-переводчик")
    w._log("  Встроенный движок · Без внешних зависимостей")
    w._log("═" * 60)
    w._log("")
    w._log("Workflow:")
    w._log("  1. Выберите папку игры и папку вывода  (вкладка Пути)")
    w._log("  2. Задайте API ключ                    (вкладка Настройки)")
    w._log("  3. [Опционально] Добавьте глоссарий    (вкладка Глоссарий)")
    w._log("  4. Нажмите Extract → Translate → Insert")
    w._log("")
    w._log("Особенности:")
    w._log("  • Дедупликация — экономия 60–80% API-запросов")
    w._log("  • Resume — Translate можно прерывать и продолжать")
    w._log(r"  • Защита \N[1]\C[2]\B кодов от порчи")
    w._log("  • Авто-бэкап перед Insert")
    w._log("")
    w._log("Готов к работе.")
    w.log_widget.sep()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
