@echo off
REM Central server (REAL mode): scheduler + reasoning agent. Ctrl+C to stop.
cd /d "%~dp0..\.."
venv\Scripts\python.exe launch.py server %*
