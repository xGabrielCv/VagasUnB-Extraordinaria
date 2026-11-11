param([switch]$SaveOnly)
. "$PSScriptRoot/common.ps1"
$secret=Read-Host 'Token futuro (somente gravação; nenhuma conexão)' -AsSecureString
$ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $plain=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ($plain -notmatch '^\d+:[A-Za-z0-9_-]{20,}$') { throw 'Formato de token inválido.' }
    [IO.File]::WriteAllText((Join-Path $PrivateRoot 'secrets/telegram_token'),$plain)
} finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $plain=$null }
Write-Host 'Token gravado privadamente. Nenhum método Telegram foi chamado; modo e serviços não foram alterados.'
