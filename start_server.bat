@echo off
cd /d "%~dp0"
python -m backend.app.main --serve --host 127.0.0.1 --port 8001
pause