@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Win.ps1" %*
set "LauncherExitCode=%ERRORLEVEL%"
if not "%LauncherExitCode%"=="0" (
    echo.
    echo NyXion Heredity could not start. Review the error above.
    pause
)
exit /b %LauncherExitCode%
