@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 munch_installer.py %*) || (python munch_installer.py %*)
echo.
pause
