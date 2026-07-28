@echo off
setlocal
cd /d "%~dp0"

where pwsh.exe >nul 2>&1
if %errorlevel% equ 0 (
    set "POWERSHELL_EXE=pwsh.exe"
) else (
    set "POWERSHELL_EXE=powershell.exe"
)

"%POWERSHELL_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0pw_run.ps1" -NoPause
set "RUN_EXIT_CODE=%errorlevel%"

echo.
pause
exit /b %RUN_EXIT_CODE%
