[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ConfigPath,

    [string]$OutputDirectory,

    [string]$UvCommand = "uv",

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-AbsolutePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path)
}

function Write-GeneratedTextFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    if ((Test-Path -LiteralPath $Path) -and -not $Force) {
        throw "Refusing to overwrite '$Path'. Re-run with -Force after reviewing it."
    }
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

# $PSScriptRoot is empty inside param() defaults on Windows PowerShell 5.1, so resolve here.
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot "..\packaging\generated" }
$projectRootFull = Resolve-AbsolutePath -Path $ProjectRoot
$configPathFull = Resolve-AbsolutePath -Path $ConfigPath
$outputDirectoryFull = Resolve-AbsolutePath -Path $OutputDirectory

if (-not (Test-Path -LiteralPath $projectRootFull -PathType Container)) {
    throw "ProjectRoot does not exist or is not a directory: '$projectRootFull'."
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRootFull "pyproject.toml") -PathType Leaf)) {
    throw "ProjectRoot must contain pyproject.toml: '$projectRootFull'."
}
if (-not [System.IO.Path]::IsPathRooted($configPathFull)) {
    throw "ConfigPath must resolve to an absolute path."
}

[System.IO.Directory]::CreateDirectory($outputDirectoryFull) | Out-Null

$server = [ordered]@{
    command = $UvCommand
    args = @("--directory", $projectRootFull, "run", "uribo-mcp", "serve")
    env = [ordered]@{ URIBO_MCP_CONFIG = $configPathFull }
}

$claudeDesktop = [ordered]@{ mcpServers = [ordered]@{ "uribo-mcp" = $server } }
$claudeDesktopPath = Join-Path $outputDirectoryFull "claude_desktop_config.json"
Write-GeneratedTextFile -Path $claudeDesktopPath -Content (($claudeDesktop | ConvertTo-Json -Depth 8) + [Environment]::NewLine)

$tomlProjectRoot = $projectRootFull.Replace("\\", "\\\\").Replace('"', '\\"')
$tomlConfigPath = $configPathFull.Replace("\\", "\\\\").Replace('"', '\\"')
$tomlUvCommand = $UvCommand.Replace("\\", "\\\\").Replace('"', '\\"')
$codexFragment = @"
[mcp_servers.uribo-mcp]
command = "$tomlUvCommand"
args = ["--directory", "$tomlProjectRoot", "run", "uribo-mcp", "serve"]
cwd = "$tomlProjectRoot"
startup_timeout_sec = 20
tool_timeout_sec = 60

[mcp_servers.uribo-mcp.env]
URIBO_MCP_CONFIG = "$tomlConfigPath"
"@
$codexFragmentPath = Join-Path $outputDirectoryFull "codex.toml.fragment"
Write-GeneratedTextFile -Path $codexFragmentPath -Content $codexFragment

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

$quotedConfig = ConvertTo-PowerShellSingleQuotedLiteral -Value "URIBO_MCP_CONFIG=$configPathFull"
$quotedUv = ConvertTo-PowerShellSingleQuotedLiteral -Value $UvCommand
$quotedProject = ConvertTo-PowerShellSingleQuotedLiteral -Value $projectRootFull

$claudeCodeRegistration = @"
# Review this command before running it. It adds a user-scoped Claude Code MCP setting.
& claude mcp add --transport stdio uribo-mcp --scope user --env $quotedConfig -- $quotedUv --directory $quotedProject run uribo-mcp serve
"@
$claudeCodeRegistrationPath = Join-Path $outputDirectoryFull "register-claude-code-user.ps1"
Write-GeneratedTextFile -Path $claudeCodeRegistrationPath -Content $claudeCodeRegistration

$codexRegistration = @"
# Review this command before running it. It adds a user-scoped Codex MCP setting.
& codex mcp add uribo-mcp --env $quotedConfig -- $quotedUv --directory $quotedProject run uribo-mcp serve
"@
$codexRegistrationPath = Join-Path $outputDirectoryFull "register-codex-user.ps1"
Write-GeneratedTextFile -Path $codexRegistrationPath -Content $codexRegistration

$readme = @"
Generated without changing any Claude or Codex user settings.

1. Claude Desktop: merge claude_desktop_config.json into its existing mcpServers object.
2. Claude Code: review and run .\\register-claude-code-user.ps1.
3. Codex: either merge codex.toml.fragment into %USERPROFILE%\\.codex\\config.toml, or review and run .\\register-codex-user.ps1.

All paths are absolute and are stored as argument values, not shell-concatenated command lines.
"@
Write-GeneratedTextFile -Path (Join-Path $outputDirectoryFull "README.txt") -Content $readme

[pscustomobject]@{
    ProjectRoot = $projectRootFull
    ConfigPath = $configPathFull
    OutputDirectory = $outputDirectoryFull
    Files = @(
        $claudeDesktopPath,
        $codexFragmentPath,
        $claudeCodeRegistrationPath,
        $codexRegistrationPath,
        (Join-Path $outputDirectoryFull "README.txt")
    )
}
