#!/bin/bash
cd "$(dirname "$0")"

if ! command -v python3 &> /dev/null; then
    echo "ОШИБКА: Python 3 не установлен."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Создание виртуального окружения..."
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt
python3 rpgmtl_desktop.py
