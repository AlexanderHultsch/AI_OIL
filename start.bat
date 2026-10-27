@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "VENV=%LOCALAPPDATA%\AI_OIL\venv"

if not exist "%VENV%\Scripts\oil.exe" goto :not_installed

echo Starting AI_OIL - keep this window open; close it (or press Ctrl+C) to stop
"%VENV%\Scripts\oil.exe" serve
set "RC=%errorlevel%"
if not "%RC%"=="0" pause
exit /b %RC%

:not_installed
echo AI_OIL is not installed yet - double-click install.bat first
pause
exit /b 1
