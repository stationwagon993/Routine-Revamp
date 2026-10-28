@echo off
rem Starts Routine Revamp and opens it in the browser.
cd /d "%~dp0"

if not exist .venv (
    echo First run: setting up Python environment...
    py -3 -m venv .venv || goto :error
    .venv\Scripts\python -m pip install --quiet --upgrade pip
)
.venv\Scripts\python -m pip install --quiet -r requirements.txt || goto :error
.venv\Scripts\python manage.py migrate --noinput || goto :error

start "" http://127.0.0.1:8000/
.venv\Scripts\python manage.py runserver 127.0.0.1:8000 --insecure --noreload
goto :eof

:error
echo.
echo Routine Revamp could not start. See the message above.
pause
