@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================================
echo  AI_OIL uninstaller
echo ================================================================
echo.
echo This removes AI_OIL from this computer in two separate steps:
echo   1. the program environment - it can be created again with install.bat
echo   2. your data - only if you type DELETE
echo.
echo The AI_OIL folder that contains this file is never touched.
echo Close start.bat first if AI_OIL is running.
echo.

choice /c YN /m "Remove the AI_OIL program environment (%LOCALAPPDATA%\AI_OIL\venv)?"
if errorlevel 2 goto :ask_data
if not exist "%LOCALAPPDATA%\AI_OIL\venv" goto :no_venv
rmdir /s /q "%LOCALAPPDATA%\AI_OIL\venv"
echo The program environment was removed.
goto :ask_data

:no_venv
echo No program environment found.

:ask_data
echo.
if defined AI_OIL_HOME echo Your data folder was moved to %AI_OIL_HOME% (AI_OIL_HOME) - it is not removed here; delete it yourself if you no longer need it
echo Your data (database, configuration, stored files) is in %LOCALAPPDATA%\AI_OIL. Backups in OneDrive are NOT touched.
echo That folder also holds the program environment, so removing it removes both.
set "ANSWER="
set /p "ANSWER=Type DELETE to remove your data as well, or press Enter to keep it: "
if "%ANSWER%"=="DELETE" if exist "%LOCALAPPDATA%\AI_OIL" rmdir /s /q "%LOCALAPPDATA%\AI_OIL"
if "%ANSWER%"=="DELETE" echo Your data was removed.
if not "%ANSWER%"=="DELETE" echo Your data was kept.

echo.
echo Your API tokens stay in Windows Credential Manager (Control Panel -^> Credential Manager -^> Windows Credentials, entries containing ai_oil); remove them there if you no longer need them.
echo The AI_OIL folder with the program code can simply be deleted.
pause
exit /b 0
