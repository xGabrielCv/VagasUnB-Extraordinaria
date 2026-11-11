param([switch]$NoBrowser)
$ErrorActionPreference='Stop'
try{
 & "$PSScriptRoot/stop.ps1"
 & "$PSScriptRoot/start.ps1" -NoBrowser:$NoBrowser
}catch{throw "Reinício interrompido: $($_.Exception.Message)"}
