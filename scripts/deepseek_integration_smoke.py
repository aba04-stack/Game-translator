#!/usr/bin/env python3
"""Manual one-request DeepSeek integration smoke test for the real game pipeline.

The API key is read only from DEEPSEEK_API_KEY and is never printed or written.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

from ai_translator import (
    DeepSeekProvider,
    TranslatorConfig,
    _validate_engine_tokens,
)
from pipeline import Pipeline

SAMPLE_TEXTS = [
    r"Hello, {player}! You have %s gold.",
    r"Press \N[1] to continue.",
    r"First line\nSecond line.",
    r"Open <color=#FF00FF>the portal</color> with \C[2]magic\C[0]! Symbols: ★ ©",
]
SAMPLE_SET = set(SAMPLE_TEXTS)


class SmokeCheckError(RuntimeError):
    """A safe, human-readable failure that contains no credential data."""


def _write_fixture(game_dir: Path) -> None:
    data_dir = game_dir / "data"
    data_dir.mkdir(parents=True)
    (game_dir / "www").mkdir()
    (data_dir / "System.json").write_text(
        json.dumps({"gameTitle": "Game Translator smoke fixture"}, ensure_ascii=False),
        encoding="utf-8",
    )
    events = [None]
    for event_id, text in enumerate(SAMPLE_TEXTS, start=1):
        events.append({
            "id": event_id,
            "name": f"Smoke {event_id}",
            "pages": [{
                "list": [
                    {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2, ""]},
                    {"code": 401, "indent": 0, "parameters": [text]},
                    {"code": 0, "indent": 0, "parameters": []},
                ]
            }],
        })
    (data_dir / "Map001.json").write_text(
        json.dumps({"events": events}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "Actors.json").write_text("[null]", encoding="utf-8")


def _read_entries(extract_dir: Path) -> list[tuple[Path, list[dict]]]:
    output = []
    for path in sorted(extract_dir.glob("*.json")):
        if path.name.endswith("_translated.json"):
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            output.append((path, rows))
    return output


def _write_translations(
    extracted: list[tuple[Path, list[dict]]],
    translations: dict[str, str],
) -> None:
    for source_path, rows in extracted:
        changed = False
        for row in rows:
            original = row.get("original", "")
            if original in translations:
                row["translated"] = translations[original]
                changed = True
        if changed:
            translated_path = source_path.with_name(f"{source_path.stem}_translated.json")
            translated_path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def _run() -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("FAIL: DEEPSEEK_API_KEY repository secret is missing.")
        return 2

    with tempfile.TemporaryDirectory(prefix="game-translator-deepseek-") as temp:
        root = Path(temp)
        game_dir = root / "game"
        output_dir = root / "workspace"
        game_dir.mkdir()
        output_dir.mkdir()
        _write_fixture(game_dir)

        pipeline = Pipeline(str(game_dir), str(output_dir))
        ok, _ = pipeline.extract()
        if not ok:
            raise SmokeCheckError("RPG Maker MV extraction failed.")

        extracted = _read_entries(output_dir / "extracted")
        found = [
            row.get("original", "")
            for _, rows in extracted
            for row in rows
            if row.get("original", "") in SAMPLE_SET
        ]
        if len(found) != len(SAMPLE_TEXTS) or set(found) != SAMPLE_SET:
            raise SmokeCheckError("Extraction did not preserve all four sample lines.")

        originals = list(SAMPLE_TEXTS)
        config = TranslatorConfig(
            provider="deepseek",
            api_key=api_key,
            model="deepseek-flash",
            source_lang="English",
            target_lang="Russian",
            game_type="rpgmv",
            batch_size=len(originals),
            max_workers=1,
            max_retries=0,
            retry_delay=0,
            request_delay=0,
            temperature=0.1,
            max_tokens=384,
            timeout=45,
            use_context=False,
        )
        provider = DeepSeekProvider(config)
        translated = provider.translate_batch(originals)
        if len(translated) != len(originals):
            raise SmokeCheckError("The API returned an incomplete batch.")

        translations: dict[str, str] = {}
        for index, (original, result) in enumerate(zip(originals, translated), start=1):
            if not result.strip():
                raise SmokeCheckError(f"Translation {index} is empty.")
            if not _validate_engine_tokens(original, result, "rpgmv"):
                raise SmokeCheckError(f"Protected tokens, escapes, tags, or symbols changed in line {index}.")
            if not any("а" <= char.lower() <= "я" or char.lower() == "ё" for char in result):
                raise SmokeCheckError(f"Line {index} does not appear to be Russian.")
            translations[original] = result

        _write_translations(extracted, translations)

        map_path = game_dir / "data" / "Map001.json"
        original_map = json.loads(map_path.read_text(encoding="utf-8"))
        expected_map = copy.deepcopy(original_map)
        for event in expected_map["events"][1:]:
            for page in event["pages"]:
                for command in page["list"]:
                    if command["code"] == 401:
                        original = command["parameters"][0]
                        if original in translations:
                            command["parameters"][0] = translations[original]

        ok, _ = pipeline.inject(apply_warp=False, backup=False)
        if not ok:
            raise SmokeCheckError("RPG Maker MV injection failed.")
        actual_map = json.loads(map_path.read_text(encoding="utf-8"))
        if actual_map != expected_map:
            raise SmokeCheckError("Injected JSON differs beyond the four translated text fields.")

        print(f"PASS: DeepSeek translated {len(translated)} RPG Maker MV strings in one API request.")
        print("PASS: placeholders, printf formats, escape codes, tags, special symbols, UTF-8 JSON, and structure were preserved.")
        for index, result in enumerate(translated, start=1):
            print(f"{index}. {result}")
        return 0


def main() -> int:
    try:
        return _run()
    except SmokeCheckError as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            print(f"FAIL: integration request or pipeline step failed (HTTP {status}; {type(exc).__name__}).")
        else:
            print(f"FAIL: integration request or pipeline step failed ({type(exc).__name__}).")
        print("The exception details were suppressed to keep credentials and request headers out of logs.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
