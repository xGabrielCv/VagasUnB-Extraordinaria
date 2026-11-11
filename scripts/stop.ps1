. "$PSScriptRoot/common.ps1"
if (Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json')) { Stop-Native } else { Compose @('stop') }
Write-Host 'Somente esta instância foi parada. Dados e chaves preservados.'
