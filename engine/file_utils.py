"""
file_utils.py — общие утилиты атомарной записи файлов и поиска папок.
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def find_dir_ci(parent: Path, name: str) -> Path | None:
    """Найти подпапку case-insensitive.

    BUG FIX: На Linux "Data" и "data" — разные папки.
    RPG Maker (Windows) использует "Data", но на Linux-порте или при копировании
    папка может быть "data" (нижний регистр). Ищем оба варианта.
    """
    if not parent.is_dir():
        return None
    # Точное совпадение сначала (быстро)
    exact = parent / name
    if exact.is_dir():
        return exact
    # Case-insensitive поиск
    name_lower = name.lower()
    try:
        for child in parent.iterdir():
            if child.is_dir() and child.name.lower() == name_lower:
                return child
    except PermissionError:
        pass
    return None


def find_file_ci(parent: Path, name: str) -> Path | None:
    """Найти файл case-insensitive."""
    if not parent.is_dir():
        return None
    exact = parent / name
    if exact.is_file():
        return exact
    name_lower = name.lower()
    try:
        for child in parent.iterdir():
            if child.is_file() and child.name.lower() == name_lower:
                return child
    except PermissionError:
        pass
    return None


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Атомарная запись бинарного файла через tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".rpgmtl_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Атомарная запись текстового файла через tempfile + os.replace."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Атомарная запись JSON-файла через tempfile + os.replace."""
    atomic_write_bytes(
        path,
        json.dumps(data, ensure_ascii=False, indent=indent).encode("utf-8"),
    )
