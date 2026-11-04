@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Architect

rem  This file only finds the right Python and starts start.py (the menu).
rem  Order: the virtual environment active in this window, then a .venv or venv here
rem  or in the folder above (that is where your app's packages are), then py, then python.
rem  No parenthesised blocks are used on purpose: a folder such as "Program Files (x86)" would break them.

set "PYEXE="
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "PYEXE=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0venv\Scripts\python.exe" set "PYEXE=%~dp0venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0..\venv\Scripts\python.exe" set "PYEXE=%~dp0..\venv\Scripts\python.exe"
if defined PYEXE goto run_exe
where py >nul 2>nul
if not errorlevel 1 goto run_py
where python >nul 2>nul
if not errorlevel 1 goto run_python

echo.
echo Python was not found on this computer.
echo Install Python 3.10 or newer from python.org and tick "Add python.exe to PATH",
echo or open this folder in a terminal where your virtual environment is active and run:
echo     python start.py
echo.
pause
exit /b 1

:run_exe
echo Using Python: %PYEXE%
"%PYEXE%" start.py %*
goto finish

:run_py
py -3 start.py %*
goto finish

:run_python
python start.py %*

:finish
if errorlevel 1 pause
endlocal
exit /b 0
