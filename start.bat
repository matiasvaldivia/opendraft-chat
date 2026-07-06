@echo off
REM ============================================================
REM  OpenDraft Chat - one-click launcher
REM ============================================================
setlocal ENABLEDELAYEDEXPANSION

set ROOT=%~dp0..
if "%ROOT:~-1%"=="\" set ROOT=%ROOT:~0,-1%
cd /d "%ROOT%"

echo.
echo ============================================================
echo   OpenDraft Chat - launching
echo ============================================================
echo.

REM ---- Check Python ----
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Download from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ---- Clone opendraft if missing ----
if not exist "opendraft\.git" (
    if not exist "opendraft\engine\draft_generator.py" (
        echo [SETUP] Cloning opendraft engine ...
        git clone https://github.com/federicodeponte/opendraft.git opendraft
        if errorlevel 1 (
            echo [ERROR] Failed to clone opendraft. Check your internet or git.
            pause
            exit /b 1
        )
    )
)

REM ---- Create .env from .env.example if missing ----
if not exist "opendraft\.env" (
    if exist "opendraft\.env.example" (
        copy /Y "opendraft\.env.example" "opendraft\.env" >nul
    ) else if exist ".env.example" (
        copy /Y ".env.example" "opendraft\.env" >nul
    )
    echo.
    echo  ----------------------------------------------------------------
    echo   IMPORTANT: open opendraft\.env and add your API keys:
    echo     - OPENAI_API_KEY (or GOOGLE_API_KEY)
    echo     - OPENALEX_EMAIL
    echo     - SEMANTIC_SCHOLAR_API_KEY (free, optional but recommended)
    echo  ----------------------------------------------------------------
    echo.
    pause
)

REM ---- Venv ----
if not exist ".venv\pyvenv.cfg" (
    echo [SETUP] Creating Python virtual environment ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv
        pause
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat" >nul

REM ---- Install deps ----
echo [INSTALL] Backend deps (first run may take a minute) ...
python -m pip install --quiet --disable-pip-version-check -r backend\requirements.txt 2>nul
if errorlevel 1 (
    echo [WARN] Some backend deps failed; retrying verbose...
    python -m pip install --disable-pip-version-check -r backend\requirements.txt
)

echo [INSTALL] Opendraft engine deps ...
python -m pip install --quiet --disable-pip-version-check -r opendraft\requirements.txt 2>nul
if errorlevel 1 (
    echo [WARN] Some opendraft deps failed; retrying verbose...
    python -m pip install --disable-pip-version-check -r opendraft\requirements.txt
)

echo.
echo ============================================================
echo   OpenDraft Chat is starting ...
echo   Open http://127.0.0.1:8000 in your browser
echo   Press Ctrl+C to stop
echo ============================================================
echo.

cd backend
python -m uvicorn server:app --host 127.0.0.1 --port 8000

if errorlevel 1 (
    echo.
    echo [ERROR] Server crashed. See traceback above.
    pause
)

endlocal