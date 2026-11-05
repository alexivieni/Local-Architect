@echo off
setlocal EnableExtensions
rem  One unattended night of Architect: benchmarks, training export, logbook, morning note (tools\OVERNIGHT.md).
rem  Finds a Python the way START_HERE.bat does, then asks start.py's python_for_tests() for the one that has pytest
rem  (the evals run your projects' tests), and runs tools\overnight.py with it. Arguments are passed on, e.g.  overnight.bat --hours 5
rem  Never pauses: it is meant for Task Scheduler. No parenthesised blocks on purpose ("Program Files (x86)" would break them).

cd /d "%~dp0.."
set "ROOTDIR=%CD%"

set "PYEXE="
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "PYEXE=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined PYEXE if exist "%ROOTDIR%\.venv\Scripts\python.exe" set "PYEXE=%ROOTDIR%\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOTDIR%\venv\Scripts\python.exe" set "PYEXE=%ROOTDIR%\venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOTDIR%\..\.venv\Scripts\python.exe" set "PYEXE=%ROOTDIR%\..\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOTDIR%\..\venv\Scripts\python.exe" set "PYEXE=%ROOTDIR%\..\venv\Scripts\python.exe"
if defined PYEXE goto pick_tests
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py"
if defined PYEXE goto pick_tests
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto pick_tests
echo Python was not found on this computer; the overnight run did not start.
exit /b 1

:pick_tests
set "PYLIST=%TEMP%\architect_overnight_python.txt"
"%PYEXE%" -c "import start; print(start.python_for_tests())" > "%PYLIST%" 2>nul
set "TESTPY="
for /f "usebackq delims=" %%L in ("%PYLIST%") do set "TESTPY=%%L"
del "%PYLIST%" >nul 2>nul
if not defined TESTPY set "TESTPY=%PYEXE%"
if not exist "%TESTPY%" if not "%TESTPY%"=="py" if not "%TESTPY%"=="python" set "TESTPY=%PYEXE%"
echo Using Python: %TESTPY%
"%TESTPY%" "%ROOTDIR%\tools\overnight.py" --python "%TESTPY%" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
