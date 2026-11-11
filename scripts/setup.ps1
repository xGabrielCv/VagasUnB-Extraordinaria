param([string]$NativePgBin, [switch]$SkipAdmin)
. "$PSScriptRoot/common.ps1"
if (-not $NativePgBin -and (Test-Path -LiteralPath '.private/native.json')) { $NativePgBin=(Native-Config).bin }
if (-not $NativePgBin) {
    Assert-Docker
    & docker volume inspect vagasunb-extraordinaria-demo_demo_postgres *> $null
    if ($LASTEXITCODE -eq 0 -and -not (Test-Path -LiteralPath '.private/secrets/keys.json')) { throw 'Volume existente sem chaves. Restaure os segredos originais; não serão geradas chaves substitutas.' }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Instale uv: https://docs.astral.sh/uv/getting-started/installation/ e execute novamente.' }
uv sync --frozen --python 3.12
Check-Exit 'dependências isoladas'
& $Python -m vagasunb.cli init-files
Check-Exit 'segredos'
# Restrict the host secret files; Compose secrets do not encrypt the source files.
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe '.private' /inheritance:r /grant:r "${account}:(OI)(CI)F" /grant 'SYSTEM:(OI)(CI)F' /Q | Out-Null
Check-Exit 'permissões privadas'
if ($NativePgBin) {
    $bin=(Resolve-Path -LiteralPath $NativePgBin).Path
    if (-not (Test-Path -LiteralPath (Join-Path $bin 'initdb.exe'))) { throw 'Informe a pasta bin do PostgreSQL 17.' }
    $data=Join-Path $ProjectRoot '.private/pgdata'
    if (-not (Test-Path -LiteralPath '.private/native.json')) {
        @{bin=$bin;data=$data;port=55437} | ConvertTo-Json | Set-Content -Encoding UTF8 '.private/native.json'
    }
    $cfg=Native-Config
    if (-not (Test-Path -LiteralPath (Join-Path $cfg.data 'PG_VERSION'))) {
        Native-Pg 'initdb.exe' @('-D',$cfg.data,'-U','vagas','-A','scram-sha-256','--pwfile','.private/secrets/postgres_password','-E','UTF8','--locale=C')
    }
    & (Join-Path $cfg.bin 'pg_ctl.exe') -D $cfg.data status *> $null
    if ($LASTEXITCODE -ne 0) { Native-Pg 'pg_ctl.exe' @('-D',$cfg.data,'-l',(Join-Path $ProjectRoot '.private/postgres.log'),'-o',"-p $($cfg.port) -h 127.0.0.1",'-w','start') }
    $env:PGPASSWORD = (Get-Content -Raw '.private/secrets/postgres_password').Trim()
    try {
        $exists=& (Join-Path $cfg.bin 'psql.exe') -h 127.0.0.1 -p $cfg.port -U vagas -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='vagas_demo'"
        Check-Exit 'consulta de banco'
        if ($exists -ne '1') { Native-Pg 'createdb.exe' @('-h','127.0.0.1','-p',"$($cfg.port)",'-U','vagas','vagas_demo') }
        $encoded=[uri]::EscapeDataString($env:PGPASSWORD)
        if (-not (Test-Path -LiteralPath '.private/config.json')) { @{mode='demo';database_url="postgresql+psycopg://vagas:$encoded@127.0.0.1:$($cfg.port)/vagas_demo"} | ConvertTo-Json | Set-Content -Encoding UTF8 '.private/config.json' }
    } finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    & (Join-Path $ProjectRoot '.venv/Scripts/alembic.exe') upgrade head
    Check-Exit 'migrações'
    & $Python -m vagasunb.cli seed
    Check-Exit 'catálogo demo'
    if (-not $SkipAdmin) { & $Python -m vagasunb.cli admin; Check-Exit 'senha administrativa' }
} else {
    Compose @('build')
    Compose @('up','-d','postgres')
    Compose @('run','--rm','migrate')
    if (-not $SkipAdmin) { Compose @('run','--rm','admin','python','-m','vagasunb.cli','admin') }
}
Write-Host 'Preparado. Execute .\scripts\start.ps1. Telegram continua desabilitado.'
