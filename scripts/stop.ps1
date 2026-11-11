. "$PSScriptRoot/common.ps1"
if (Test-Path -LiteralPath '.private/native.json') { Stop-Native } else { Compose @('stop') }
Write-Host 'Somente esta instância foi parada. Dados e chaves preservados.'
