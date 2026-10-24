@echo off
rem Build the VLCspeaker installer end to end:
rem   1. PyInstaller -> dist\vlcspeaker\ (standalone app, Piper TTS bundled)
rem   2. copy the bundled voice next to the exe (mirrors the installed layout)
rem   3. generate the WiX file list
rem   4. wix build -> dist\VLCSpeaker-Setup-<VERSION>.msi
setlocal
cd /d "%~dp0"
set VERSION=0.0.1
set MSI=dist\VLCSpeaker-Setup-%VERSION%.msi

echo [1/4] Building the standalone app with PyInstaller ...
py -3 -m PyInstaller VLCspeaker-installer.spec --noconfirm --clean
if errorlevel 1 goto :err

echo [2/4] Copying Piper voices next to the exe ...
if not exist piper\voices goto :novoices
if not exist dist\vlcspeaker\piper\voices mkdir dist\vlcspeaker\piper\voices
robocopy piper\voices dist\vlcspeaker\piper\voices /E /NFL /NDL /NJH /NJS >nul
if %errorlevel% geq 8 goto :err
:novoices

echo [3/4] Generating the WiX file list ...
py -3 wix\gen_installer.py
if errorlevel 1 goto :err

echo [4/4] Building the MSI ...
wix build -arch x64 -ext WixToolset.UI.wixext -o "%MSI%" wix\main-installer.wxs
if errorlevel 1 goto :err

echo.
echo Done: %MSI%
echo Test the app itself with:  dist\vlcspeaker\vlcspeaker.exe --selftest
exit /b 0

:err
echo.
echo BUILD FAILED
exit /b 1
