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

rem --onefile: всё (Python, pygame, vgamepad с установщиком ViGEmBus) внутри одного exe.
rem Путь к иконке абсолютный: с --specpath относительные пути PyInstaller ищет от папки build.
echo [2/3] Собираю wheel2pad.exe (1-2 минуты)...
".venv\Scripts\python" -m PyInstaller --noconfirm --clean --onefile --console ^
    --name wheel2pad --icon "%~dp0assets\wheel2pad.ico" ^
    --collect-all vgamepad --exclude-module vgamepad.lin ^
    --distpath build\dist --workpath build\work --specpath build wheel2pad.py
if errorlevel 1 goto fail

rem Отдаётся один файл: команды R/H заменили прежние bat-ярлыки,
rem а инструкция передаётся отдельно (в репозитории её нет).
echo [3/3] Кладу exe в папку release...
if exist release rmdir /s /q release
mkdir release
copy /y build\dist\wheel2pad.exe release\ >nul

echo.
echo Готово!
echo   Файл: release\wheel2pad.exe  ^<- его и отдавайте (инструкцию - отдельно)
explorer release
pause
exit /b

:fail
echo.
echo Сборка не удалась — смотрите ошибку выше.
pause
