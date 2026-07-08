@echo off
REM Double-click this to launch the Pepper admin panel.
REM It runs start_pepper_admin.ps1 (which sets up the environment and opens
REM your browser at http://127.0.0.1:8080).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_pepper_admin.ps1"
pause
