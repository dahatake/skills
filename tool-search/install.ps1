# install.ps1 - Windows entry point for the distribution kits.
#
# Kept ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI, so a
# UTF-8 (no BOM) file carrying non-ASCII text fails to parse. A bare Windows
# ships 5.1 only, which is exactly the situation this script must survive.
# Japanese guidance lives in install.py (UTF-8 safe) and GETTING-STARTED.md.
#
# Responsibilities, and nothing else:
#   1. Install Python 3.11+ and git through winget / choco when missing
#   2. Delegate every other decision to install.py -> kit/kit_setup.py
#
# Usage (run from the root of the target repository):
#   pwsh -NoLogo -NoProfile -File <kit>\install.ps1
#   powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File <kit>\install.ps1
#   pwsh -NoLogo -NoProfile -File <kit>\install.ps1 -WithGui -WithWatch
#   pwsh -NoLogo -NoProfile -File <kit>\install.ps1 -RepoRoot D:\other-repo -Force
#   pwsh -NoLogo -NoProfile -File <kit>\install.ps1 -Version
#   pwsh -NoLogo -NoProfile -File <kit>\install.ps1 -Verify

[CmdletBinding()]
param(
    [string]$RepoRoot = (Get-Location).Path,
    [string]$Python = "",
    [switch]$WithGui,
    [switch]$WithWatch,
    [switch]$WithTokenizer,
    [switch]$NoIndex,
    [switch]$NoSkill,
    [switch]$NoExtras,
    [switch]$NoVenv,
    [switch]$Force,
    [switch]$SkipPrereq,
    [switch]$Version,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MinMajor = 3
$MinMinor = 11

# Reporting the installed version or checking the shipped files never installs
# anything, so the toolchain bootstrap is pointless there.
if ($Version -or $Verify) { $SkipPrereq = $true }

function Write-Step([string]$Message) { Write-Host "[install] $Message" }

function Update-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}

function Test-PythonVersion([string]$Exe) {
    try {
        $raw = & $Exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $false }
    $parts = $raw.Trim().Split(".")
    if ($parts.Count -lt 2) { return $false }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    return ($major -gt $MinMajor) -or ($major -eq $MinMajor -and $minor -ge $MinMinor)
}

function Resolve-Python {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    $candidates += @("python", "python3")
    foreach ($candidate in $candidates) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found -and (Test-PythonVersion $found.Source)) { return $found.Source }
    }
    $launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($tag in @("-3.13", "-3.12", "-3.11", "-3")) {
            try { $exe = (& $launcher.Source $tag -c "import sys;print(sys.executable)" 2>$null) } catch { $exe = $null }
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-PythonVersion $exe.Trim())) { return $exe.Trim() }
        }
    }
    return $null
}

function Install-WithPackageManager([string]$WingetId, [string]$ChocoId, [string]$Label) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Step "installing $Label with winget ($WingetId)"
        # --no-upgrade: never touch a toolchain the user already has.
        & winget install --id $WingetId --exact --silent --no-upgrade `
            --accept-package-agreements --accept-source-agreements
        Update-PathFromRegistry
        return $true
    }
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        Write-Step "installing $Label with Chocolatey ($ChocoId)"
        & choco install $ChocoId -y
        Update-PathFromRegistry
        return $true
    }
    return $false
}

if (-not $SkipPrereq) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if (-not (Install-WithPackageManager "Git.Git" "git" "git")) {
            [Console]::Error.WriteLine(
                "git is missing and neither winget nor choco is available. Install it from https://git-scm.com/download/win")
            exit 3
        }
    }

    $resolved = Resolve-Python
    if (-not $resolved) {
        if (-not (Install-WithPackageManager "Python.Python.3.12" "python" "Python 3.12")) {
            [Console]::Error.WriteLine(
                "Python $MinMajor.$MinMinor+ is missing and neither winget nor choco is available. Install it from https://www.python.org/downloads/windows/")
            exit 3
        }
        $resolved = Resolve-Python
    }
    if (-not $resolved) {
        [Console]::Error.WriteLine(
            "Could not resolve Python $MinMajor.$MinMinor+. Open a new shell and run this script again.")
        exit 3
    }
} else {
    $resolved = Resolve-Python
    if (-not $resolved) {
        [Console]::Error.WriteLine("-SkipPrereq requires Python $MinMajor.$MinMinor+ to be installed already.")
        exit 3
    }
}

Write-Step "python: $resolved"
Write-Step "git   : $((Get-Command git -ErrorAction SilentlyContinue).Source)"

$Arguments = @(
    (Join-Path $ScriptDir "install.py"),
    "--kit-dir", $ScriptDir,
    "--repo-root", $RepoRoot,
    "--python", $resolved
)
if ($WithGui) { $Arguments += "--with-gui" }
if ($WithWatch) { $Arguments += "--with-watch" }
if ($WithTokenizer) { $Arguments += "--with-tokenizer" }
if ($NoIndex) { $Arguments += "--no-index" }
if ($NoSkill) { $Arguments += "--no-skill" }
if ($NoExtras) { $Arguments += "--no-extras" }
if ($NoVenv) { $Arguments += "--no-venv" }
if ($Force) { $Arguments += "--force" }
if ($Version) { $Arguments += "--version" }
if ($Verify) { $Arguments += "--verify" }

# install.py and kit_setup.py print Japanese. The Windows console defaults to the
# ANSI code page, which turns that output into mojibake, so force UTF-8 on both
# sides for the duration of the call only: SetConsoleOutputCP mutates the console
# itself, so it would otherwise survive this process.
$PrevConsoleEncoding = [Console]::OutputEncoding
$PrevIoEncoding = $env:PYTHONIOENCODING
$Code = 1
try {
    $PSNativeCommandUseErrorActionPreference = $false
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = "utf-8"
    & $resolved @Arguments
    $Code = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $PrevConsoleEncoding
    $env:PYTHONIOENCODING = $PrevIoEncoding
}
exit $Code
