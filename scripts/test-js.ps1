<#
.SYNOPSIS
  Локальный прогон фронт-тестов WebApp (Vitest) через Node.

.DESCRIPTION
  Использует Node из .tools/node, если он там есть (см. scripts/setup-node.ps1),
  иначе — системный node из PATH. Ставит npm-зависимости при первом запуске
  (если нет node_modules) и запускает `npm test` (vitest run). Любые лишние
  аргументы пробрасываются в vitest.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/test-js.ps1
  powershell -ExecutionPolicy Bypass -File scripts/test-js.ps1 --watch
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$localNode = Join-Path $repoRoot '.tools\node'

# Приоритет — локальный portable Node; иначе системный.
if (Test-Path (Join-Path $localNode 'node.exe')) {
    $env:Path = "$localNode;$env:Path"
} elseif (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "Node не найден. Сначала: scripts/setup-node.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host ("Node {0}, npm {1}" -f (node --version), (npm --version)) -ForegroundColor Cyan

Push-Location $repoRoot
try {
    if (-not (Test-Path (Join-Path $repoRoot 'node_modules'))) {
        Write-Host "Ставлю npm-зависимости (первый запуск) ..." -ForegroundColor Cyan
        npm install
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    if ($Args) { npm test -- @Args } else { npm test }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
