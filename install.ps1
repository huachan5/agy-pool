[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'agy-pool'),
    [switch]$NoPath
)
$ErrorActionPreference = 'Stop'
$python = Get-Command py.exe -CommandType Application
& $python.Source -3.12 -I -c 'import sqlite3'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
$dest = [IO.Path]::GetFullPath($InstallDir)
if ($dest.TrimEnd('\') -ieq $PSScriptRoot.TrimEnd('\')) { throw 'Choose an installation directory outside the source repository.' }
$package = Join-Path $dest 'agy_pool'
New-Item -ItemType Directory -Force -Path $package | Out-Null
Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'agy_pool') -Filter '*.py' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $package -Force
}
$shim = Join-Path $dest 'agy-pool.cmd'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'agy-pool.cmd') -Destination $shim -Force
& $shim init
if ($LASTEXITCODE -ne 0) { throw 'agy-pool initialization failed.' }
if (-not $NoPath) {
    $old = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($old -split ';' | Where-Object { $_ })
    if ($entries -notcontains $dest) {
        [Environment]::SetEnvironmentVariable('Path', (($entries + $dest) -join ';'), 'User')
    }
    Write-Output 'Open a new terminal to use agy-pool from PATH.'
}
Write-Output "Installed: $shim"
