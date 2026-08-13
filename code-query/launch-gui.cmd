@echo off
REM Launch the standalone Code Query GUI on Windows.
setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv-cq\Scripts\python.exe"
if defined CQ_PYTHON (
  set "PYTHON=%CQ_PYTHON%"
) else if exist "%VENV_PY%" (
  set "PYTHON=%VENV_PY%"
) else (
  set "PYTHON=python"
)
"%PYTHON%" "%SCRIPT_DIR%launch.py" %*
exit /b %ERRORLEVEL%
