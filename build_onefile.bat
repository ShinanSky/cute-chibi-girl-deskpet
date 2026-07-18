@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv 2>nul
    if errorlevel 1 py -3.11 -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt

for %%F in (idle.png walk1.png walk2.png walk3.png walk4.png sleep1.png sleep2.png speak1.png speak2.png drag.png) do (
    if not exist "assets\%%F" (
        echo [ERROR] Missing assets\%%F
        pause
        exit /b 1
    )
)

python -m PyInstaller --noconfirm --clean desktop_pet.spec
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)
echo Build complete: %CD%\dist\MoeDesktopPet.exe
pause
