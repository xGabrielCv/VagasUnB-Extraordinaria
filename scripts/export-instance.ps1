param([string]$Destination='.private/exports')
. "$PSScriptRoot/common.ps1"
if (Test-Path -LiteralPath '.private/native.json') {
    $cfg=Native-Config
    Stop-Native
    Native-Pg 'pg_ctl.exe' @('-D',$cfg.data,'-l',(Join-Path $ProjectRoot '.private/postgres.log'),'-o',"-p $($cfg.port) -h 127.0.0.1",'-w','start')
    $env:VAGAS_PG_BIN=$cfg.bin
    & $Python -m vagasunb.cli export --directory $Destination
    Check-Exit 'exportação cifrada'
} else {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $target=(Resolve-Path -LiteralPath $Destination).Path
    $key=(Resolve-Path -LiteralPath '.private/secrets/backup_key').Path
    Compose @('stop','admin','worker','bot')
    Compose @('run','--rm','-v',"${target}:/exports",'-v',"${key}:/run/backup_key:ro",'admin','python','-m','vagasunb.cli','export','--directory','/exports','--key','/run/backup_key')
}
Write-Host 'Exportado. Chave backup_key deve ser transportada separadamente. Escritores permanecem parados; execute start.ps1 quando desejar.'
