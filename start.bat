@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Руль как геймпад

if exist ".venv\installed.ok" goto run

echo ============================================================
echo   Первый запуск: готовлю всё нужное. Это займёт 1-3 минуты.
echo   Нужен интернет. Окно не закрывайте.
echo ============================================================
echo.

rem Подходит только официальный Python для Windows (собран MSVC, в sys.version есть "MSC").
rem Сборки MSYS2/MinGW создают venv без .venv\Scripts\python.exe, и pygame-ce под них не ставится.
rem Заглушка из Microsoft Store (WindowsApps\python.exe) на -c вернёт ошибку и тоже отсеется.
set "PY="
py -3 -c "import sys; sys.exit(0 if 'MSC' in sys.version else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto have_python
python -c "import sys; sys.exit(0 if 'MSC' in sys.version else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto have_python

echo Python на компьютере не найден. Пробую установить его автоматически...
echo.
winget --version >nul 2>&1
if errorlevel 1 goto no_winget
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
echo.
echo ============================================================
echo   Python установлен. ЗАКРОЙТЕ это окно и запустите start.bat ещё раз.
echo ============================================================
pause
exit /b

:no_winget
echo Автоматически установить не получилось.
echo Скачайте Python с сайта https://www.python.org/downloads/
echo При установке ОБЯЗАТЕЛЬНО поставьте галочку "Add python.exe to PATH".
echo Потом запустите start.bat ещё раз.
start "" "https://www.python.org/downloads/"
pause
exit /b

:have_python
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv
if errorlevel 1 goto install_failed
".venv\Scripts\python" -m pip install --upgrade pip
".venv\Scripts\python" -m pip install -r requirements.txt
if errorlevel 1 goto install_failed
echo ok> ".venv\installed.ok"
echo.
echo Всё установлено!
echo Если появлялось окно установки ViGEmBus и оно попросило перезагрузку —
echo перезагрузите компьютер и снова запустите start.bat.
echo.

:run
".venv\Scripts\python" wheel2pad.py %*
echo.
pause
exit /b

:install_failed
echo.
echo ============================================================
echo   Установка не удалась. Проверьте интернет и запустите start.bat ещё раз.
echo   Если ошибка повторяется — сфотографируйте это окно и отправьте Кириллу.
echo ============================================================
pause
exit /b
