# PowerShell 5.1+; no global execution-policy changes.
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $ProjectRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "Falha em: $Step (código $LASTEXITCODE)." }
}
function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker ausente. Instale Docker Desktop com Linux containers, abra-o e execute scripts/setup.ps1. Alternativa: setup.ps1 -NativePgBin <pasta bin de PostgreSQL 17>.' }
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    Check-Exit 'Docker Desktop: abra o aplicativo e aguarde o engine iniciar'
}
function Compose([string[]]$Arguments) {
    Assert-Docker
    & docker compose --project-name vagasunb-extraordinaria-demo @Arguments
    Check-Exit 'Docker Compose deste projeto'
}
function Native-Config { Get-Content -Raw -LiteralPath '.private/native.json' | ConvertFrom-Json }
function Native-Pg([string]$Command, [string[]]$Arguments) {
    $cfg = Native-Config
    & (Join-Path $cfg.bin $Command) @Arguments
    Check-Exit $Command
}
function Wait-Dashboard {
    $deadline = (Get-Date).AddSeconds(90)
    do {
        try { $response=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/health' -TimeoutSec 2; if ($response.StatusCode -eq 200) { return } } catch { }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw 'Dashboard não ficou saudável em 90 s. Execute scripts/logs.ps1.'
}
function Stop-Native {
    $path = '.private/processes.json'
    if (Test-Path -LiteralPath $path) {
        New-Item -ItemType File -Force -Path '.private/shutdown' | Out-Null
        $items = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
        foreach ($item in $items) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($item.id)" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -like '*vagasunb*' -and $proc.ExecutablePath -eq $Python) {
                $null = Wait-Process -Id $item.id -Timeout 25 -ErrorAction SilentlyContinue
                if (Get-Process -Id $item.id -ErrorAction SilentlyContinue) {
                    Get-CimInstance Win32_Process -Filter "ParentProcessId = $($item.id)" | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*vagasunb*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
                    Stop-Process -Id $item.id -ErrorAction SilentlyContinue
                }
            }
        }
        Remove-Item -LiteralPath $path
    }
    $cfg=Native-Config
    & (Join-Path $cfg.bin 'pg_ctl.exe') -D $cfg.data status *> $null
    if ($LASTEXITCODE -eq 0) { Native-Pg 'pg_ctl.exe' @('-D',$cfg.data,'-m','fast','-w','stop') }
}
