@echo off
rem  Double-click: starts Architect (Ollama if needed, the model servers, the hub) with this console window visible.
rem  Start_Architect_GUI.vbs runs this same file with the window hidden; stop a hidden hub from the hub itself or Task Manager.
rem  The two OLLAMA lines only affect the Ollama this window starts (set, not setx): nothing on your account changes.
title Local-Architect GUI
set OLLAMA_FLASH_ATTENTION=1
set OLLAMA_KV_CACHE_TYPE=q8_0
cd /d "%~dp0"
rem  Prefer a Python 3.12 (the one with pytest on this machine), then any py launcher, then python on PATH. No path is hardcoded.
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -3.12 architect.py start
    goto :eof
)
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -3 architect.py start
    goto :eof
)
python architect.py start
