@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0web_server.py" --host 0.0.0.0 --open-browser
pause
