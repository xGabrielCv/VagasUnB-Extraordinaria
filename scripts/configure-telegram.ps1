param([switch]$SaveOnly, [switch]$Public, [switch]$Connect)
. "$PSScriptRoot/common.ps1"
$secretsDir = if ($Public) { Join-Path $PrivateRoot 'public/secrets' } else { Join-Path $PrivateRoot 'secrets' }
New-Item -ItemType Directory -Force $secretsDir | Out-Null
$prompt = if ($Connect) { 'Token do BotFather' } else { 'Token futuro (somente gravacao; nenhuma conexao)' }
$secret = Read-Host $prompt -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ($plain -notmatch '^\d+:[A-Za-z0-9_-]{20,}$') { throw 'Formato de token invalido.' }
    if ($Connect -and -not $SaveOnly) {
        try {
            $me = Invoke-RestMethod "https://api.telegram.org/bot$plain/getMe" -TimeoutSec 8
            if ($me.ok -and $me.result.username) {
                Write-Host "Bot validado no Telegram: https://t.me/$($me.result.username)"
            }
        } catch {
            Write-Warning 'Nao foi possivel validar getMe agora; o token foi salvo normalmente.'
        }
    }
    [IO.File]::WriteAllText((Join-Path $secretsDir 'telegram_token'), $plain)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    $plain = $null
}
if ($Connect -and -not $SaveOnly) {
    Write-Host 'Token salvo. Para ativar envio real em modo publico, defina VAGAS_TELEGRAM_ENABLE=1 e reinicie worker + bot.'
} else {
    Write-Host 'Token gravado privadamente. Nenhum metodo Telegram foi chamado; modo e servicos nao foram alterados.'
}
