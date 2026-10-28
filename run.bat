@echo off
rem Starts Routine Revamp and opens it in the browser.
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python is not installed. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this file again.
    goto :error
)

if not exist config.ini (
    copy config.example.ini config.ini >nul
    echo Created config.ini. Edit it later to point at the network drive.
)

if not exist .venv (
    echo First run: setting up. This takes a minute or two...
    py -m venv .venv || goto :error
    .venv\Scripts\python -m pip install --quiet --upgrade pip
)
.venv\Scripts\python -m pip install --quiet -r requirements.txt || goto :error
.venv\Scripts\python manage.py migrate --noinput || goto :error

.venv\Scripts\python manage.py shell -c "from django.contrib.auth.models import User; import sys; sys.exit(0 if User.objects.exists() else 1)"
if errorlevel 1 (
    echo.
    echo No accounts yet. Create the first admin account now.
    .venv\Scripts\python manage.py createsuperuser || goto :error
)

echo.
echo Routine Revamp is running at http://127.0.0.1:8000/
echo Keep this window open while you use it. Close it to stop the app.
start "" http://127.0.0.1:8000/
.venv\Scripts\python manage.py runserver 127.0.0.1:8000 --insecure --noreload
goto :eof

:error
echo.
echo Routine Revamp could not start. See the message above.
pause
