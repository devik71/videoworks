<#
.SYNOPSIS
    Shallow-клонує зовнішні референсні репозиторії у vendor/.

.EXAMPLE
    .\scripts\fetch-refs.ps1 -Tier core
    .\scripts\fetch-refs.ps1 -Name video-use,kinocut
    .\scripts\fetch-refs.ps1 -Group transcription -Update
    .\scripts\fetch-refs.ps1 -List
#>
[CmdletBinding()]
param(
    [string[]]$Tier,
    [string[]]$Group,
    [string[]]$Name,
    [switch]$Update,
    [switch]$List
)

$ErrorActionPreference = 'Stop'

$root      = Split-Path -Parent $PSScriptRoot
$tsvPath   = Join-Path $root 'refs\repos.tsv'
$vendorDir = Join-Path $root 'vendor'

if (-not (Test-Path $tsvPath)) { throw "Не знайдено $tsvPath" }

$repos = Import-Csv -Path $tsvPath -Delimiter "`t"

if ($Tier)  { $repos = $repos | Where-Object { $Tier  -contains $_.tier  } }
if ($Group) { $repos = $repos | Where-Object { $Group -contains $_.group } }
if ($Name)  { $repos = $repos | Where-Object { $Name  -contains $_.name  } }

if ($List) {
    $repos | Format-Table tier, group, name, url -AutoSize
    return
}

if (-not $repos) {
    Write-Warning 'Під ці фільтри нічого не підійшло. Запусти з -List, щоб побачити каталог.'
    return
}

if (-not (Test-Path $vendorDir)) {
    New-Item -ItemType Directory -Path $vendorDir | Out-Null
}

foreach ($repo in $repos) {
    $target = Join-Path $vendorDir $repo.name

    if (Test-Path $target) {
        if ($Update) {
            Write-Host "[update] $($repo.name)" -ForegroundColor Cyan
            git -C $target fetch --depth 1 origin
            git -C $target reset --hard origin/HEAD
        }
        else {
            Write-Host "[skip]   $($repo.name) (вже є; -Update щоб оновити)" -ForegroundColor DarkGray
        }
        continue
    }

    Write-Host "[clone]  $($repo.name) <- $($repo.url)" -ForegroundColor Green
    git clone --depth 1 --filter=blob:none $repo.url $target
    if ($LASTEXITCODE -ne 0) { Write-Warning "Не вдалося клонувати $($repo.name)" }
}

Write-Host ''
Write-Host "Готово. Референси лежать у $vendorDir (у git не комітяться)." -ForegroundColor Yellow
