param(
    [Parameter(Mandatory=$true)][string]$Certificate,
    [Parameter(Mandatory=$true)][string]$PrivateKey,
    [ValidateRange(1,65535)][int]$Port = 7443
)
$ErrorActionPreference = 'Stop'
$serverExe = Join-Path $PSScriptRoot 'dist/SeedRoomServer.exe'
if (-not (Test-Path -LiteralPath $serverExe)) { throw 'Packaged room server is missing.' }
$certificatePath = (Resolve-Path -LiteralPath $Certificate).Path
$privateKeyPath = (Resolve-Path -LiteralPath $PrivateKey).Path
Write-Host "Starting TLS room host on TCP $Port. Router and firewall reachability must be configured separately."
& $serverExe --host 0.0.0.0 --port $Port --cert $certificatePath --key $privateKeyPath
