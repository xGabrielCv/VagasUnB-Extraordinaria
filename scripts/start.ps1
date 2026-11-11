param([switch]$NoBrowser)
. "$PSScriptRoot/common.ps1"
if(Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json')){
 $cfg=Native-Config
 & (Join-Path $cfg.bin 'pg_ctl.exe') -D $cfg.data status *> $null
 if($LASTEXITCODE -ne 0){Native-Pg 'pg_ctl.exe' @('-D',$cfg.data,'-l',(Join-Path $PrivateRoot 'postgres.log'),'-o',"-p $($cfg.port) -h 127.0.0.1",'-w','start')}
 try{$tcp=[Net.Sockets.TcpClient]::new('127.0.0.1',$DashboardPort);$tcp.Dispose();$occupied=$true}catch{$occupied=$false}
 if($occupied){
  $health=Local-Health
  $records=Recover-State $health
  if($health -and $health.instance -eq $InstanceId -and $records.Count -eq 2 -and $health.worker.alive){$records | ConvertTo-Json | Set-Content -Encoding UTF8 $StatePath;Write-Host "Instância já ativa: $DashboardUrl";return}
  throw 'Porta ocupada por outra instância ou processo degradado. Nenhum processo alheio foi encerrado; use status/stop para esta instância.'
 }
 $old=@(Read-State | Where-Object{Owned-Process $_})
 if($old.Count){
  $other=@($old | Where-Object {$_.url -and $_.url -ne $DashboardUrl})
  if($other.Count){throw 'Já existe processo desta instância em outra URL. Use status/stop antes de mudar a porta; a instância anterior foi preservada.'}
  Stop-Apps
 }
 & (Join-Path $ProjectRoot '.venv/Scripts/alembic.exe') upgrade head
 Check-Exit 'migrações'
 $shutdown=Join-Path $PrivateRoot 'shutdown'
 if(Test-Path -LiteralPath $shutdown){Remove-Item -LiteralPath $shutdown}
 $created=@()
 try{
  foreach($component in @('admin','worker')){
   $arguments=if($component -eq 'admin'){@('-m','vagasunb.server')}else{@('-m','vagasunb.cli','worker')}
   $launcher=Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PrivateRoot "$component.log") -RedirectStandardError (Join-Path $PrivateRoot "$component-error.log") -PassThru
   $p=Get-CimInstance Win32_Process -Filter "ProcessId = $($launcher.Id)"
   if($p){$created+=Record-Process $p $component}
   Start-Sleep -Milliseconds 300
   foreach($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($launcher.Id)" | Where-Object{$_.Name -eq 'python.exe' -and $_.CommandLine -match '-m\s+vagasunb\.'})){$created+=Record-Process $child $component}
   $created | ConvertTo-Json | Set-Content -Encoding UTF8 $StatePath
  }
  $health=Wait-Dashboard
  $records=Recover-State $health
  $records | ConvertTo-Json | Set-Content -Encoding UTF8 $StatePath
 }catch{Stop-Apps;throw}
}else{
 Compose @('up','-d','--build','admin','worker')
 $null=Wait-Dashboard
}
Write-Host "Aplicação ativa: $DashboardUrl — abra o painel (cadastro de senha somente no primeiro acesso)."
if(-not $NoBrowser){Start-Process $DashboardUrl}
