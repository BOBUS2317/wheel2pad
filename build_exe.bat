@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Сборка wheel2pad.exe

if not exist ".venv\installed.ok" (
    echo Сначала один раз запустите start.bat — он поставит Python и зависимости.
    pause
    exit /b
)

echo [1/3] Ставлю PyInstaller...
".venv\Scripts\python" -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail

echo [2/3] Собираю wheel2pad.exe (1-2 минуты)...
".venv\Scripts\python" -m PyInstaller --noconfirm --clean --onefile --console ^
    --name wheel2pad --collect-all vgamepad --exclude-module vgamepad.lin ^
    --distpath build\dist --workpath build\work --specpath build wheel2pad.py
if errorlevel 1 goto fail

echo [3/3] Собираю папку для сестры...
if exist release rmdir /s /q release
mkdir release\Wheel2Pad
copy /y build\dist\wheel2pad.exe release\Wheel2Pad\ >nul
copy /y release_files\*.* release\Wheel2Pad\ >nul
powershell -NoProfile -Command "Compress-Archive -Path 'release\Wheel2Pad' -DestinationPath 'release\Wheel2Pad.zip' -Force"

echo.
echo Готово!
echo   Папка: release\Wheel2Pad
echo   Архив: release\Wheel2Pad.zip  ^<- его и отдавайте
explorer release
pause
exit /b

:fail
echo.
echo Сборка не удалась — смотрите ошибку выше.
pause
