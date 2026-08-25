@echo off
rem ============================================
rem  bilibili-to-doc - launcher (double-click to run)
rem  Auto-installs dependencies on first run; opens a console window
rem  showing status; close that window to stop the program.
rem ============================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and check "Add python.exe to PATH".
    pause
    exit /b 1
)

python -c "import yt_dlp" >nul 2>nul
if errorlevel 1 (
    echo First run: installing dependency yt-dlp - requires network...
    python -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Dependency install failed. Run manually: pip install -r requirements.txt
        pause
        exit /b 1
    )
)

python "%~dp0app.py"
if errorlevel 1 pause
