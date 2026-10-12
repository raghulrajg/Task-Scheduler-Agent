@echo off
REM Central server (REAL mode): agent configs, e.g. the conveyor QC station:
REM   agents.bat agents\qc_01_conveyor.json
cd /d "%~dp0..\.."
venv\Scripts\python.exe launch.py agents %*
