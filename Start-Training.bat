@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Set up the Python environment first. See docs\TRAINING.en.md or docs\TRAINING.zh-CN.md.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0python\training_ui.py"
