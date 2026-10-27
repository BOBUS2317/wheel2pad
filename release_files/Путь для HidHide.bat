@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Этот путь нужно добавить в HidHide (вкладка Applications).
echo Он уже СКОПИРОВАН: в окне выбора файла нажми Ctrl+V, потом Enter.
echo.
wheel2pad.exe --where
