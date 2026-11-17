@echo off
rem Builds the folder version of DMM Viewer into dist\DMM_Viewer\
rem Uses .venv if it exists, otherwise the Windows Python 3.14.
rem Needs: pip install -r requirements.txt pyinstaller
cd /d "%~dp0"

set "PY=py -3.14"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

%PY% -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name DMM_Viewer ^
  --add-data "%~dp0ui;ui" ^
  --hidden-import pyvisa_py --collect-submodules pyvisa_py --hidden-import serial ^
  --specpath build --workpath build --distpath dist ^
  app.py

if errorlevel 1 (
  echo.
  echo Build FAILED.
  exit /b 1
)
echo.
echo Done: dist\DMM_Viewer\DMM_Viewer.exe
