# sync-vendor.ps1 - Windows launcher for the shared kit sync (FR-KIT-03).
#
# Maintainer tool. The rules for what ships live in kit/kit_sync.py, which reads
# the engine and the canonical Skill definition out of the UPSTREAM repository,
# so point it there explicitly when running outside it:
#
#   pwsh -NoLogo -NoProfile -File sync-vendor.ps1 -Source C:\upstream\mdq --repo-root C:\upstream

[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Source = $null,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Entry = Join-Path $ScriptDir "kit\kit_sync.py"

if (-not (Test-Path -LiteralPath $Entry)) {
    [Console]::Error.WriteLine("shared sync implementation not found: $Entry")
    exit 2
}

$Arguments = @("--kit-dir", $ScriptDir)
if ($Source) { $Arguments += @("--source", $Source) }
if ($Rest) { $Arguments += $Rest }

& $Python $Entry @Arguments
exit $LASTEXITCODE
