. "$PSScriptRoot/common.ps1"
if (Test-Path -LiteralPath '.private/native.json') {
    Write-Host 'Diagnóstico sanitizado:'
    & $Python -m vagasunb.cli diagnose
} else { Compose @('logs','--tail','100','admin','worker') }
