param(
    [string]$AppDir = $(Join-Path $env:USERPROFILE "AutoVPN"),
    [string]$RepoUrl = $(if ($env:AUTOVPN_REPO_URL) { $env:AUTOVPN_REPO_URL } else { "https://github.com/nevrozzkie/AutoVPN.git" }),
    [string]$AppHost = "127.0.0.1",
    [string]$AppPort = "8000",

    [Alias("EuIp")]
    [string]$EuHost = $env:EU_SSH_HOST,
    [Alias("EuLogin")]
    [string]$EuUser = $(if ($env:EU_SSH_USER) { $env:EU_SSH_USER } else { "root" }),
    [int]$EuPort = $(if ($env:EU_SSH_PORT) { [int]$env:EU_SSH_PORT } else { 22 }),
    [Alias("SshPassword")]
    [string]$EuPassword = $env:EU_SSH_PASSWORD,
    [Alias("SshKey")]
    [string]$EuKeyPath = $env:EU_SSH_KEY_PATH,

    [string]$CurrentIp = $env:CURRENT_IP,
    [string]$AdminUsername = $(if ($env:ADMIN_USERNAME) { $env:ADMIN_USERNAME } else { "admin" }),
    [string]$AdminPassword = $env:ADMIN_PASSWORD,
    [string]$AezaToken = $env:AEZA_TOKEN,
    [string]$AezaServiceId = $env:AEZA_SERVICE_ID,
    [string]$AezaDomain = $env:AEZA_IPV4_DOMAIN
)

$ErrorActionPreference = "Stop"

function Ask([string]$Prompt, [string]$Default = "") {
    if ($Default) {
        $Value = Read-Host "$Prompt [$Default]"
        if ($Value) { return $Value }
        return $Default
    }
    return Read-Host $Prompt
}

function Ask-SecretText([string]$Prompt) {
    $Secret = Read-Host $Prompt -AsSecureString
    $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Ptr)
    }
}

function New-RandomSecret {
    $Bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($Bytes)
    return [Convert]::ToBase64String($Bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Require-Command([string]$Name, [string]$InstallHint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is not installed. $InstallHint"
    }
}

function Load-EnvFile([string]$Path) {
    Get-Content $Path | ForEach-Object {
        $Line = $_.Trim()
        if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
            $Parts = $Line.Split("=", 2)
            [Environment]::SetEnvironmentVariable($Parts[0], $Parts[1], "Process")
        }
    }
}

function Write-EnvFile([string]$Path, [hashtable]$Values) {
    $Lines = @(
        "APP_HOST=$($Values.APP_HOST)",
        "APP_PORT=$($Values.APP_PORT)",
        "DATABASE_PATH=$($Values.DATABASE_PATH)",
        "ADMIN_USERNAME=$($Values.ADMIN_USERNAME)",
        "",
        "AEZA_API_BASE=https://my.aeza.net",
        "AEZA_TOKEN=$($Values.AEZA_TOKEN)",
        "AEZA_SERVICE_ID=$($Values.AEZA_SERVICE_ID)",
        "AEZA_IPV4_PAYMENT_METHOD=balance",
        "AEZA_IPV4_DOMAIN=$($Values.AEZA_IPV4_DOMAIN)",
        "AEZA_IPV4_AFTER_PURCHASE_DELAY_SECONDS=120",
        "",
        "EU_SSH_HOST=$($Values.EU_SSH_HOST)",
        "EU_SSH_USER=$($Values.EU_SSH_USER)",
        "EU_SSH_PORT=$($Values.EU_SSH_PORT)",
        "EU_SSH_KEY_PATH=$($Values.EU_SSH_KEY_PATH)",
        "EU_SSH_PASSWORD=$($Values.EU_SSH_PASSWORD)",
        "SSH_CONNECT_TIMEOUT_SECONDS=15",
        "",
        "VLESS_PORT=443",
        "VLESS_REALITY_TARGET=ok.ru:443",
        "VLESS_REALITY_SERVER_NAMES=ok.ru,www.ok.ru",
        "VLESS_REALITY_SERVER_NAME=ok.ru",
        "VLESS_REALITY_FINGERPRINT=chrome",
        "VLESS_REALITY_SPIDER_X=/",
        "HYSTERIA_PORT=8443",
        "AMNEZIA_PORT=51820",
        "AMNEZIA_NETWORK_PREFIX=10.66.66",
        "AMNEZIA_DNS=1.1.1.1, 8.8.8.8",
        "SSH_PORT=22",
        "",
        "IP_APPEAR_TIMEOUT_SECONDS=300",
        "IP_APPEAR_INTERVAL_SECONDS=5",
        "REBOOT_WAIT_SECONDS=30",
        "HEALTHCHECK_TIMEOUT_SECONDS=300",
        "HEALTHCHECK_INTERVAL_SECONDS=10"
    )
    Set-Content -Path $Path -Value $Lines -Encoding UTF8
}

