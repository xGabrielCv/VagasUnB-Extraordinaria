# PowerShell 5.1+; scoped to this project and its configured private directory.
$ErrorActionPreference='Stop'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$OutputEncoding=[Console]::OutputEncoding
$ProjectRoot=Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $ProjectRoot
$PrivateRoot=if($env:VAGAS_PRIVATE){[IO.Path]::GetFullPath($env:VAGAS_PRIVATE)}else{Join-Path $ProjectRoot '.private'}
$env:VAGAS_PRIVATE=$PrivateRoot
$Python=Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$env:VAGAS_HOST=if($env:VAGAS_HOST){$env:VAGAS_HOST.Replace('localhost:','127.0.0.1:')}else{'127.0.0.1:8765'}
if($env:VAGAS_HOST -notmatch '^127\.0\.0\.1:(\d+)$'){throw 'Use VAGAS_HOST=127.0.0.1:porta. IPv6 não é suportado pelos scripts nativos.'}
$DashboardPort=[int]$Matches[1]
$DashboardUrl="http://$($env:VAGAS_HOST)"
$sha=[Security.Cryptography.SHA256]::Create()
$InstanceId=([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($PrivateRoot)))).Replace('-','').ToLower().Substring(0,20)
$StatePath=Join-Path $PrivateRoot 'processes.json'
function Check-Exit([string]$Step){if($LASTEXITCODE -ne 0){throw "Falha em $Step (código $LASTEXITCODE)."}}
function Assert-Docker {
 if(-not(Get-Command docker -ErrorAction SilentlyContinue)){throw 'Docker ausente. Abra Docker Desktop, ou use setup.ps1 -NativePgBin <pasta bin PostgreSQL 17>.'}
 docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
 Check-Exit 'Docker Desktop'
}
function Compose([string[]]$Arguments){Assert-Docker; & docker compose --project-name vagasunb-extraordinaria-demo @Arguments; Check-Exit 'Docker Compose'}
function Native-Config {Get-Content -Raw -LiteralPath (Join-Path $PrivateRoot 'native.json') | ConvertFrom-Json}
function Native-Pg([string]$Command,[string[]]$Arguments){
 $cfg=Native-Config
 if($Command -eq 'pg_ctl.exe' -and $Arguments -contains 'start'){
  $quoted=@($Arguments | ForEach-Object {'"'+$_+'"'})
  $tool=Start-Process -FilePath (Join-Path $cfg.bin $Command) -ArgumentList $quoted -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PrivateRoot 'pg-tool.log') -RedirectStandardError (Join-Path $PrivateRoot 'pg-tool-error.log') -PassThru
  $null=$tool.Handle
  if(-not $tool.WaitForExit(30000)){throw 'Timeout no inicializador PostgreSQL; confira status e logs privados.'}
  $tool.Refresh()
  if($tool.ExitCode -ne 0){throw "Falha em PostgreSQL/start (código $($tool.ExitCode)). Confira caminhos, porta e logs privados."}
 }else{& (Join-Path $cfg.bin $Command) @Arguments;Check-Exit $Command}
}
function Read-State {try{if(Test-Path -LiteralPath $StatePath){$data=Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json;foreach($item in $data){Write-Output $item}}}catch{}}
function Owned-Process($record){
 if(-not $record.started -or -not $record.exe){return $null}
 $proc=Get-CimInstance Win32_Process -Filter "ProcessId = $($record.id)" -ErrorAction SilentlyContinue
 if($proc -and $proc.ExecutablePath -eq $record.exe -and $proc.CommandLine -match '-m\s+vagasunb\.(server|cli\s+worker)' -and [Math]::Abs(($proc.CreationDate.ToUniversalTime()-[datetime]::Parse($record.started).ToUniversalTime()).TotalSeconds) -eq 0){return $proc}
 return $null
}
function Record-Process($proc,[string]$component){@{url=$DashboardUrl;id=$proc.ProcessId;started=$proc.CreationDate.ToUniversalTime().ToString('o');exe=$proc.ExecutablePath;component=$component}}
function Local-Health {try{return Invoke-RestMethod "$DashboardUrl/health" -TimeoutSec 2}catch{return $null}}
function Recover-State($health){
 if(-not $health -or $health.instance -ne $InstanceId){return @()}
 $baseHome=((Get-Content -LiteralPath (Join-Path $ProjectRoot '.venv/pyvenv.cfg') | Where-Object{$_ -like 'home = *'}) -replace '^home = ','').Trim()
 $allowed=@($Python,(Join-Path $baseHome 'python.exe'))
 $records=@()
 foreach($pair in @(@{id=$health.pid;component='admin'},@{id=$health.worker.pid;component='worker'})){
  if(-not $pair.id){continue}
  $p=Get-CimInstance Win32_Process -Filter "ProcessId = $($pair.id)" -ErrorAction SilentlyContinue
  if($p -and $p.ExecutablePath -in $allowed -and $p.CommandLine -match '-m\s+vagasunb\.(server|cli\s+worker)'){$records+=Record-Process $p $pair.component}
 }
 return $records
}
function Wait-Dashboard {
 $deadline=(Get-Date).AddSeconds(60)
 do{
  $health=Local-Health
  if($health -and $health.worker.alive -and $health.worker.collect -ne 'starting' -and $health.worker.dispatch -ne 'starting' -and (($health.instance -eq $InstanceId) -or (-not (Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json')) -and $health.service -eq 'vagasunb-extraordinaria'))){return $health}
  $records=Read-State
  if($records.Count -and @($records | Where-Object{Owned-Process $_}).Count -ne $records.Count){throw 'Processo admin/worker terminou. Execute scripts/logs.ps1.'}
  Start-Sleep -Milliseconds 500
 }while((Get-Date)-lt $deadline)
 throw 'Admin ou worker não ficou saudável em 60 s. Execute scripts/logs.ps1.'
}
function Stop-Apps {
 $records=Read-State
 if(-not $records.Count){$records=Recover-State (Local-Health)}
 $owned=@($records | Where-Object{Owned-Process $_})
 if($owned.Count){
  New-Item -ItemType File -Force -Path (Join-Path $PrivateRoot 'shutdown') | Out-Null
  $deadline=(Get-Date).AddSeconds(30)
  do{$alive=@($owned | Where-Object{Owned-Process $_});if(-not $alive.Count){break};Start-Sleep -Milliseconds 500}while((Get-Date)-lt $deadline)
  foreach($record in $alive){if(Owned-Process $record){Stop-Process -Id $record.id;Wait-Process -Id $record.id -Timeout 5 -ErrorAction SilentlyContinue}}
  if(@($owned | Where-Object{Owned-Process $_}).Count){throw 'Não foi possível parar processos desta instância.'}
 }
 if(Test-Path -LiteralPath $StatePath){Remove-Item -LiteralPath $StatePath}
}
function Stop-Native {
 Stop-Apps
 $cfg=Native-Config
 & (Join-Path $cfg.bin 'pg_ctl.exe') -D $cfg.data status *> $null
 if($LASTEXITCODE -eq 0){Native-Pg 'pg_ctl.exe' @('-D',$cfg.data,'-m','fast','-w','stop')}
}
