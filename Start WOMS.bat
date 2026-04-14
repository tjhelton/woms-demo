@echo off
setlocal EnableDelayedExpansion
REM ============================================================
REM  WOMS - Work Order Management System (Windows Launcher)
REM  Double-click this file to start WOMS.
REM  Automatically installs Python if needed.
REM ============================================================

set "PYTHON_VERSION=3.12.7"

cd /d "%~dp0"

cls
echo.
echo   =============================================
echo    WOMS  -  Work Order Management System
echo   =============================================
echo.

REM ---- Find or install Python 3 ----
set "PYTHON="

REM Check if python is on PATH
where python >nul 2>nul
if %errorlevel% equ 0 (
    python -c "import sys; exit(0 if sys.version_info[0]>=3 else 1)" 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON=python"
        goto :python_found
    )
)

where python3 >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON=python3"
    goto :python_found
)

REM Check common install locations
for %%V in (312 313 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :python_found
    )
    if exist "C:\Python%%V\python.exe" (
        set "PYTHON=C:\Python%%V\python.exe"
        goto :python_found
    )
    if exist "%PROGRAMFILES%\Python%%V\python.exe" (
        set "PYTHON=%PROGRAMFILES%\Python%%V\python.exe"
        goto :python_found
    )
)

REM Python not found - install it
echo   Python 3 is not installed. Setting it up now...
echo.

set "INSTALLER=%TEMP%\python-%PYTHON_VERSION%-install.exe"
set "INSTALLER_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-amd64.exe"

echo   Downloading Python %PYTHON_VERSION%...
echo.

REM Try curl first (available on Windows 10 1803+)
curl -fSL --progress-bar -o "%INSTALLER%" "%INSTALLER_URL%" 2>nul
if not exist "%INSTALLER%" (
    REM Fallback to PowerShell
    powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%INSTALLER_URL%' -OutFile '%INSTALLER%' }" 2>nul
)

if not exist "%INSTALLER%" (
    echo   Could not download Python automatically.
    echo   Opening the Python download page in your browser...
    echo.
    echo   IMPORTANT: During install, check "Add Python to PATH"
    echo   After installing, double-click this file again.
    echo.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

echo   Installing Python (this may take a minute)...
echo.

REM Silent install for current user, with PATH setup
"%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1

if %errorlevel% neq 0 (
    echo   Silent install didn't work. Opening the installer...
    echo   IMPORTANT: Check "Add Python to PATH" at the bottom of the first screen!
    echo.
    "%INSTALLER%" PrependPath=1
)

del "%INSTALLER%" 2>nul

REM Refresh PATH from registry so we can find the new Python
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UPATH=%%b"
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SPATH=%%b"
set "PATH=%SPATH%;%UPATH%;%PATH%"

REM Re-check for Python
where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON=python"
    goto :python_found
)

REM Check the default install location directly
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :python_found
)

echo.
echo   [!] Python installation may not have completed.
echo       Please install Python from https://www.python.org/downloads/
echo       Make sure to check "Add Python to PATH" during install.
echo       Then double-click this file again.
echo.
pause
exit /b 1

:python_found
for /f "delims=" %%v in ('"%PYTHON%" --version 2^>^&1') do echo   Using %%v
echo.

REM ---- First-time setup or broken venv ----
if not exist ".venv" goto :setup_venv

call .venv\Scripts\activate.bat
"%PYTHON%" -c "import fastapi, uvicorn, multipart, httpx, aiosqlite, dotenv" 2>nul
if %errorlevel% neq 0 (
    echo   Dependencies are missing or broken. Reinstalling...
    echo.
    goto :setup_venv
)
goto :venv_ready

:setup_venv
echo   Installing app dependencies...
echo   ^(This only happens once and takes about a minute.^)
echo.
if exist ".venv" rmdir /s /q .venv
"%PYTHON%" -m venv .venv
if %errorlevel% neq 0 (
    echo   [!] Failed to create virtual environment.
    echo       Please make sure Python 3 is installed correctly.
    echo.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
pip install --quiet --disable-pip-version-check -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo   [!] Failed to install dependencies.
    echo       Please check your internet connection and try again.
    echo.
    pause
    exit /b 1
)
echo   Setup complete!
echo.

:venv_ready

REM ---- Ensure .env exists ----
if not exist ".env" copy .env.example .env >nul

REM ---- Always prompt for API token ----
set "CURRENT_TOKEN="
for /f "tokens=2 delims==" %%a in ('findstr /B "SC_API_TOKEN=" .env') do set "CURRENT_TOKEN=%%a"

echo   -------------------------------------------------------
echo   SafetyCulture API Token
echo   -------------------------------------------------------
echo.
echo   Enter your SafetyCulture API token to sync work orders.
echo   ^(Find it in SafetyCulture ^> Company Settings ^>
echo    Integrations ^> API Tokens^)
echo.
echo   Press Enter to run in demo mode ^(no live sync^).
if defined CURRENT_TOKEN (
    echo.
    echo   Current: !CURRENT_TOKEN:~0,8!...!CURRENT_TOKEN:~-4!
    echo   ^(Press Enter to keep the current token.^)
)
echo.
set "SC_TOKEN="
set /p "SC_TOKEN=  API Token: "

if defined SC_TOKEN (
    powershell -Command "(Get-Content .env) -replace 'SC_API_TOKEN=.*', 'SC_API_TOKEN=!SC_TOKEN!' | Set-Content .env"
    echo.
    echo   Token saved!
) else if not defined CURRENT_TOKEN (
    echo.
    echo   No token - running in demo mode.
)
echo.

REM ---- Launch ----
"%PYTHON%" run.py

echo.
pause
