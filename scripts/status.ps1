. "$PSScriptRoot/common.ps1"
if (Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json')) {
    & $Python -m vagasunb.cli diagnose
    Check-Exit 'diagnóstico'
    try { Invoke-RestMethod "$DashboardUrl/health" -TimeoutSec 3 | ConvertTo-Json } catch { Write-Host 'Dashboard parado ou degradado.' }
} else { Compose @('ps') }
Write-Host "URL configurada: $DashboardUrl (verifique o estado acima)."
