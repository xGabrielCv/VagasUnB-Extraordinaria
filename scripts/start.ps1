param([switch]$NoBrowser)
. "$PSScriptRoot/common.ps1"
if (Test-Path -LiteralPath '.private/native.json') {
    $cfg=Native-Config
    & (Join-Path $cfg.bin 'pg_ctl.exe') -D $cfg.data status *> $null
    if ($LASTEXITCODE -ne 0) { Native-Pg 'pg_ctl.exe' @('-D',$cfg.data,'-l',(Join-Path $ProjectRoot '.private/postgres.log'),'-o',"-p $($cfg.port) -h 127.0.0.1",'-w','start') }
    try { $tcp=[Net.Sockets.TcpClient]::new('127.0.0.1',8765); $tcp.Dispose(); $occupied=$true } catch { $occupied=$false }
    if ($occupied) {
        if (Test-Path -LiteralPath '.private/processes.json') { Wait-Dashboard; Write-Host 'InstÃ¢ncia jÃ¡ ativa: http://127.0.0.1:8765'; return }
        throw 'Porta 8765 ocupada. Nenhum processo foi encerrado.'
    }
    & (Join-Path $ProjectRoot '.venv/Scripts/alembic.exe') upgrade head
    Check-Exit 'migraÃ§Ãµes'
    # Keep raw exception output private; product logging contains only fixed categories.
    if (Test-Path -LiteralPath '.private/shutdown') { Remove-Item -LiteralPath '.private/shutdown' }
    $admin=Start-Process -FilePath $Python -ArgumentList @('-m','vagasunb.server') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput '.private/admin.log' -RedirectStandardError '.private/admin-error.log' -PassThru
    $worker=Start-Process -FilePath $Python -ArgumentList @('-m','vagasunb.cli','worker') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput '.private/worker.log' -RedirectStandardError '.private/worker-error.log' -PassThru
    @(@{id=$admin.Id;component='admin'},@{id=$worker.Id;component='worker'}) | ConvertTo-Json | Set-Content -Encoding UTF8 '.private/processes.json'
} else { Compose @('up','-d','--build','admin','worker') }
Wait-Dashboard
Write-Host 'Demo ativa: http://127.0.0.1:8765'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
