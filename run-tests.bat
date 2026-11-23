@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

netstat -ano | findstr ":8080" | findstr LISTENING >nul 2>&1
if errorlevel 1 goto :nosrv

echo === running ParaBank api test suite ===
python -m pytest %*
if "%~1"=="" pause
goto :eof

:nosrv
echo [FAIL] ParaBank is not running on 8080.
echo        Start it first:  D:\parabank\start-parabank.bat
pause
goto :eof
