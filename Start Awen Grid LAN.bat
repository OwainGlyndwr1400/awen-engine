@echo off
rem One double-click for network mode: deck reachable from the tablet, Steam
rem Deck, phone or laptop. Launches the grid with --lan and opens the QR page
rem so a camera scan gets any device straight in - no typing IP:port into a
rem browser bar that treats it as a search.
rem
rem If another device cannot reach the deck:
rem   1. Windows firewall prompt: allow python.exe on PRIVATE networks.
rem   2. Settings > Network > your wifi must be set to PRIVATE profile -
rem      the Public profile silently blocks inbound connections (this is the
rem      one that has bitten before).
rem
rem The memory engine itself stays on loopback either way - other devices see
rem only the deck on :7777, which proxies it.
call "%~dp0Start Awen Grid.bat" lan
