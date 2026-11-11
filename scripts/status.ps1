. "$PSScriptRoot/common.ps1"
if (Test-Path -LiteralPath '.private/native.json') {
    & $Python -m vagasunb.cli diagnose
    Check-Exit 'diagnóstico'
    try { Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 3 | ConvertTo-Json } catch { Write-Host 'Dashboard parado ou degradado.' }
} else { Compose @('ps') }
Write-Host 'URL configurada: http://127.0.0.1:8765 (verifique o estado acima).'
