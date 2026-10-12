@echo off
REM Scanner PC (REAL mode): MQTT bridge for one QC station, e.g.  scanner.bat qc_01
REM The inspection program comes from SCANNER_COMMAND in .env (or add --command "...").
cd /d "%~dp0..\.."
venv\Scripts\python.exe launch.py scanner %*
