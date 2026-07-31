@echo off
REM Kort og Godt — double-click launcher (Windows).
REM Creates the virtual environment on first run, then starts the app.
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo First run: creating virtual environment and installing dependencies...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: could not create the virtual environment.
        echo Make sure Python 3.11+ is installed and on your PATH.
        pause
        exit /b 1
    )
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
)

echo.
echo Starting Kort og Godt at http://localhost:8501
echo Close this window (or press Ctrl+C) to stop the app.
echo.
"%PY%" -m streamlit run app.py --server.headless=false --browser.gatherUsageStats=false

pause
