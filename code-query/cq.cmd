@echo off
REM cq.cmd - Convenience wrapper so `cq ...` works from cmd.exe.
REM Delegates to cq.ps1, which resolves the interpreter and vendor path.
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0cq.ps1" %*
exit /b %ERRORLEVEL%
