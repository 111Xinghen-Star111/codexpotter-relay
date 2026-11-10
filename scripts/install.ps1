# CodexPotter Relay - one-step installer for Windows.
#
# What it does:
#   1. verifies Python 3.11+, git and the Codex CLI are available;
#   2. installs the `relay` command for the current user;
#   3. creates the configuration file and the SQLite database;
#   4. writes the managed hooks block into the Codex configuration;
#   5. prints a health report.
#
# Nothing here needs administrator rights: everything is per-user.

[CmdletBinding()]
param(
    [string]$Python,
    [string]$Codex,
    [switch]$WithHooks,
    [switch]$SkipPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

Write-Host "CodexPotter Relay installer" -ForegroundColor Green
Write-Host "repository: $repoRoot"

# ---------------------------------------------------------------- Python
Write-Step 'Locating Python 3.11+'
$pythonPath = $Python
if (-not $pythonPath) {
    $candidates = @()
    $candidates += (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $candidates += (Get-Command python3 -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if (-not (Test-Path $candidate)) { continue }
        if ((Get-Item $candidate).Length -eq 0) { continue }   # Microsoft Store stub
        $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        if ([version]$version -ge [version]'3.11') { $pythonPath = $candidate; break }
    }
}
if (-not $pythonPath) {
    Fail 'Python 3.11+ not found. Install it first:  winget install --id Python.Python.3.11 -e'
}
Write-Host "    python: $pythonPath"

# ---------------------------------------------------------------- git
Write-Step 'Locating git'
$gitPath = (Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $gitPath) {
    $fallback = 'C:\Program Files\Git\cmd\git.exe'
    if (Test-Path $fallback) { $gitPath = $fallback }
}
if (-not $gitPath) {
    Fail 'git not found. Install it first:  winget install --id Git.Git -e'
}
Write-Host "    git: $gitPath"

# ---------------------------------------------------------------- Codex
Write-Step 'Locating the Codex CLI'
# --codex is a global option, so it must come before the subcommand.
$codexArgs = @()
if ($Codex) { $codexArgs += @('--codex', $Codex) }
Push-Location $repoRoot
try {
    $doctorOutput = & $pythonPath -m relay @codexArgs doctor 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($doctorOutput | Out-String)
        Fail 'Codex CLI is not usable. Install Codex and sign in, then re-run the installer.'
    }
    Write-Host ($doctorOutput | Out-String)
}
finally {
    Pop-Location
}

# ---------------------------------------------------------------- install package
Write-Step 'Installing the relay command (per-user)'
Push-Location $repoRoot
try {
    & $pythonPath -m pip install --quiet --no-warn-script-location -e $repoRoot
    if ($LASTEXITCODE -ne 0) {
        Fail 'pip install failed. Run it manually to see the error: python -m pip install -e .'
    }
}
finally {
    Pop-Location
}

# ---------------------------------------------------------------- PATH
Write-Step 'Configuring PATH'
$scriptsDir = & $pythonPath -c "import sysconfig; print(sysconfig.get_path('scripts'))"
if (-not $SkipPath) {
    $normalized = $scriptsDir.TrimEnd('\', '/')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($userPath) { $parts = $userPath -split ';' | Where-Object { $_ } }
    # Windows PATH comparisons ignore case and trailing separators, so dedupe
    # the same directory even when the Python installer already added it.
    $alreadyOnPath = $parts | Where-Object { $_.TrimEnd('\', '/') -ieq $normalized }
    if (-not $alreadyOnPath) {
        $newPath = (@($parts) + $scriptsDir) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Host "    added to user PATH: $scriptsDir"
    }
    else {
        Write-Host "    already on user PATH: $scriptsDir"
    }
    if (-not ($env:Path -split ';' | Where-Object { $_.TrimEnd('\', '/') -ieq $normalized })) {
        $env:Path = "$scriptsDir;$env:Path"
    }
}

# ---------------------------------------------------------------- initialise
Write-Step 'Creating configuration and database'
& $pythonPath -m relay init
if ($LASTEXITCODE -ne 0) { Fail 'relay init failed.' }

# ---------------------------------------------------------------- hooks
if ($WithHooks) {
    Write-Step 'Installing the relay hooks block (opt-in)'
    & $pythonPath -m relay hooks install
    Write-Host '    note: Codex 0.167 does not execute [hooks] config entries; state capture'
    Write-Host '          runs through app-server notifications either way. Check with: relay hooks status'
}
else {
    Write-Step 'Skipping the hooks block'
    Write-Host '    Hooks are opt-in (-WithHooks). Codex 0.167 does not execute [hooks] config'
    Write-Host '    entries, so state capture uses app-server notifications by default.'
}

Write-Step 'Done'
Write-Host 'Next steps:' -ForegroundColor Green
Write-Host '  relay doctor'
Write-Host '  relay test --mode mock'
Write-Host '  relay start --goal "your objective" --project C:\path\to\project'
Write-Host ''
Write-Host 'Open a new terminal so the PATH change takes effect.'
