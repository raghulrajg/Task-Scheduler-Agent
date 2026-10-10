@echo off
REM Robot PC (REAL mode): agents of one robot, e.g.  robot.bat robot_01
cd /d "%~dp0..\.."
venv\Scripts\python.exe launch.py robot %*
