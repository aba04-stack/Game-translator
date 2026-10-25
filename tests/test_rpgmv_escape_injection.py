"""Regression coverage for RPG Maker MV/MZ event text injection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.extractor import RPGMVExtractor
from engine.injector import _inject_dialog, _inject_scroll


def test_event_injection_preserves_literal_newline_control_codes():
    original = r"First line\nSecond line."
    translated = r"Первая строка\nВторая строка."

    for header_code, line_code, suffix, inject in (
        (101, 401, "dialog", _inject_dialog),
        (105, 405, "scroll", _inject_scroll),
    ):
        commands = [
            {"code": header_code, "parameters": []},
            {"code": line_code, "parameters": [original]},
        ]
        extractor = RPGMVExtractor("/tmp")
        extractor._extract_event_list(
            commands, "Map001.json", "events[3].pages[0].list"
        )

        assert len(extractor.entries) == 1
        entry = extractor.entries[0]
        assert entry.original == original
        assert entry.path.endswith(f"[1..1].{suffix}")

        data = {
            "events": [
                None,
                None,
                None,
                {"pages": [{"list": commands}]},
            ]
        }
        assert inject(data, entry.path, translated, entry.original)
        assert commands[1]["parameters"][0] == translated
