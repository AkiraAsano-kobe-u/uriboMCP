[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ConfigPath,

    [string]$GeneratedDirectory,

    [switch]$RunSmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$errors = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()
$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $checks.Add([pscustomobject]@{ Name = $Name; Passed = $Passed; Detail = $Detail })
    if (-not $Passed) { $errors.Add("${Name}: $Detail") }
}

$projectRootFull = [System.IO.Path]::GetFullPath($ProjectRoot)
$configPathFull = [System.IO.Path]::GetFullPath($ConfigPath)
Add-Check -Name "project root" -Passed (Test-Path -LiteralPath $projectRootFull -PathType Container) -Detail $projectRootFull
Add-Check -Name "pyproject.toml" -Passed (Test-Path -LiteralPath (Join-Path $projectRootFull "pyproject.toml") -PathType Leaf) -Detail "Expected at project root"
Add-Check -Name "configuration file" -Passed (Test-Path -LiteralPath $configPathFull -PathType Leaf) -Detail $configPathFull

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    $warnings.Add("uv was not found on PATH. Install uv before starting the server.")
    $checks.Add([pscustomobject]@{ Name = "uv command"; Passed = $false; Detail = "Not found on PATH (warning)" })
} else {
    $checks.Add([pscustomobject]@{ Name = "uv command"; Passed = $true; Detail = $uv.Source })
}

if ($GeneratedDirectory) {
    $generatedFull = [System.IO.Path]::GetFullPath($GeneratedDirectory)
    Add-Check -Name "generated directory" -Passed (Test-Path -LiteralPath $generatedFull -PathType Container) -Detail $generatedFull
    if (Test-Path -LiteralPath $generatedFull -PathType Container) {
        $desktopPath = Join-Path $generatedFull "claude_desktop_config.json"
        Add-Check -Name "Claude Desktop JSON" -Passed (Test-Path -LiteralPath $desktopPath -PathType Leaf) -Detail $desktopPath
        if (Test-Path -LiteralPath $desktopPath -PathType Leaf) {
            try {
                $desktop = Get-Content -LiteralPath $desktopPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $server = $desktop.mcpServers."uribo-mcp"
                $matches = $server.args[1] -eq $projectRootFull -and $server.env.URIBO_MCP_CONFIG -eq $configPathFull
                Add-Check -Name "Claude Desktop generated paths" -Passed $matches -Detail "Expected generated absolute paths"
            } catch {
                Add-Check -Name "Claude Desktop JSON parse" -Passed $false -Detail $_.Exception.Message
            }
        }
        foreach ($name in @("codex.toml.fragment", "register-claude-code-user.ps1", "register-codex-user.ps1")) {
            Add-Check -Name "generated $name" -Passed (Test-Path -LiteralPath (Join-Path $generatedFull $name) -PathType Leaf) -Detail $name
        }
    }
}

if ($RunSmokeTest) {
    if ($null -eq $uv) {
        Add-Check -Name "server smoke test" -Passed $false -Detail "uv is unavailable"
    } else {
        & $uv.Source --directory $projectRootFull run uribo-mcp --help
        Add-Check -Name "server smoke test" -Passed ($LASTEXITCODE -eq 0) -Detail "uv run uribo-mcp --help"
    }
}

$result = [pscustomobject]@{
    Passed = ($errors.Count -eq 0)
    Checks = $checks
    Warnings = $warnings
    Errors = $errors
}
$result | ConvertTo-Json -Depth 5

if ($errors.Count -gt 0) {
    exit 1
}
