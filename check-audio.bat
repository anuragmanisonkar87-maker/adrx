@echo off
rem Finds out why audio notes or videos fail. Optional: check-audio.bat --file "C:\path\note.ogg"
cd /d "%~dp0"
if not exist "%~dp0app\check_audio.py" goto notextracted
if not exist ".venv\Scripts\python.exe" goto nosetup
".venv\Scripts\python.exe" -m app.check_audio %*
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
