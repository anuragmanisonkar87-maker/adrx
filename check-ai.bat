@echo off
rem Checks that AI review works with the Anthropic key in .env. Add --quick for the free check only.
cd /d "%~dp0"
if not exist "%~dp0app\ai_check.py" goto notextracted
if not exist ".venv\Scripts\python.exe" goto nosetup
".venv\Scripts\python.exe" -m app.ai_check %*
pause
exit /b
:nosetup
echo Run setup.bat first: it installs everything the app needs.
pause
exit /b 1
:notextracted
echo.
echo This file was opened from inside the zip, so the other project files are missing.
echo   1. Close this window.
echo   2. Right-click the zip file, choose "Extract All...", then click Extract.
echo   3. Open the extracted nmc-compliance-checker folder and double-click setup.bat there.
echo.
pause
exit /b 1
