@echo off
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else (
    echo Setting up virtual environment...
    python -m venv venv
    call "venv\Scripts\activate.bat"
    pip install -r requirements.txt
)
python run.py
endlocal
