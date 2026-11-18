@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 woa_installer.py %*) || (python woa_installer.py %*)
echo.
pause
