@echo off
setlocal
set "APP_VERSION=%~1"
if "%APP_VERSION%"=="" set "APP_VERSION=0.1.0"

python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --name AirRetro --add-data "index.html;." --add-data "assets;assets" server.py
if errorlevel 1 exit /b 1

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 is required to build the installer.
  echo Install it with: winget install --id JRSoftware.InnoSetup --exact
  exit /b 1
)

"%ISCC%" /DMyAppVersion=%APP_VERSION% installer\airretro.iss
if errorlevel 1 exit /b 1
echo.
echo AirRetro.exe and AirRetro-Setup-%APP_VERSION%.exe are ready in the dist folder.
endlocal
