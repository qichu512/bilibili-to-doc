@echo off
rem Create the desktop shortcut for bilibili-to-doc
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-shortcut.ps1"
pause
