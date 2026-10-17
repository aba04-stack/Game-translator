@echo off
chcp 65001 >nul

:: Проверяем Python
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo ОШИБКА: Python не найден.
    echo Скачайте Python 3.11+ с https://python.org
    echo При установке отметьте "Add Python to PATH"
    pause
    exit /b 1
)

:: Устанавливаем зависимости если нужно
pip show PyQt6 >nul 2>&1
if errorlevel 1 (
    echo Первый запуск — устанавливаем зависимости...
    pip install PyQt6 openai anthropic google-generativeai --quiet
)

:: Запуск
python run.py
