# Publish this repository to GitHub.
#
# Why a script: on some networks github.com is unreachable while api.github.com
# and ssh.github.com:443 still work, so this repository is pushed over SSH on
# port 443 using a dedicated key. Re-running the script is safe: it never force
# pushes and refuses to touch a remote that already has diverging history.
#
# Prerequisites (one-time, done by the repository owner):
#   1. Add the public key printed by `Get-Content ~/.ssh/id_ed25519_codexpotter_relay.pub`
#      to https://github.com/settings/ssh/new
#   2. Create an empty PRIVATE repository on GitHub (no README, no .gitignore)
#   3. `~/.ssh/config` already maps `Host github.com` to `ssh.github.com:443`
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\publish.ps1 -Owner <github-user>
#   powershell -ExecutionPolicy Bypass -File .\scripts\publish.ps1 -Owner <github-user> -DryRun

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Owner,
    [string]$Repo = 'codexpotter-relay',
    [string]$Branch = 'main',
    [string]$RemoteName = 'origin',
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) {
    $fallback = 'C:\Program Files\Git\cmd\git.exe'
    if (Test-Path $fallback) { $git = $fallback } else { throw 'git not found' }
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

Push-Location $repoRoot
try {
    Write-Step 'Checking the local repository'
    $dirty = & $git status --porcelain
    if ($dirty) {
        throw 'working tree is not clean; commit or stash your changes first'
    }
    $head = (& $git rev-parse HEAD).Trim()
    $currentBranch = (& $git rev-parse --abbrev-ref HEAD).Trim()
    Write-Host "    branch: $currentBranch  head: $($head.Substring(0, 7))"
    if ($currentBranch -ne $Branch) {
        throw "expected branch '$Branch' but HEAD is on '$currentBranch'"
    }

    Write-Step 'Checking SSH access to GitHub (port 443)'
    $sshText = (& ssh -o BatchMode=yes -o ConnectTimeout=15 -T git@github.com 2>&1 | Out-String)
    if ($sshText -match 'successfully authenticated') {
        Write-Host "    $($sshText.Trim())"
    }
    elseif ($sshText -match 'Permission denied') {
        Write-Host 'SSH reached GitHub but this key is not authorized yet.' -ForegroundColor Yellow
        Write-Host 'Add the public key below to https://github.com/settings/ssh/new, then re-run:'
        Write-Host ''
        Get-Content "$env:USERPROFILE\.ssh\id_ed25519_codexpotter_relay.pub"
        exit 1
    }
    else {
        throw "cannot reach git@github.com over SSH: $($sshText.Trim())"
    }

    $url = "git@github.com:$Owner/$Repo.git"
    Write-Step "Checking the remote repository $Owner/$Repo"
    $remoteHead = (& $git ls-remote --heads $url $Branch 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "cannot read $url (does the private repository exist?): $remoteHead"
    }
    if ($remoteHead.Trim()) {
        $remoteSha = ($remoteHead.Trim() -split '\s+')[0]
        Write-Host "    remote $Branch is at $($remoteSha.Substring(0, 7))"
        $isAncestor = & $git merge-base --is-ancestor $remoteSha $head 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "remote $Branch has commits that are not in the local history; refusing to push (no force push)"
        }
        Write-Host '    remote is an ancestor of the local branch: fast-forward is safe'
    }
    else {
        Write-Host '    remote branch does not exist yet'
    }

    if ($DryRun) {
        Write-Step 'Dry run: nothing was pushed'
        Write-Host "    would push $head to $url ($Branch)"
        return
    }

    Write-Step 'Configuring the remote'
    $existing = (& $git remote get-url $RemoteName 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) {
        if ($existing -ne $url) {
            throw "remote '$RemoteName' already points at $existing; refusing to repoint it"
        }
        Write-Host "    $RemoteName -> $url"
    }
    else {
        & $git remote add $RemoteName $url
        Write-Host "    added $RemoteName -> $url"
    }

    Write-Step 'Pushing'
    & $git push --set-upstream $RemoteName $Branch
    if ($LASTEXITCODE -ne 0) { throw 'git push failed' }

    Write-Step 'Verifying the remote'
    $verify = (& $git ls-remote --heads $url $Branch 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "cannot verify $url after push: $verify" }
    $verifySha = ($verify.Trim() -split '\s+')[0]
    if ($verifySha -ne $head) {
        throw "remote head $verifySha does not match local head $head"
    }
    Write-Host "    remote $Branch == local HEAD ($($head.Substring(0, 7)))"
    Write-Host ""
    Write-Host "Published: https://github.com/$Owner/$Repo (visibility unchanged: private)" -ForegroundColor Green
}
finally {
    Pop-Location
}
