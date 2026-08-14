@echo off
setlocal
rem Launch Seque from the folder where this batch file is stored.
cd /d "%~dp0"
start "" /b pythonw "%~dp0seque.py" %*
