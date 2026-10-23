@echo off
rem Baut KeepaliveProxy.exe (eine einzelne Datei) mit PyInstaller. Public Domain (Unlicense).
rem Voraussetzung: Python im PATH. Ergebnis: dist\KeepaliveProxy.exe
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Richte Python-Umgebung ein ...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q pystray pillow
)
.venv\Scripts\python.exe -m pip install -q pyinstaller
.venv\Scripts\python.exe make_icon.py
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name KeepaliveProxy --icon keepalive_proxy.ico --hidden-import pystray._win32 keepalive_proxy.py
echo.
if exist dist\KeepaliveProxy.exe (echo Fertig: %~dp0dist\KeepaliveProxy.exe) else (echo FEHLER: keine KeepaliveProxy.exe erzeugt)
