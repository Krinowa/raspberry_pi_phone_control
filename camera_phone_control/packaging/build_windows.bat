@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv-packaging\Scripts\python.exe" (
  py -3.11 -m venv .venv-packaging
  if errorlevel 1 exit /b 1
)

call ".venv-packaging\Scripts\activate.bat"
if errorlevel 1 exit /b 1

python -m pip install --upgrade pip wheel
if errorlevel 1 exit /b 1
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python -m pip install "pyinstaller>=6.10,<7" pyinstaller-hooks-contrib
if errorlevel 1 exit /b 1

python packaging\prepare_ocr_models.py
if errorlevel 1 exit /b 1
set "PHONE_CONTROLLER_BUILD_MODEL_ROOT=%USERPROFILE%\.paddlex\official_models"

python -m PyInstaller --noconfirm --clean packaging\phone_controller.spec
if errorlevel 1 exit /b 1

echo.
echo Build complete: dist\PhoneController\PhoneController.exe
endlocal
