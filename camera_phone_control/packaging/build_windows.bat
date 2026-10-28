@echo off
setlocal
cd /d "%~dp0\.."

py -3.13 -c "import struct,sys; assert sys.version_info[:3] == (3, 13, 13) and struct.calcsize('P') == 8, 'CPython 3.13.13 x64 is required'"
if errorlevel 1 exit /b 1

if not exist ".venv-packaging-3.13.13\Scripts\python.exe" (
  py -3.13 -m venv .venv-packaging-3.13.13
  if errorlevel 1 exit /b 1
)

call ".venv-packaging-3.13.13\Scripts\activate.bat"
if errorlevel 1 exit /b 1

python -c "import struct,sys; assert sys.version_info[:3] == (3, 13, 13) and struct.calcsize('P') == 8, 'CPython 3.13.13 x64 is required'"
if errorlevel 1 exit /b 1

python -m pip install --upgrade "pip==25.3" "wheel==0.45.1"
if errorlevel 1 exit /b 1
python -m pip install --only-binary=:all: -r packaging\requirements-windows.txt
if errorlevel 1 exit /b 1

python -m pip check
if errorlevel 1 exit /b 1

python packaging\validate_workflow_seeds.py packaging\workflow_seeds
if errorlevel 1 exit /b 1

set "PHONE_CONTROLLER_BUILD_CACHE=%CD%\build\paddlex"
python packaging\prepare_ocr_models.py --cache-root "%PHONE_CONTROLLER_BUILD_CACHE%"
if errorlevel 1 exit /b 1
set "PHONE_CONTROLLER_BUILD_MODEL_ROOT=%PHONE_CONTROLLER_BUILD_CACHE%\official_models"
if not defined APP_VERSION set "APP_VERSION=development"
set "PHONE_CONTROLLER_BUILD_VERSION=%APP_VERSION%"

if not exist "packaging\vendor\ffmpeg.exe" (
  echo WARNING: packaging\vendor\ffmpeg.exe is absent.
  echo Stable DirectShow camera-name mapping will use PATH fallback only.
  echo Supply an approved ffmpeg.exe before a production release build.
)

python -m PyInstaller --noconfirm --clean packaging\phone_controller.spec
if errorlevel 1 exit /b 1

echo.
echo Build complete: dist\PhoneController\PhoneController.exe
endlocal
