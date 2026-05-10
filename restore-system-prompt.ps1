$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$target = Join-Path $repoRoot "browser_use\agent\system_prompts\system_prompt.md"
$backup = Join-Path $repoRoot "browser_use\agent\system_prompts\backups\system_prompt.original.md"

if (-not (Test-Path -LiteralPath $backup)) {
    throw "Original backup not found: $backup"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$snapshot = Join-Path $repoRoot "browser_use\agent\system_prompts\backups\system_prompt.before-restore-$stamp.md"
Copy-Item -LiteralPath $target -Destination $snapshot
Copy-Item -LiteralPath $backup -Destination $target -Force

Write-Host "Restored original system prompt."
Write-Host "Prompt before restore saved to: $snapshot"
