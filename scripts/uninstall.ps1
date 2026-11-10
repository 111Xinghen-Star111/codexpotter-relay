# CodexPotter Relay - uninstaller for Windows.
#
# Removes the `relay` command, the managed hooks block and the PATH entry.
# Work data (database, logs, reports) is kept unless -Purge is given, because it
# is the only record of past work chains.

[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

$pythonPath = $Python
if (-not $pythonPath) {
    $candidates = @()
    $candidates += (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate) -and (Get-Item $candidate).Length -gt 0) {
            $pythonPath = $candidate
            break
        }
    }
}
if (-not $pythonPath) {
    Write-Host 'Python not found; skipping package and hook removal.' -ForegroundColor Yellow
}

if ($pythonPath) {
    Write-Step 'Uninstalling the Codex plugin'
    Push-Location $repoRoot
    try {
        & $pythonPath -m relay plugin uninstall
    }
    catch {
        Write-Host "    plugin was not installed or could not be removed: $_" -ForegroundColor Yellow
    }
    finally {
        Pop-Location
    }

    Write-Step 'Removing the managed hooks block'
    Push-Location $repoRoot
    try {
        & $pythonPath -m relay hooks uninstall
    }
    catch {
        Write-Host "    hooks were not installed or could not be removed: $_" -ForegroundColor Yellow
    }
    finally {
        Pop-Location
    }

    Write-Step 'Uninstalling the relay package'
    & $pythonPath -m pip uninstall --quiet --yes codexpotter-relay
    if ($LASTEXITCODE -ne 0) {
        Write-Host '    package was not installed via pip (or removal failed)' -ForegroundColor Yellow
    }

    $scriptsDir = & $pythonPath -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    Write-Step 'Cleaning PATH'
    $normalized = $scriptsDir.TrimEnd('\', '/')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath) {
        # Only drop an entry that this installer added; keep any copy that the
        # Python installer itself created for other console scripts.
        $parts = $userPath -split ';' | Where-Object { $_ }
        $kept = @()
        $removed = 0
        foreach ($part in $parts) {
            if ($part.TrimEnd('\', '/') -ieq $normalized -and $part -ne $scriptsDir) {
                $removed++
                continue
            }
            $kept += $part
        }
        if ($removed -gt 0) {
            [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
            Write-Host "    removed installer-added PATH entry: $scriptsDir"
        }
        else {
            Write-Host "    no installer-added PATH entry found (a Python-owned entry is left as is)"
        }
    }
}

$relayHome = Join-Path $env:LOCALAPPDATA 'CodexPotterRelay'
if ($Purge) {
    Write-Step 'Deleting work data'
    if (Test-Path $relayHome) {
        Remove-Item -Recurse -Force $relayHome
        Write-Host "    deleted: $relayHome"
    }
}
else {
    Write-Host ""
    Write-Host "Work data kept at: $relayHome" -ForegroundColor Yellow
    Write-Host 'Delete it manually or re-run with -Purge to remove chains, logs and reports.'
}

Write-Host ""
Write-Host 'Uninstall finished.' -ForegroundColor Green
