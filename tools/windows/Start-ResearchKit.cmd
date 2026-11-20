@echo off
setlocal
title ResearchKit
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ResearchKit.ps1" -FirstUse %*
if errorlevel 1 pause
endlocal
