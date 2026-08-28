[CmdletBinding()]
param(
    [string]$ProjectRoot = (Join-Path $PSScriptRoot ".."),
    [string]$OutputPath = (Join-Path $PSScriptRoot "..\\packaging\\artifacts\\uribo-mcp.mcpb"),
    [string]$StagingRoot = (Join-Path $env:TEMP ("uribo-mcp-mcpb-" + [guid]::NewGuid().ToString("N")))
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRootFull = [System.IO.Path]::GetFullPath($ProjectRoot)
$outputPathFull = [System.IO.Path]::GetFullPath($OutputPath)
$stagingRootFull = [System.IO.Path]::GetFullPath($StagingRoot)

if (-not (Test-Path -LiteralPath (Join-Path $projectRootFull "pyproject.toml") -PathType Leaf)) {
    throw "ProjectRoot must contain pyproject.toml: '$projectRootFull'."
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRootFull "src") -PathType Container)) {
    throw "ProjectRoot must contain src: '$projectRootFull'."
}
if (Test-Path -LiteralPath $stagingRootFull) {
    throw "StagingRoot already exists; refusing to reuse or delete it: '$stagingRootFull'."
}

$mcpb = Get-Command mcpb -ErrorAction SilentlyContinue
$npx = Get-Command npx -ErrorAction SilentlyContinue
if ($null -eq $mcpb -and $null -eq $npx) {
    throw "Neither mcpb nor npx was found. Install Node.js or @anthropic-ai/mcpb."
}

[System.IO.Directory]::CreateDirectory($stagingRootFull) | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRootFull "pyproject.toml") -Destination (Join-Path $stagingRootFull "pyproject.toml")
Copy-Item -LiteralPath (Join-Path $projectRootFull "README.md") -Destination (Join-Path $stagingRootFull "README.md")
Copy-Item -LiteralPath (Join-Path $projectRootFull "src") -Destination (Join-Path $stagingRootFull "src") -Recurse
Copy-Item -LiteralPath (Join-Path $projectRootFull "config.example.toml") -Destination (Join-Path $stagingRootFull "config.example.toml")
Copy-Item -LiteralPath (Join-Path $projectRootFull "packaging\\mcpb\\.mcpbignore") -Destination (Join-Path $stagingRootFull ".mcpbignore")

$manifestPath = Join-Path $projectRootFull "packaging\\mcpb\\manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$pyproject = Get-Content -LiteralPath (Join-Path $projectRootFull "pyproject.toml") -Raw
if ($pyproject -match '(?m)^version\s*=\s*"([^"]+)"') {
    $manifest.version = $Matches[1]
}
[System.IO.File]::WriteAllText(
    (Join-Path $stagingRootFull "manifest.json"),
    (($manifest | ConvertTo-Json -Depth 20) + [Environment]::NewLine),
    [System.Text.UTF8Encoding]::new($false)
)

$outputDirectory = Split-Path -Parent $outputPathFull
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
if (Test-Path -LiteralPath $outputPathFull) {
    throw "OutputPath already exists; refusing to overwrite it: '$outputPathFull'."
}

if ($null -ne $mcpb) {
    & $mcpb.Source pack $stagingRootFull $outputPathFull
} else {
    & $npx.Source --yes "@anthropic-ai/mcpb" pack $stagingRootFull $outputPathFull
}
if ($LASTEXITCODE -ne 0) {
    throw "mcpb pack failed with exit code $LASTEXITCODE. Staging files were kept at '$stagingRootFull'."
}

[pscustomobject]@{
    Bundle = $outputPathFull
    StagingDirectory = $stagingRootFull
    NextStep = "Install the .mcpb in Claude Desktop, then choose a local config.local.toml when prompted."
}
