@echo off
setlocal
rem Launch Seque from the folder where this batch file is stored.
cd /d "%~dp0"
python "%~dp0seque.py" %*
