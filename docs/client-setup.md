# Desktop client setup

`uriboMCP` is a local stdio MCP server. It is started with:

```powershell
uv --directory <PROJECT_ROOT> run uribo-mcp serve
```

Every client must receive an absolute `URIBO_MCP_CONFIG` path. Do not put BEEF+, university, Google, or mail credentials in an MCP client configuration or an MCPB bundle.

## Prepare a local configuration

From the project root, create the untracked local file once. This command never overwrites an existing file.

```powershell
Copy-Item -LiteralPath .\config.example.toml -Destination .\config.local.toml -ErrorAction Stop
```

Edit `config.local.toml` only as needed. Keep secrets in the configured credential store rather than in a desktop-client configuration.

Generate client-specific materials with argument arrays and absolute paths. This is safe for Japanese and space-containing paths, such as an OneDrive project directory, and does not change Claude/Codex settings.

```powershell
.\scripts\New-ClientConfigs.ps1 `
  -ProjectRoot (Get-Location).Path `
  -ConfigPath (Join-Path (Get-Location).Path 'config.local.toml') `
  -OutputDirectory .\packaging\generated
```

Run the read-only doctor before registration:

```powershell
.\scripts\Test-UriboMcpSetup.ps1 `
  -ProjectRoot (Get-Location).Path `
  -ConfigPath (Join-Path (Get-Location).Path 'config.local.toml') `
  -GeneratedDirectory .\packaging\generated
```

Add `-RunSmokeTest` only after dependencies are installed; it runs `uribo-mcp --help` and still does not modify a client configuration.

## Claude Code on Windows

Use a user-scoped server so it is available in every local project. Review the generated script before running it:

```powershell
.\packaging\generated\register-claude-code-user.ps1
claude mcp get uribo-mcp
```

The equivalent command is below. The `--` delimiter is required: options before it configure Claude Code, and the remaining values start the MCP server.

```powershell
claude mcp add --transport stdio uribo-mcp --scope user `
  --env "URIBO_MCP_CONFIG=C:\absolute\path\to\config.local.toml" -- `
  uv --directory "C:\absolute\path\to\uriboMCP" run uribo-mcp serve
```

This command changes `~/.claude.json` only when you run it. It is not run by any repository script. `uv.exe` is an executable, so unlike an `npx`-based server it does not need a `cmd /c` wrapper on Windows.

## Claude Desktop

Claude Desktop's legacy JSON configuration is useful for local development. Merge the `uribo-mcp` entry from generated `claude_desktop_config.json` into the existing `mcpServers` object in:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

An unexpanded example is available at `packaging/claude-desktop/claude_desktop_config.example.json`. Preserve existing servers and replace the example paths with the generated absolute values. Restart Claude Desktop after saving. No project script opens, edits, or replaces this user file.

For one-click distribution, use the MCPB path below instead.

## Codex desktop app and Codex CLI

The ChatGPT desktop app, Codex CLI, and Codex IDE extension share local MCP configuration on the same host. Choose either route:

1. In the desktop app, open **Settings → MCP servers → Add server**, choose **STDIO**, and copy the generated command/arguments/environment values. Restart the app after saving.
2. Merge `packaging/generated/codex.toml.fragment` into `%USERPROFILE%\.codex\config.toml`.
3. Review and run `packaging/generated/register-codex-user.ps1`; then confirm with `codex mcp list`.

The direct CLI equivalent is:

```powershell
codex mcp add uribo-mcp `
  --env "URIBO_MCP_CONFIG=C:\absolute\path\to\config.local.toml" -- `
  uv --directory "C:\absolute\path\to\uriboMCP" run uribo-mcp serve
```

The TOML form uses a `[mcp_servers.uribo-mcp]` table, `args` as an array, and an `[mcp_servers.uribo-mcp.env]` table. This avoids shell parsing of paths containing spaces.

## Claude Desktop MCPB bundle

The bundle is a Windows-only `uv` MCPB. It contains application source, `pyproject.toml`, the non-secret example configuration, and the MCPB manifest. It deliberately excludes `config.local.toml`, credentials, local databases, exports, and caches.

The build script uses an installed `mcpb` command when available. Otherwise it
uses `npx` to run the official package temporarily, without a global install:

```powershell
npx --yes @anthropic-ai/mcpb validate .\packaging\mcpb\manifest.json
```

Build without overwriting an existing bundle or deleting a staging directory:

```powershell
.\scripts\Build-Mcpb.ps1 `
  -ProjectRoot (Get-Location).Path `
  -OutputPath .\packaging\artifacts\uribo-mcp.mcpb
```

The build script runs `mcpb pack <staging-directory> <output-file>`, which validates the manifest before packaging. Install the resulting `.mcpb` via Claude Desktop **Settings → Extensions → Advanced settings → Install Extension…**, then select the local `config.local.toml` when prompted. The MCPB manifest contains only a file-path setting; it contains no username, password, token, or cookie.

## Sources

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp.md)
- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Claude Desktop MCPB setup](https://claude.com/docs/connectors/building/mcpb)
- [MCPB manifest specification](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md)
