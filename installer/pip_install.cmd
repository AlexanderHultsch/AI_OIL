@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem Shared by install.bat and update.bat. Expects VENV to be set by the caller.
rem Installs AI_OIL (the repository root, one folder up) into the environment.

if not defined VENV (
    echo pip_install.cmd: the VENV variable is not set - run install.bat or update.bat instead.
    exit /b 1
)

rem --- Proxy: use the Windows setting for this process only ---------------------
set "REGKEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
if defined HTTPS_PROXY goto :proxy_done
set "PROXY_ENABLE="
set "PROXY_SERVER="
set "PROXY_PAC="
for /f "tokens=3" %%A in ('reg query "%REGKEY%" /v ProxyEnable 2^>nul ^| findstr /i "ProxyEnable"') do set "PROXY_ENABLE=%%A"
for /f "tokens=3*" %%A in ('reg query "%REGKEY%" /v ProxyServer 2^>nul ^| findstr /i "ProxyServer"') do set "PROXY_SERVER=%%A"
for /f "tokens=3*" %%A in ('reg query "%REGKEY%" /v AutoConfigURL 2^>nul ^| findstr /i "AutoConfigURL"') do set "PROXY_PAC=%%A"
if /i "%PROXY_ENABLE%"=="0x1" if defined PROXY_SERVER goto :proxy_server
if defined PROXY_PAC goto :proxy_ask
goto :proxy_done

:proxy_server
rem A per-protocol value such as http=host:port;https=host:port contains an equals sign.
echo "%PROXY_SERVER%"| find "=" >nul
if not errorlevel 1 goto :proxy_ask
set "HTTPS_PROXY=http://%PROXY_SERVER%"
set "HTTP_PROXY=http://%PROXY_SERVER%"
echo Using the Windows proxy %PROXY_SERVER% for this installation only.
goto :proxy_done

:proxy_ask
echo.
echo Your Windows proxy is configured by a script, which pip cannot read.
set "PROXY_ANSWER="
set /p "PROXY_ANSWER=Proxy address (e.g. http://proxy.company.com:8080) - leave empty if you have none: "
if not defined PROXY_ANSWER goto :proxy_done
set "HTTPS_PROXY=%PROXY_ANSWER%"
set "HTTP_PROXY=%PROXY_ANSWER%"
echo Using the proxy %PROXY_ANSWER% for this installation only.

:proxy_done

rem --- Install ------------------------------------------------------------------
echo.
echo Upgrading pip ...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
if not errorlevel 1 goto :pip_ready
echo pip could not be upgraded - retrying with the Windows certificate store ...
"%VENV%\Scripts\python.exe" -m pip install --use-feature=truststore --upgrade pip
if errorlevel 1 echo Note: pip could not be upgraded - continuing with the version that came with Python.

:pip_ready

echo.
echo Installing AI_OIL - attempt 1 of 3: standard install ...
"%VENV%\Scripts\python.exe" -m pip install -e "%~dp0.."
if not errorlevel 1 goto :done

echo.
echo Attempt 1 failed. Attempt 2 of 3: using the Windows certificate store ...
"%VENV%\Scripts\python.exe" -m pip install --use-feature=truststore -e "%~dp0.."
if not errorlevel 1 goto :done

if not exist "%~dp0..\wheels\" goto :failed
echo.
echo Attempt 2 failed. Attempt 3 of 3: offline, from the wheels folder ...
"%VENV%\Scripts\python.exe" -m pip install --no-index --find-links "%~dp0..\wheels" -e "%~dp0.."
if not errorlevel 1 goto :done

:failed
echo.
echo ERROR: AI_OIL could not be installed.
echo Tried: a standard install, an install with the Windows certificate store.
if exist "%~dp0..\wheels\" echo Tried: an offline install from the wheels folder.
if not exist "%~dp0..\wheels\" echo Not tried: an offline install, because there is no wheels folder in the AI_OIL folder.
echo See docs\install_windows.md, section "Troubleshooting" - and "Offline installation" if PyPI is blocked.
exit /b 1

:done
echo.
echo AI_OIL is installed in "%VENV%".
exit /b 0
