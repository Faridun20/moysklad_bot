<#
.SYNOPSIS
  Ставит portable Node.js в .tools/node (без admin, без системного PATH).

.DESCRIPTION
  На машине нет ни Node, ни пакетных менеджеров (winget/choco/scoop). Этот
  скрипт скачивает официальный Windows-zip с nodejs.org и распаковывает его в
  .tools/node (папка в .gitignore — в репозиторий не попадает). После этого
  фронт-тесты гоняются через scripts/test-js.ps1.

  Идемпотентно: если .tools/node/node.exe уже есть — ничего не делает
  (перекачать можно флагом -Force).

.PARAMETER Version
  Версия Node вида v24.16.0. По умолчанию — текущая LTS с nodejs.org.

.PARAMETER Force
  Перекачать/переустановить, даже если Node уже стоит.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/setup-node.ps1
#>
[CmdletBinding()]
param(
    [string]$Version,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $repoRoot '.tools'
$nodeDir  = Join-Path $toolsDir 'node'
$nodeExe  = Join-Path $nodeDir 'node.exe'

if ((Test-Path $nodeExe) -and -not $Force) {
    $v = & $nodeExe --version
    Write-Host "Node уже установлен в .tools/node ($v). Перекачать: -Force." -ForegroundColor Green
    exit 0
}

# Архитектура (поддерживаем x64 и arm64 — оба есть у nodejs.org).
$arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'x64' }
    'ARM64' { 'arm64' }
    'x86'   { 'x86' }
    default { 'x64' }
}

$ProgressPreference = 'SilentlyContinue'

if (-not $Version) {
    Write-Host "Узнаю текущую LTS с nodejs.org ..."
    $idx = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json' -TimeoutSec 30
    $Version = ($idx | Where-Object { $_.lts } | Select-Object -First 1).version
}

$pkg = "node-$Version-win-$arch"
$url = "https://nodejs.org/dist/$Version/$pkg.zip"
$zip = Join-Path $env:TEMP "$pkg.zip"

Write-Host "Скачиваю $url ..."
Invoke-WebRequest -Uri $url -OutFile $zip -TimeoutSec 300
Write-Host ("Скачано {0:N1} MB, распаковываю ..." -f ((Get-Item $zip).Length / 1MB))

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
Expand-Archive -Path $zip -DestinationPath $toolsDir -Force
if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
Rename-Item -Path (Join-Path $toolsDir $pkg) -NewName 'node'
Remove-Item $zip -Force

$v = & $nodeExe --version
Write-Host "Готово: Node $v в .tools/node. Запуск тестов — scripts/test-js.ps1" -ForegroundColor Green
