@echo off
cd /d "%~dp0"
echo Starting Trandia Backend...
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
pause
