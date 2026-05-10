$ErrorActionPreference = "Stop"

param(
    [Parameter(Mandatory = $true)]
    [string]$PromptFile
)

$repoRoot = $PSScriptRoot
$target = Join-Path $repoRoot "browser_use\agent\system_prompts\system_prompt.md"
$backupDir = Join-Path $repoRoot "browser_use\agent\system_prompts\backups"
$stableBackup = Join-Path $backupDir "system_prompt.original.md"

if (-not (Test-Path -LiteralPath $PromptFile)) {
    throw "Prompt file not found: $PromptFile"
}

New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

if (-not (Test-Path -LiteralPath $stableBackup)) {
    Copy-Item -LiteralPath $target -Destination $stableBackup
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$snapshot = Join-Path $backupDir "system_prompt.before-$stamp.md"
Copy-Item -LiteralPath $target -Destination $snapshot

Copy-Item -LiteralPath $PromptFile -Destination $target -Force

Write-Host "Applied system prompt from: $PromptFile"
Write-Host "Previous prompt snapshot: $snapshot"
Write-Host "Restore original with: .\restore-system-prompt.ps1"
