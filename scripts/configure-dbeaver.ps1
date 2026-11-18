param([switch]$EnsureInstalled, [switch]$Open)
. "$PSScriptRoot/common.ps1"

function Find-DBeaverExe {
    @(
        (Join-Path $env:LOCALAPPDATA 'DBeaver\dbeaver.exe'),
        'C:\Program Files\DBeaver\dbeaver.exe'
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

$exe = Find-DBeaverExe
if (-not $exe -and $EnsureInstalled) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'DBeaver Community nao encontrado e winget indisponivel para instalacao automatica.'
    }
    Write-Host 'Instalando DBeaver Community...'
    & winget install --id DBeaver.DBeaver.Community --exact --silent --accept-package-agreements --accept-source-agreements
    Check-Exit 'instalacao do DBeaver Community'
    $exe = Find-DBeaverExe
}

$configPath = Join-Path $PrivateRoot 'config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'Instancia local ainda nao configurada. Execute .\scripts\setup.ps1 ou .\scripts\run.ps1.'
}
$cfg = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$uri = [uri]($cfg.database_url -replace '^postgresql\+psycopg://', 'postgresql://')
$pgHost = if ($uri.Host) { $uri.Host } else { '127.0.0.1' }
$pgPort = if ($uri.Port -gt 0) { "$($uri.Port)" } else { '5432' }
$pgDb = $uri.AbsolutePath.TrimStart('/')
$pgUser = if ($uri.UserInfo) { ($uri.UserInfo -split ':', 2)[0] } else { 'vagas' }
$pgPassFile = Join-Path $PrivateRoot 'secrets/postgres_password'
if (-not (Test-Path -LiteralPath $pgPassFile)) {
    throw "Arquivo de senha ausente: $pgPassFile"
}
$pgPass = (Get-Content -Raw -LiteralPath $pgPassFile).Trim()

$dbeaverWorkspace = Join-Path $env:APPDATA 'DBeaverData\workspace6'
$generalDir = Join-Path $dbeaverWorkspace 'General'
$dbeaverDir = Join-Path $generalDir '.dbeaver'
New-Item -ItemType Directory -Force $dbeaverDir | Out-Null

