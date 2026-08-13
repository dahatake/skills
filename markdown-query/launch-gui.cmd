@echo off
REM launch-gui.cmd - Launch the standalone markdown-query GUI on Windows.
REM
REM Usage:
REM   launch-gui.cmd                    Use current working directory as repo_root
REM   launch-gui.cmd C:\path\to\repo    Operate on a specific repository
setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv-mdq-gui\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [launch-gui] venv not found. Run setup.ps1 first: pwsh -File setup.ps1
    exit /b 2
)

"%VENV_PY%" "%SCRIPT_DIR%launch.py" %*
endlocal
