@echo off
echo ============================================================
echo   Starting TableServe Restaurant Management System
echo ============================================================
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found at .\venv.
    echo Please run: python -m venv venv and pip install -r requirements.txt
    pause
    exit /b 1
)

echo Activating virtual environment...
call .\venv\Scripts\activate.bat

echo Running Django system checks...
python manage.py check
if %errorlevel% neq 0 (
    echo [ERROR] Django system check failed.
    pause
    exit /b %errorlevel%
)

echo.
echo ============================================================
echo   Server is starting at http://127.0.0.1:8000/
echo   - Customer Site:    http://127.0.0.1:8000/
echo   - Admin Panel:      http://127.0.0.1:8000/admin-login/
echo   - Staff Panel:      http://127.0.0.1:8000/login/
echo   - Kitchen Board:    http://127.0.0.1:8000/kitchen/
echo   - Django Admin:     http://127.0.0.1:8000/django-admin/
echo ============================================================
echo.
python manage.py runserver 127.0.0.1:8000
pause