# Ensure Eclipse project descriptor exists so DBeaver recognizes General on first open
$projectFile = Join-Path $generalDir '.project'
if (-not (Test-Path -LiteralPath $projectFile)) {
    [IO.File]::WriteAllText($projectFile, @"
<?xml version="1.0" encoding="UTF-8"?>
<projectDescription>
	<name>General</name>
	<comment></comment>
	<projects>
	</projects>
	<buildSpec>
	</buildSpec>
	<natures>
		<nature>org.jkiss.dbeaver.DBeaverNature</nature>
	</natures>
</projectDescription>
"@, [Text.UTF8Encoding]::new($false))
}

# Suppress first-run sample database prompt so DBeaver opens straight to our connection
$settingsDir = Join-Path $dbeaverWorkspace '.metadata\.plugins\org.eclipse.core.runtime\.settings'
New-Item -ItemType Directory -Force $settingsDir | Out-Null
$corePrefs = Join-Path $settingsDir 'org.jkiss.dbeaver.core.prefs'
$prefsText = if (Test-Path -LiteralPath $corePrefs) { Get-Content -Raw -LiteralPath $corePrefs } else { '' }
if ($prefsText -notmatch 'sample\.database\.canceled=') {
    Add-Content -LiteralPath $corePrefs -Value "eclipse.preferences.version=1`nsample.database.canceled=true`ntipOfTheDay=false"
}

$driverDir = Join-Path $env:APPDATA 'DBeaverData\drivers\maven\maven-central\org.postgresql'
$jarFile = Join-Path $driverDir 'postgresql-42.7.13.jar'
$pomFile = Join-Path $driverDir 'postgresql-42.7.13.pom'

# Fast idempotent check: if everything matches current PostgreSQL endpoint + secret + JDBC cache, do nothing
$dsPath = Join-Path $dbeaverDir 'data-sources.json'
$credPath = Join-Path $dbeaverDir 'credentials-config.json'
$alreadyReady = $false
if ((Test-Path -LiteralPath $dsPath) -and (Test-Path -LiteralPath $credPath) -and (Test-Path -LiteralPath $jarFile) -and (Test-Path -LiteralPath $pomFile)) {
    $env:VAGAS_DBEAVER_DS_PATH = $dsPath
    $env:VAGAS_DBEAVER_CRED_PATH = $credPath
    $env:VAGAS_DBEAVER_URL = "jdbc:postgresql://${pgHost}:${pgPort}/${pgDb}"
    $env:VAGAS_DBEAVER_USER = $pgUser
    $env:VAGAS_DBEAVER_PASS = $pgPass
    try {
        & $Python -c @"
import json, os, sys
from pathlib import Path
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DBS_KEY = bytes([186, 187, 74, 159, 119, 74, 184, 83, 201, 108, 45, 101, 61, 254, 84, 74])
ds = json.loads(Path(os.environ['VAGAS_DBEAVER_DS_PATH']).read_text(encoding='utf-8')).get('connections', {}).get('vagasunb-local', {})
cfg = ds.get('configuration', {})
if not (ds.get('save-password') is True and cfg.get('url') == os.environ['VAGAS_DBEAVER_URL'] and cfg.get('user') == os.environ['VAGAS_DBEAVER_USER']):
    sys.exit(1)
raw = Path(os.environ['VAGAS_DBEAVER_CRED_PATH']).read_bytes()
dec = Cipher(algorithms.AES(DBS_KEY), modes.CBC(raw[:16])).decryptor()
padded = dec.update(raw[16:]) + dec.finalize()
unpadder = padding.PKCS7(128).unpadder()
creds = json.loads((unpadder.update(padded) + unpadder.finalize()).decode('utf-8')).get('vagasunb-local', {}).get('#connection', {})
if creds.get('user') != os.environ['VAGAS_DBEAVER_USER'] or creds.get('password') != os.environ['VAGAS_DBEAVER_PASS']:
    sys.exit(1)
"@ 2>$null
        $alreadyReady = ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item Env:VAGAS_DBEAVER_DS_PATH, Env:VAGAS_DBEAVER_CRED_PATH, Env:VAGAS_DBEAVER_URL, Env:VAGAS_DBEAVER_USER, Env:VAGAS_DBEAVER_PASS -ErrorAction SilentlyContinue
    }
}

if ($alreadyReady) {
    Write-Host "DBeaver Community ja configurado (${pgHost}:${pgPort}/${pgDb})."
    if ($Open -and $exe) {
        Start-Process -FilePath $exe -ArgumentList @('-nosplash', '-con', 'id=vagasunb-local|connect=true|openConsole=true')
    }
    return
}

$data = [ordered]@{ folders = [ordered]@{}; connections = [ordered]@{} }
if (Test-Path -LiteralPath $dsPath) {
    try {
        $existing = Get-Content -Raw -LiteralPath $dsPath | ConvertFrom-Json -AsHashtable
        if ($existing.folders) { $data.folders = $existing.folders }
        if ($existing.connections) { $data.connections = $existing.connections }
    } catch { }
}

$data.connections['vagasunb-local'] = [ordered]@{
    provider = 'postgresql'
    driver = 'postgres-jdbc'
    name = 'VagasUnB Local (PostgreSQL)'
    'save-password' = $true
    configuration = [ordered]@{
        host = $pgHost
        port = $pgPort
        database = $pgDb
        url = "jdbc:postgresql://${pgHost}:${pgPort}/${pgDb}"
        configurationType = 'MANUAL'
        type = 'dev'
        authModel = 'native'
        user = $pgUser
    }
}

[IO.File]::WriteAllText($dsPath, ($data | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))

# Encrypt credentials into DBeaver credentials-config.json without exposing plaintext secrets.
$credPath = Join-Path $dbeaverDir 'credentials-config.json'
$env:VAGAS_DBEAVER_CRED_PATH = $credPath
$env:VAGAS_DBEAVER_USER = $pgUser
$env:VAGAS_DBEAVER_PASS = $pgPass
try {
    & $Python -c @"
import json, os
from pathlib import Path
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DBS_KEY = bytes([186, 187, 74, 159, 119, 74, 184, 83, 201, 108, 45, 101, 61, 254, 84, 74])
cred_path = Path(os.environ['VAGAS_DBEAVER_CRED_PATH'])
data = {}
if cred_path.exists():
    raw = cred_path.read_bytes()
    if len(raw) > 16:
        try:
            dec = Cipher(algorithms.AES(DBS_KEY), modes.CBC(raw[:16])).decryptor()
            padded = dec.update(raw[16:]) + dec.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            plain = unpadder.update(padded) + unpadder.finalize()
            data = json.loads(plain.decode('utf-8'))
        except Exception:
            data = {}
data['vagasunb-local'] = {
    '#connection': {
        'user': os.environ['VAGAS_DBEAVER_USER'],
        'password': os.environ['VAGAS_DBEAVER_PASS'],
    }
}
payload = json.dumps(data, separators=(',', ':')).encode('utf-8')
padder = padding.PKCS7(128).padder()
padded = padder.update(payload) + padder.finalize()
iv = os.urandom(16)
enc = Cipher(algorithms.AES(DBS_KEY), modes.CBC(iv)).encryptor()
cred_path.write_bytes(iv + enc.update(padded) + enc.finalize())
"@
    Check-Exit 'gravacao das credenciais do DBeaver'
} finally {
    Remove-Item Env:VAGAS_DBEAVER_CRED_PATH, Env:VAGAS_DBEAVER_USER, Env:VAGAS_DBEAVER_PASS -ErrorAction SilentlyContinue
    $pgPass = $null
}

# Ensure PostgreSQL JDBC driver is cached locally so DBeaver connects without prompting.
$driverDir = Join-Path $env:APPDATA 'DBeaverData\drivers\maven\maven-central\org.postgresql'
New-Item -ItemType Directory -Force $driverDir | Out-Null
$existingDriver = Get-ChildItem -LiteralPath $driverDir -Filter 'postgresql-*.jar' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $existingDriver) {
    $baseUrl = 'https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.13/postgresql-42.7.13'
    Write-Host 'Baixando driver JDBC do PostgreSQL para o DBeaver...'
    try {
        Invoke-WebRequest -Uri "$baseUrl.jar" -OutFile (Join-Path $driverDir 'postgresql-42.7.13.jar') -UseBasicParsing
        Invoke-WebRequest -Uri "$baseUrl.pom" -OutFile (Join-Path $driverDir 'postgresql-42.7.13.pom') -UseBasicParsing
    } catch {
        Write-Warning 'Nao foi possivel baixar o driver JDBC automaticamente; o DBeaver baixara ao conectar.'
    }
}

Write-Host "Conexao 'VagasUnB Local (PostgreSQL)' configurada no DBeaver (${pgHost}:${pgPort}/${pgDb}, usuario ${pgUser}, senha salva)."

if ($Open) {
    if ($exe) {
        Start-Process -FilePath $exe -ArgumentList @('-nosplash', '-con', 'id=vagasunb-local|connect=true|openConsole=true')
    } else {
        Write-Warning 'DBeaver Community nao encontrado. Use -EnsureInstalled para instalar.'
    }
}
