@echo off
chcp 65001 >nul
echo ================================================
echo  RPG Maker AI Translator — сборка EXE
echo ================================================
echo.

:: Переходим в папку скрипта (на случай пробелов в пути)
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo ОШИБКА: Python не найден.
    echo Скачайте Python 3.11+ с https://python.org
    echo При установке отметьте "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/3] Установка зависимостей...
pip install -r requirements.txt pyinstaller --quiet
if errorlevel 1 (
    echo ОШИБКА при установке зависимостей.
    pause
    exit /b 1
)
echo     OK

echo.
echo [2/3] Сборка EXE (3-5 минут)...
pyinstaller ^
  --onefile ^
  --windowed ^
  --name "RPGTranslator" ^
  --add-data "engine;engine" ^
  --hidden-import "engine" ^
  --hidden-import "engine.extractor" ^
  --hidden-import "engine.injector" ^
  --hidden-import "engine.deduplicator" ^
  --hidden-import "engine.glossary" ^
  --hidden-import "engine.state" ^
  --hidden-import "engine.restore" ^
  --hidden-import "engine.text_filter" ^
  --hidden-import "engine.unity_extractor" ^
  --hidden-import "engine.unity_injector" ^
  --hidden-import "engine.wolf_extractor" ^
  --hidden-import "engine.wolf_injector" ^
  --hidden-import "engine.vxace_extractor" ^
  --hidden-import "engine.vxace_injector" ^
  --hidden-import "engine.kirikiri_extractor" ^
  --hidden-import "engine.kirikiri_injector" ^
  --hidden-import "engine.tyrano_extractor" ^
  --hidden-import "engine.tyrano_injector" ^
  --hidden-import "engine.godot_extractor" ^
  --hidden-import "engine.godot_injector" ^
  --hidden-import "game_detector" ^
  --hidden-import "renpy_extractor" ^
  --hidden-import "renpy_injector" ^
  --hidden-import "smart_warp" ^
  --hidden-import "font_analyzer" ^
  --hidden-import "pipeline" ^
  --hidden-import "ai_translator" ^
  --hidden-import "config" ^
  --hidden-import "error_utils" ^
  --hidden-import "qa_report" ^
  --hidden-import "openai" ^
  --hidden-import "anthropic" ^
  --hidden-import "openpyxl" ^
  --hidden-import "rubymarshal.reader" ^
  --hidden-import "rubymarshal.writer" ^
  --hidden-import "rubymarshal.classes" ^
  --hidden-import "google.genai" ^
  --hidden-import "PyQt6.QtWidgets" ^
  --hidden-import "PyQt6.QtCore" ^
  --hidden-import "PyQt6.QtGui" ^
  run.py

if errorlevel 1 (
    echo.
    echo ОШИБКА при сборке. Используйте run.bat для запуска без сборки.
    pause
    exit /b 1
)

echo.
echo [3/3] Готово!
echo EXE: dist\RPGTranslator.exe
echo.
pause
