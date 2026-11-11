[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$ConfigPath,
    [string]$AllTaskName = "UriboMCP-AllSync",
    [string]$BeefTaskName = "UriboMCP-BeefSync"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Quote-TaskArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

# $PSScriptRoot is empty inside param() defaults on Windows PowerShell 5.1, so resolve here.
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$projectRootFull = [System.IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath $projectRootFull -PathType Container)) {
    throw "Project root does not exist: $projectRootFull"
}

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $projectRootFull "config.local.toml"
}
$configPathFull = [System.IO.Path]::GetFullPath($ConfigPath)
$pythonwPath = Join-Path $projectRootFull ".venv\Scripts\pythonw.exe"

foreach ($requiredPath in @($configPathFull, $pythonwPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required file does not exist: $requiredPath"
    }
}

$commonArguments = @(
    "-m"
    "uribo_mcp.scheduler"
    "--root"
    (Quote-TaskArgument $projectRootFull)
    "--config"
    (Quote-TaskArgument $configPathFull)
) -join " "

$allAction = New-ScheduledTaskAction `
    -Execute $pythonwPath `
    -Argument "$commonArguments --scope all" `
    -WorkingDirectory $projectRootFull
$beefAction = New-ScheduledTaskAction `
    -Execute $pythonwPath `
    -Argument "$commonArguments --scope beef" `
    -WorkingDirectory $projectRootFull

$allTriggers = @(
    New-ScheduledTaskTrigger -Daily -At "07:00"
)
$beefTriggers = @(
    "08:00", "11:00", "14:00", "17:00", "20:00", "23:00" |
        ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $AllTaskName `
    -Action $allAction `
    -Trigger $allTriggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Windowless daily refresh of all uriboMCP sources at 07:00." `
    -Force | Out-Null

Register-ScheduledTask `
    -TaskName $BeefTaskName `
    -Action $beefAction `
    -Trigger $beefTriggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Windowless BEEF+ refresh every three hours from 08:00 through 23:00." `
    -Force | Out-Null

$tasks = foreach ($taskName in @($AllTaskName, $BeefTaskName)) {
    $task = Get-ScheduledTask -TaskName $taskName
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{
        TaskName = $task.TaskName
        State = [string]$task.State
        Execute = $task.Actions[0].Execute
        Arguments = $task.Actions[0].Arguments
        NextRunTime = $info.NextRunTime
    }
}

[pscustomobject]@{
    Installed = $true
    User = $currentUser
    WindowMode = "pythonw.exe (no console window)"
    Tasks = $tasks
} | ConvertTo-Json -Depth 5
