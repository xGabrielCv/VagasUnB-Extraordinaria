param([switch]$Open)
. "$PSScriptRoot/common.ps1"

$configPath = Join-Path $PrivateRoot 'config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'Instancia local ainda nao configurada. Execute .\scripts\setup.ps1 primeiro.'
}
$cfg = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$uri = [uri]($cfg.database_url -replace '^postgresql\+psycopg://', 'postgresql://')
$pgHost = if ($uri.Host) { $uri.Host } else { '127.0.0.1' }
$pgPort = if ($uri.Port -gt 0) { "$($uri.Port)" } else { '5432' }
$pgDb = $uri.AbsolutePath.TrimStart('/')
$pgUser = if ($uri.UserInfo) { ($uri.UserInfo -split ':', 2)[0] } else { 'vagas' }

$dbeaverDir = Join-Path $env:APPDATA 'DBeaverData\workspace6\General\.dbeaver'
New-Item -ItemType Directory -Force $dbeaverDir | Out-Null
$dsPath = Join-Path $dbeaverDir 'data-sources.json'

$data = [ordered]@{ folders = [ordered]@{}; connections = [ordered]@{} }
if (Test-Path -LiteralPath $dsPath) {
    try {
        $existing = Get-Content -Raw -LiteralPath $dsPath | ConvertFrom-Json -AsHashtable
        if ($existing.folders) { $data.folders = $existing.folders }
        if ($existing.connections) { $data.connections = $existing.connections }
    } catch { }
}

$data.connections['vagasunb-local'] = [ordered]@{
    provider = 'postgresql'
    driver = 'postgres-jdbc'
    name = 'VagasUnB Local (PostgreSQL)'
    'save-password' = $false
    configuration = [ordered]@{
        host = $pgHost
        port = $pgPort
        database = $pgDb
        url = "jdbc:postgresql://${pgHost}:${pgPort}/${pgDb}"
        configurationType = 'MANUAL'
        type = 'dev'
        authModel = 'native'
        user = $pgUser
    }
}

$data | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -LiteralPath $dsPath
Write-Host "Conexao 'VagasUnB Local (PostgreSQL)' configurada no DBeaver (${pgHost}:${pgPort}/${pgDb}, usuario ${pgUser})."
Write-Host "Senha privada disponivel em: $(Join-Path $PrivateRoot 'secrets/postgres_password')"

if ($Open) {
    $exe = @(
        (Join-Path $env:LOCALAPPDATA 'DBeaver\dbeaver.exe'),
        'C:\Program Files\DBeaver\dbeaver.exe'
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($exe) { Start-Process $exe } else { Write-Warning 'DBeaver Community nao encontrado no PATH padrao.' }
}
