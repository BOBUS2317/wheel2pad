@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\installed.ok" (
    echo Сначала запустите start.bat — он всё установит.
    pause
    exit /b
)
echo Этот путь нужно добавить в HidHide (вкладка Applications):
echo.
".venv\Scripts\python" wheel2pad.py --where
echo.
echo Он уже СКОПИРОВАН. В окне выбора файла нажмите Ctrl+V, потом Enter.
pause
