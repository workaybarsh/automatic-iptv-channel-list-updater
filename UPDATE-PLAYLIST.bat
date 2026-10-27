@echo off
chcp 65001 >nul
title IPTV Playlist Updater
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)

if not defined PY (
    echo.
    echo ERROR: Python was not found.
    echo.
    echo Install Python 3.9 or newer:
    echo https://www.python.org/downloads/
    echo.
    echo During installation, tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0playlist_updater.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Finished with errors ^(code: %RC%^).
echo You can close this window.
pause
exit /b %RC%
