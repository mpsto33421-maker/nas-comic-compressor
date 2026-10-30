@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python 3.12+
where py >nul 2>nul && (set "PY=py -3.12") || (set "PY=python")
%PY% --version || (echo Install Python 3.12 or newer: https://www.python.org/downloads/ & pause & exit /b 1)

echo [2/4] Preparing virtual environment
if not exist .venv (%PY% -m venv .venv || (pause & exit /b 1))
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || (pause & exit /b 1)

echo [3/4] Running automated tests
python -m pytest -q tests || (echo TESTS FAILED - build aborted & pause & exit /b 1)

echo [4/4] Building single-file EXE
if not exist assets\icon.ico (python -m pip install pillow && python tools\make_icon.py)
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name NAS_Comic_Compressor ^
  --icon assets\icon.ico ^
  --exclude-module tkinter ^
  app.py || (pause & exit /b 1)

echo.
echo Done: dist\NAS_Comic_Compressor.exe
pause
