@echo off
rem Starts AdRx and opens it in your browser. Optional port: start.bat 8001
cd /d "%~dp0"
if not exist "%~dp0scripts\start.ps1" goto notextracted
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
pause
exit /b
:notextracted
echo.
echo This file was opened from inside the zip, so the other project files are missing.
echo   1. Close this window.
echo   2. Right-click the zip file, choose "Extract All...", then click Extract.
echo   3. Open the extracted nmc-compliance-checker folder and double-click setup.bat there.
echo.
pause
exit /b 1
