param([switch]$NoBrowser)
& "$PSScriptRoot/stop.ps1"
& "$PSScriptRoot/start.ps1" -NoBrowser:$NoBrowser
