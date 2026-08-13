# setup.ps1 - Windows launcher for the shared kit setup (FR-KIT-03).
#
# Every decision (dependency resolution, path decisions, configuration
# scaffolding, Skill placement) lives in kit/kit_setup.py. This file only
# resolves a bootstrap interpreter and forwards the arguments, so the same
# behaviour is guaranteed on Windows, Linux and macOS.
#
# Usage:
#   pwsh -NoLogo -NoProfile -File setup.ps1 --with-gui --install-skill
#   pwsh -NoLogo -NoProfile -File setup.ps1 -Python C:\Python313\python.exe --build-index
#
# Run `pwsh -File setup.ps1 --help` for the full option list.

[CmdletBinding()]
param(
    [string]$Python = "python",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Entry = Join-Path $ScriptDir "kit\kit_setup.py"

if (-not (Test-Path -LiteralPath $Entry)) {
    [Console]::Error.WriteLine("shared setup implementation not found: $Entry")
    exit 2
}

$Arguments = @("--kit-dir", $ScriptDir, "--python", $Python)
if ($Rest) { $Arguments += $Rest }

& $Python $Entry @Arguments
exit $LASTEXITCODE
