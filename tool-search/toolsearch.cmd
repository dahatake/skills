@echo off
REM toolsearch.cmd - Convenience wrapper so `toolsearch ...` works from cmd.exe.
REM Delegates to toolsearch.ps1, which resolves the interpreter and vendor path.
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0toolsearch.ps1" %*
exit /b %ERRORLEVEL%
