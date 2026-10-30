@echo off
setlocal
cd /d "%~dp0"
if not defined NCC_PYTHON set "NCC_PYTHON=python"
"%NCC_PYTHON%" -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P') == 8, 'Python 3.12+ x64 required'"
if errorlevel 1 exit /b 1
"%NCC_PYTHON%" -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" build_exe.py
if errorlevel 1 exit /b 1
echo Built: %~dp0dist\NAS_Comic_Compressor.exe
endlocal
