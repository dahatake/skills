# launch-gui.ps1 - Launch the standalone markdown-query GUI on Windows (PowerShell).
#
# Usage:
#   pwsh -File launch-gui.ps1                    # operate on CWD
#   pwsh -File launch-gui.ps1 C:\path\to\repo    # operate on specific repo
[CmdletBinding()]
param(
    [Parameter(Position=0)] [string]$RepoRoot
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy    = Join-Path $ScriptDir ".venv-mdq-gui\Scripts\python.exe"
$Launcher  = Join-Path $ScriptDir "launch.py"

if (-not (Test-Path $VenvPy)) {
    Write-Error "venv not found. Run setup.ps1 first."
    exit 2
}

if ($RepoRoot) {
    & $VenvPy $Launcher $RepoRoot
} else {
    & $VenvPy $Launcher
}
