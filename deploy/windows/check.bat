@echo off
REM Preflight: broker login, Ollama, config, robot state (nothing moves).
cd /d "%~dp0..\.."
venv\Scripts\python.exe launch.py check %*
pause
