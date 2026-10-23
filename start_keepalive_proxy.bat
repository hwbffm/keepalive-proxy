@echo off
rem Startet den Keepalive-Proxy aus dem Quellcode ohne Konsolenfenster. Public Domain (Unlicense).
rem Eigene Adressen: start_keepalive_proxy.bat --node 192.168.10.50:4403 --interval 60
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    echo Richte Python-Umgebung ein ...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q pystray pillow
)
start "" .venv\Scripts\pythonw.exe keepalive_proxy.py %*
