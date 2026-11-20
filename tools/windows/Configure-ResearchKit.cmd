@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ResearchKit.ps1" -Configure
pause
endlocal
