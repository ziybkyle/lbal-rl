@echo off
setlocal
title Luck be a Landlord TUI
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
py -3.12 "%~dp0tui_play.py" %*
echo.
if errorlevel 1 echo The TUI exited with an error. See the message above.
pause
