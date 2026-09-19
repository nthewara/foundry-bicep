#Requires -Version 7.3
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Path)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Private-RecoveryFiles.ps1")
$Path = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
Write-PrivateRecoveryText -Path $Path -Content ([Console]::In.ReadToEnd())
