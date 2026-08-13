@echo off
REM mdq.cmd - Convenience wrapper so `mdq ...` works from cmd.exe.
REM Delegates to mdq.ps1, which resolves the interpreter and vendor path.
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0mdq.ps1" %*
exit /b %ERRORLEVEL%
