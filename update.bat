@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "VENV=%LOCALAPPDATA%\AI_OIL\venv"

where git >nul 2>&1
if errorlevel 1 goto :no_git

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto :no_checkout

set "DIRTY="
for /f "delims=" %%L in ('git status --porcelain 2^>nul') do set "DIRTY=1"
if defined DIRTY goto :dirty

echo Fetching the latest AI_OIL ...
git pull --ff-only
if errorlevel 1 goto :pull_failed

if not exist "%VENV%\Scripts\python.exe" goto :no_venv

call "%~dp0installer\pip_install.cmd"
if errorlevel 1 goto :install_failed

echo.
echo Updated. Database changes are applied automatically the next time AI_OIL starts.
echo Restart start.bat if it is running.
pause
exit /b 0

:no_git
echo Updates need Git for Windows: https://git-scm.com/download/win
echo (or download the new ZIP and run install.bat)
pause
exit /b 1

:no_checkout
echo This AI_OIL folder is not a Git checkout, so it cannot be updated here.
echo Updates need Git for Windows: https://git-scm.com/download/win
echo (or download the new ZIP and run install.bat)
pause
exit /b 1

:dirty
echo You have local changes in the AI_OIL folder - update stopped so nothing is lost
echo.
git status --short
echo.
echo See docs\install_windows.md, section "Updating", for what to do with them.
pause
exit /b 1

:pull_failed
echo.
echo The update could not be downloaded or does not fit onto your copy.
echo Check the network connection and the message above. Nothing was changed.
pause
exit /b 1

:no_venv
echo.
echo The code is updated, but AI_OIL is not installed on this computer yet - run install.bat
pause
exit /b 1

:install_failed
echo.
echo The code is updated, but the packages could not be installed.
echo See docs\install_windows.md, section "Troubleshooting".
pause
exit /b 1