function Write-RunScript([string]$Path, [string]$EnvPath, [string]$HostValue, [string]$PortValue) {
    $Content = @"
`$ErrorActionPreference = "Stop"
`$EnvFile = "$EnvPath"
Get-Content `$EnvFile | ForEach-Object {
    `$Line = `$_.Trim()
    if (`$Line -and -not `$Line.StartsWith("#") -and `$Line.Contains("=")) {
        `$Parts = `$Line.Split("=", 2)
        [Environment]::SetEnvironmentVariable(`$Parts[0], `$Parts[1], "Process")
    }
}
`$RunHost = if (`$env:APP_HOST) { `$env:APP_HOST } else { "$HostValue" }
`$RunPort = if (`$env:APP_PORT) { [int]`$env:APP_PORT } else { [int]"$PortValue" }
while (`$true) {
    try {
        `$BindAddress = if (`$RunHost -eq "0.0.0.0") { [Net.IPAddress]::Any } else { [Net.IPAddress]::Parse("127.0.0.1") }
        `$Listener = [Net.Sockets.TcpListener]::new(`$BindAddress, `$RunPort)
        `$Listener.Start()
        `$Listener.Stop()
        break
    }
    catch {
        `$RunPort += 1
    }
}
if (`$RunPort -ne [int]"$PortValue") {
    Write-Host "Port $PortValue is busy, using `$RunPort."
}
Write-Host "Open: http://`$RunHost`:`$RunPort/admin/setup"
& "$Path\.venv\Scripts\python.exe" -m uvicorn app.main:app --host `$RunHost --port `$RunPort
"@
    Set-Content -Path (Join-Path $Path "run-local.ps1") -Value $Content -Encoding UTF8
}

Write-Host "[autovpn] Windows local installer"
Require-Command "git" "Install Git for Windows: https://git-scm.com/download/win"

$PythonCommand = Get-Command "py" -ErrorAction SilentlyContinue
if ($PythonCommand) {
    $PythonExe = "py"
    $PythonArgs = @("-3")
}
elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    $PythonExe = "python"
    $PythonArgs = @()
}
else {
    throw "Python 3.12+ is not installed. Install it from https://www.python.org/downloads/windows/"
}

if ((Test-Path "pyproject.toml") -and (Test-Path "app")) {
    $AppDir = (Get-Location).Path
}
elseif (Test-Path (Join-Path $AppDir ".git")) {
    Write-Host "[autovpn] updating $AppDir"
    git -C $AppDir pull --ff-only
}
else {
    Write-Host "[autovpn] cloning $RepoUrl to $AppDir"
    git clone $RepoUrl $AppDir
}

Set-Location $AppDir

if (-not $CurrentIp) {
    $CurrentIp = $EuHost
}
if (-not $EuHost) {
    $EuPassword = ""
    $EuKeyPath = ""
}
if ($EuPassword) {
    $EuKeyPath = ""
}

# Require an admin login + password (with confirmation) at install time.
if (-not $AdminUsername) {
    $AdminUsername = Ask "Admin username" "admin"
}
if (-not $AdminUsername) { $AdminUsername = "admin" }
if (-not $AdminPassword) {
    while ($true) {
        $p1 = Ask-SecretText "Admin password"
        $p2 = Ask-SecretText "Confirm admin password"
        if (-not $p1) { Write-Host "Password cannot be empty."; continue }
        if ($p1 -ne $p2) { Write-Host "Passwords do not match, please try again."; continue }
        $AdminPassword = $p1
        break
    }
}

$DataDir = Join-Path $AppDir "data"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$EnvPath = Join-Path $AppDir ".env"
$DatabasePath = (Join-Path $DataDir "autovpn.sqlite3").Replace("\", "/")

Write-EnvFile $EnvPath @{
    APP_HOST = $AppHost
    APP_PORT = $AppPort
    DATABASE_PATH = $DatabasePath
    ADMIN_USERNAME = $AdminUsername
    ADMIN_PASSWORD = $AdminPassword
    AEZA_TOKEN = $AezaToken
    AEZA_SERVICE_ID = $AezaServiceId
    AEZA_IPV4_DOMAIN = $AezaDomain
    EU_SSH_HOST = $EuHost
    EU_SSH_USER = $EuUser
    EU_SSH_PORT = $EuPort
    EU_SSH_KEY_PATH = $EuKeyPath
    EU_SSH_PASSWORD = $EuPassword
}

Write-Host "[autovpn] creating virtualenv"
& $PythonExe @PythonArgs -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -e .

Load-EnvFile $EnvPath
$env:AUTOVPN_INITIAL_CURRENT_IP = $CurrentIp
$env:ADMIN_USERNAME = $AdminUsername
$env:ADMIN_PASSWORD = $AdminPassword
$BootstrapPy = @"
import os
from app.db import init_db, set_setting
from app.security import hash_password

init_db()
ip = os.getenv('AUTOVPN_INITIAL_CURRENT_IP', '')
if ip:
    set_setting('current_ip', ip)
set_setting('config.admin_username', os.environ.get('ADMIN_USERNAME') or 'admin')
pw = os.environ.get('ADMIN_PASSWORD', '')
if pw:
    set_setting('config.admin_password', hash_password(pw))
"@
$BootstrapPy | & ".\.venv\Scripts\python.exe" -

Write-RunScript $AppDir $EnvPath $AppHost $AppPort

Write-Host ""
Write-Host "AutoVPN local install is ready."
Write-Host "App dir: $AppDir"
Write-Host "Config: $EnvPath"
Write-Host "Run: powershell -ExecutionPolicy Bypass -File `"$AppDir\run-local.ps1`""
Write-Host "Open: http://$AppHost`:$AppPort/admin/setup"
