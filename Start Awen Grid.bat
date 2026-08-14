@echo off
chcp 65001 >nul
title Awen Grid Ignition
cd /d "%~dp0"

echo.
echo   AWEN GRID - IGNITION
echo   Y Gwir yn Erbyn y Byd
echo.

rem --- clear any previous instances so nothing fights over port 5000/7777 ---
echo   [1/3] clearing old instances...
powershell -NoProfile -ExecutionPolicy Bypass -File "stop_grid.ps1"
timeout /t 2 /nobreak >nul

echo   [2/3] raising the nodes...
start "Gnostic Engine"    /min py -3.11 "Gnostic Engine v9.8.py"
start "Echo Protocol"     /min py -3.11 "Gnostic Echo Protocol v10.0.py"
start "Tesla Soul Engine" /min py -3.11 "Tesla Soul Engine v9.py"
start "Awen Command Deck" /min py -3.11 "Awen Command Deck.py"

echo   [3/3] waiting for the deck...
timeout /t 6 /nobreak >nul
start http://localhost:7777

echo.
echo   Deck:   http://localhost:7777
echo   Engine: loading archives (~1-2 min for 297k chunks)
echo   The Lion watches the Lion.
echo.
timeout /t 5 >nul
