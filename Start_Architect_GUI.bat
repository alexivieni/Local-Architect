@echo off
title Local-Architect v12 GUI
set OLLAMA_FLASH_ATTENTION=1
set OLLAMA_KV_CACHE_TYPE=q8_0
cd /d "%~dp0"
if exist "C:\Users\alexi\AppData\Local\Programs\Python\Python312\python.exe" (
    "C:\Users\alexi\AppData\Local\Programs\Python\Python312\python.exe" architect.py start
) else (
    python architect.py start
)
