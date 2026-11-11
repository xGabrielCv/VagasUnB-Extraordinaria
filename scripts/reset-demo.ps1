param([switch]$ConfirmDemo)
. "$PSScriptRoot/common.ps1"
if ($env:VAGAS_MODE -and $env:VAGAS_MODE -ne 'demo') { throw 'Reset recusado: modo diferente de demo. Nenhum processo foi parado.' }
if (Test-Path -LiteralPath (Join-Path $PrivateRoot 'config.json')) {
    $instance=Get-Content -Raw -LiteralPath (Join-Path $PrivateRoot 'config.json') | ConvertFrom-Json
    if ($instance.mode -ne 'demo' -or $instance.database_url -notmatch '/[^/]*_demo(?:\?.*)?$') { throw 'Reset recusado: banco não identificado como demo. Nenhum processo foi parado.' }
}
if (-not $ConfirmDemo -and (Read-Host 'Digite RESET DEMO para recriar somente dados sintéticos') -ne 'RESET DEMO') { throw 'Reset cancelado.' }
if (Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json')) {
    # Stop writers while preserving PostgreSQL, keys, configuration and administrator.
    $cfg=Native-Config
    Stop-Native
    Native-Pg 'pg_ctl.exe' @('-D',$cfg.data,'-l',(Join-Path $PrivateRoot 'postgres.log'),'-o',"-p $($cfg.port) -h 127.0.0.1",'-w','start')
    & $Python -m vagasunb.cli reset-demo --confirm
    Check-Exit 'reset exclusivamente demo'
} else {
    Compose @('stop','admin','worker','bot')
    Compose @('run','--rm','admin','python','-m','vagasunb.cli','reset-demo','--confirm')
}
Write-Host 'Demo recriada. Execute start.ps1.'
