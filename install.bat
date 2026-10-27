@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "VENV=%LOCALAPPDATA%\AI_OIL\venv"

echo ================================================================
echo  AI_OIL installer
echo ================================================================
echo.
echo This will:
echo   1. create a Python environment in %LOCALAPPDATA%\AI_OIL\venv
echo   2. install AI_OIL into it
echo   3. start the setup wizard, which asks a few questions
echo.
echo Your data stays in %LOCALAPPDATA%\AI_OIL. Nothing needs admin rights.
echo.
echo If Windows warned about this file: More info -^> Run anyway,
echo or right-click -^> Properties -^> Unblock.
echo.

rem --- Python 3.12 ----------------------------------------------------------------
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto :no_python

rem --- Existing environment -------------------------------------------------------
if exist "%VENV%\Scripts\python.exe" goto :existing
if exist "%VENV%" goto :broken_venv
goto :create

:existing
choice /c RSQ /m "AI_OIL is already installed. [R]einstall packages, run [S]etup only, or [Q]uit"
rem choice returns the position of the key: R=1, S=2, Q=3. Test the highest first.
if errorlevel 3 goto :quit
if errorlevel 2 goto :setup
goto :install

:broken_venv
echo The environment folder is incomplete - removing it and creating it again.
rmdir /s /q "%LOCALAPPDATA%\AI_OIL\venv"

:create
echo Creating the Python environment ...
py -3.12 -m venv "%VENV%"
if errorlevel 1 goto :venv_failed

:install
call "%~dp0installer\pip_install.cmd"
if errorlevel 1 goto :install_failed

rem --- Keep the code folder on this device if it lies inside OneDrive -------------
rem Prefix test without delayed expansion: remove "<OneDrive folder>\" from HERE with
rem string substitution. If putting it back in front restores HERE exactly, the folder
rem was a prefix of HERE, so the AI_OIL folder is inside that OneDrive.
set "HERE=%~dp0"
set "IN_ONEDRIVE="
if defined OneDrive call :check_prefix "%OneDrive%"
if defined OneDriveCommercial call :check_prefix "%OneDriveCommercial%"
if defined OneDriveConsumer call :check_prefix "%OneDriveConsumer%"
if not defined IN_ONEDRIVE goto :setup
attrib +P -U "%~dp0*" /S /D >nul 2>&1
echo Pinned the AI_OIL folder so OneDrive keeps it on this device.

:setup
echo.
"%VENV%\Scripts\oil.exe" setup
set "SETUP_RC=%errorlevel%"
echo.
echo Start AI_OIL any time by double-clicking start.bat
pause
exit /b %SETUP_RC%

:check_prefix
set "ONEDRIVE_ROOT=%~1\"
call set "REST=%%HERE:%ONEDRIVE_ROOT%=%%"
if /i "%ONEDRIVE_ROOT%%REST%"=="%HERE%" if not "%REST%"=="%HERE%" set "IN_ONEDRIVE=1"
exit /b 0

:no_python
echo Python 3.12 was not found.
where py >nul 2>&1
if errorlevel 1 echo The "py" launcher is missing.
where python 2>nul | findstr /i "WindowsApps" >nul
if not errorlevel 1 echo "python" is only the Microsoft Store placeholder, which cannot be used.
echo.
echo Download Python 3.12 from https://www.python.org/downloads/windows/
echo Install Python 3.12 for the current user and keep "py launcher" ticked.
echo Then double-click install.bat again.
pause
exit /b 1

:venv_failed
echo.
echo ERROR: The Python environment could not be created in %LOCALAPPDATA%\AI_OIL\venv
echo See docs\install_windows.md, section "Troubleshooting".
pause
exit /b 1

:install_failed
echo.
echo Installation stopped. Nothing was configured.
pause
exit /b 1

:quit
echo Nothing was changed.
pause
exit /b 0
