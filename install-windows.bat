@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 woa_gui.py) || (start "" pythonw woa_gui.py)
