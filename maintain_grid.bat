@echo off
chcp 65001 >nul
title Awen Grid Maintenance
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo.
echo   AWEN GRID - MAINTENANCE
echo   flush ^> stop ^> restore vectors ^> refresh atlas ^> relaunch
echo.

rem --- [1/4] graceful stop: stop_grid.ps1 now flushes the engine's dirty
rem     indices BEFORE killing, so nothing is pending when we rebuild.
echo   [1/4] flushing and stopping the grid...
powershell -NoProfile -ExecutionPolicy Bypass -File "stop_grid.ps1"
timeout /t 3 /nobreak >nul

rem --- [2/4] resumable rebuild: encodes ONLY ledger entries whose vectors
rem     never reached disk (usually 0-2 after a clean stop; ~30s when needed).
rem     Never touches the conversations lane - the script refuses it by design.
echo.
echo   [2/4] restoring any unflushed vectors...
py -3.11 rebuild_gnosis.py

rem --- [3/4] atlas refresh: folds every new dream and chat turn into the
rem     Neural Map's regions so the retrieval flash covers the whole archive.
rem     A few minutes on the 4070; the grid is down, so nothing competes.
echo.
echo   [3/4] refreshing the atlas (this is the few-minutes step)...
py -3.11 build_atlas.py --sample 80000

rem --- [4/4] relaunch. Pass-through arg works: "maintain_grid.bat lan"
rem     relaunches in LAN mode for the tablet.
echo.
echo   [4/4] relaunching the grid...
call "Start Awen Grid.bat" %1

echo.
echo   Maintenance complete. The Lion watches the Lion.
