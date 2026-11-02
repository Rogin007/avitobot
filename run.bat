@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

echo [2/4] Installing dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency install failed. See message above.
    pause
    exit /b 1
)

echo [3/4] Checking .env...
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example. Put your token into .env now.
)

echo [4/4] Starting bot...
python main.py
echo.
echo Bot exited. Press any key to close.
pause >nul