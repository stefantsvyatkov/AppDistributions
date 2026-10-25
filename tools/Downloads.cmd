@echo off
rem How often the releases were downloaded (Get-Downloads.ps1); the window waits for a key so it can be read.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Get-Downloads.ps1"
echo.
pause
