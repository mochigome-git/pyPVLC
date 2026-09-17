@echo off
cd /d "%~dp0"

python -m PyInstaller --noconfirm --onedir --windowed ^
  --collect-data ttkbootstrap ^
  main.py

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

:: Runtime files must sit next to main.exe (dist\main), not in _internal
copy /Y accept.png dist\main\
copy /Y close.png  dist\main\
copy /Y ca.pem     dist\main\
copy /Y .env       dist\main\
if exist config.ini copy /Y config.ini dist\main\

echo.
if exist "dist\main\_internal\ttkbootstrap\assets\icons\bootstrap.ttf" (
    echo OK: ttkbootstrap assets bundled.
) else (
    echo WARNING: bootstrap.ttf missing from build!
)