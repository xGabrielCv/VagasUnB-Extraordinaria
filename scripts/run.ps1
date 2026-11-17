param(
    [string]$NativePgBin,
    [switch]$NoBrowser,
    [switch]$OpenDBeaver
)
. "$PSScriptRoot/common.ps1"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Instalando uv...'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install --id astral-sh.uv --exact --silent --accept-package-agreements --accept-source-agreements
    } else {
        Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression
    }
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:PATH"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'Nao foi possivel localizar uv apos instalacao. Reabra o terminal e execute novamente.'
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    uv sync --frozen --python 3.12
    Check-Exit 'sincronizacao de dependencias Python'
}

function Find-PgBin {
    if ($NativePgBin) { return $NativePgBin }
    if (Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json')) {
        return (Native-Config).bin
    }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'VagasUnB-private\postgres\pgsql\bin')
    ) + @(Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin' -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | ForEach-Object { $_.FullName })
    foreach ($dir in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $dir 'initdb.exe')) { return $dir }
    }
    return $null
}

$useDocker = $false
if (-not (Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json')) -and -not $NativePgBin) {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $useDocker = $true }
    }
}

$pgBin = if (-not $useDocker) { Find-PgBin } else { $null }
if (-not $useDocker -and -not $pgBin) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'PostgreSQL/Docker nao encontrados. Instale Docker Desktop ou PostgreSQL 17 e execute setup.ps1 -NativePgBin <pasta bin>.'
    }
    Write-Host 'Instalando PostgreSQL 17...'
    & winget install --id PostgreSQL.PostgreSQL.17 --exact --silent --accept-package-agreements --accept-source-agreements
    Check-Exit 'instalacao do PostgreSQL 17'
    $pgBin = Find-PgBin
    if (-not $pgBin) { throw 'PostgreSQL instalado, mas pasta bin nao localizada. Informe -NativePgBin.' }
}

$needsSetup = -not (Test-Path -LiteralPath (Join-Path $PrivateRoot 'secrets/keys.json')) -or (-not $useDocker -and -not (Test-Path -LiteralPath (Join-Path $PrivateRoot 'config.json')))
if ($needsSetup) {
    if ($useDocker) {
        & "$PSScriptRoot/setup.ps1"
    } else {
        & "$PSScriptRoot/setup.ps1" -NativePgBin $pgBin
    }
}

& "$PSScriptRoot/configure-dbeaver.ps1" -EnsureInstalled

if ($NoBrowser) {
    & "$PSScriptRoot/start.ps1" -NoBrowser
} else {
    & "$PSScriptRoot/start.ps1"
}

if ($OpenDBeaver) {
    & "$PSScriptRoot/configure-dbeaver.ps1" -Open
}
