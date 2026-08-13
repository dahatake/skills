# mdq.ps1 - Run the vendored markdown-query CLI from any repository (Windows).
#
# Resolves the interpreter, puts vendor/ on the import path, and forwards every
# argument to `python -m mdq`.
#
# Usage:
#   .\mdq.ps1 index
#   .\mdq.ps1 search --q "requirement definition"
#
# Environment:
#   MDQ_PYTHON  Interpreter to use (default: .venv-mdq-gui if present, else python).

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VendorDir = Join-Path $ScriptDir "vendor"
$VenvPy    = Join-Path $ScriptDir ".venv-mdq-gui\Scripts\python.exe"

if (-not (Test-Path (Join-Path $VendorDir "mdq\cli.py"))) {
    # Write-Error would be turned into a terminating error by $ErrorActionPreference,
    # so the documented exit code 2 would never be reached.
    [Console]::Error.WriteLine("vendor/mdq is missing. The kit was copied without its engine; re-copy the whole directory.")
    exit 2
}

$Python = if ($env:MDQ_PYTHON) { $env:MDQ_PYTHON }
          elseif (Test-Path $VenvPy) { $VenvPy }
          else { "python" }

$Arguments = @()
if ($Rest) { $Arguments = @($Rest) }

# This script runs in-process, so PYTHONPATH and the console encoding must be
# restored: otherwise every call appends another vendor entry to the caller's
# session. UTF-8 is forced because mdq prints Japanese and the Windows console
# defaults to the ANSI code page.
$PrevPythonPath = $env:PYTHONPATH
$PrevIoEncoding = $env:PYTHONIOENCODING
$PrevConsole    = [Console]::OutputEncoding
$Code = 1
try {
    # PowerShell 7 can be configured to throw on a non-zero native exit code,
    # which would replace the engine's exit code with a terminating error.
    $PSNativeCommandUseErrorActionPreference = $false
    $Separator = [IO.Path]::PathSeparator
    $env:PYTHONPATH = if ($PrevPythonPath) { "$VendorDir$Separator$PrevPythonPath" } else { $VendorDir }
    $env:PYTHONIOENCODING = "utf-8"
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    & $Python -m mdq @Arguments
    $Code = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $PrevPythonPath
    $env:PYTHONIOENCODING = $PrevIoEncoding
    [Console]::OutputEncoding = $PrevConsole
}
exit $Code
