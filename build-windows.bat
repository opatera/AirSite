@echo off
setlocal
python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --name AirRetro --add-data "index.html;." --add-data "assets;assets" server.py
echo.
echo AirRetro.exe is ready in the dist folder.
endlocal
