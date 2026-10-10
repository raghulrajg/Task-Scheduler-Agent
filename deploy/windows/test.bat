@echo off
REM TEST mode on the same EMQX: scheduler + simulated fleet. Ctrl+C to stop.
cd /d "%~dp0..\.."
venv\Scripts\python.exe launch.py test %*
