# Launch the standalone Code Query GUI on Windows (PowerShell 7+).
#
# Usage:
#   pwsh.exe -NoLogo -NoProfile -File launch-gui.ps1                  # operate on CWD
#   pwsh.exe -NoLogo -NoProfile -File launch-gui.ps1 D:\work\my-repo  # operate on a repository
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSEdition -ne "Core" -or $PSVersionTable.PSVersion.Major -lt 7) {
    Write-Error "PowerShell 7+ (pwsh.exe) is required."
    exit 2
}
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $ScriptDir ".venv-cq\Scripts\python.exe"
$Launcher = Join-Path $ScriptDir "launch.py"
$Python = if ($env:CQ_PYTHON) { $env:CQ_PYTHON }
          elseif (Test-Path $VenvPy) { $VenvPy }
          else { "python" }

$Arguments = @()
if ($Rest) { $Arguments = @($Rest) }
& $Python $Launcher @Arguments
exit $LASTEXITCODE
