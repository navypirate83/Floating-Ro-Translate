@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\pip install -q -r requirements.txt
.venv\Scripts\pythonw ro_hover_translate.py
