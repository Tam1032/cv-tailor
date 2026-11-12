@echo off
REM Chay CV Tailor: http://127.0.0.1:8000  (cau hinh trong file .env)
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Khong tim thay python. Cai Python 3.10+ tu python.org va tick "Add python.exe to PATH". & pause & exit /b 1)
REM Tao lai .venv neu bi hong (vi du sau khi doi cho thu muc)
if exist .venv\Scripts\python.exe (.venv\Scripts\python.exe -c "import sys" >nul 2>nul || rmdir /s /q .venv)
if not exist .venv\Scripts\python.exe (echo Dang tao moi truong Python... & python -m venv .venv)
echo Dang kiem tra thu vien...
.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (echo Cai thu vien that bai - kiem tra ket noi mang roi chay lai. & pause & exit /b 1)
.venv\Scripts\python.exe -m app %*
pause
