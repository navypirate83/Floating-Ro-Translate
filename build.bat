@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\pip install -q -r requirements-dev.txt
if errorlevel 1 (
  echo.
  echo Package install failed - check your internet connection and try again.
  pause
  exit /b 1
)
.venv\Scripts\pyinstaller --noconfirm --onefile --noconsole --name RoHoverTranslate ^
  --collect-all winrt --collect-all comtypes --collect-all uiautomation ro_hover_translate.py
echo.
echo Done. Your app is at: %~dp0dist\RoHoverTranslate.exe
pause
