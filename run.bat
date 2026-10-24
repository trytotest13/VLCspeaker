@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

%PY% -c "import vlc, comtypes, pytesseract, PIL, winrt, tkinterdnd2" >nul 2>nul
if errorlevel 1 (
    echo First run: installing required Python packages...
    %PY% -m pip install -r requirements.txt
    %PY% -c "import vlc, comtypes, pytesseract, PIL, winrt, tkinterdnd2" >nul 2>nul
    if errorlevel 1 (
        echo Could not install packages automatically. Try running:
        echo    %PY% -m pip install --user -r requirements.txt
        pause
        exit /b 1
    )
)

%PY% vlcspeaker.py %*
if errorlevel 1 pause
endlocal
