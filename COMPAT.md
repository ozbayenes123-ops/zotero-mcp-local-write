# COMPAT — zotero sunucusunu diger uygulamalara baglama

Tek komut: `uvrun --project C:\dev\mcp\zotero-mcp-src zotero-mcp`
Bu komut asagidaki uclude aynen boyle kullanilir.

## Claude Desktop
Konum: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "zotero": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "C:\\dev\\mcp\\zotero-mcp-src",
        "zotero-mcp"
      ],
      "env": {
        "ZOTERO_LOCAL": "true"
      }
    }
  }
}
```

## Codex CLI
Konum: `%USERPROFILE%\.codex\config.toml` (satirlari ekleyin)

```toml
# Codex CLI: %USERPROFILE%\.codex\config.toml

[mcp_servers.zotero]
command = "uv"
args = ["run", "--project", "C:\dev\mcp\zotero-mcp-src", "zotero-mcp"]

[mcp_servers.zotero.env]
ZOTERO_LOCAL = "true"

```

## ChatGPT (masaustu/web)
Yerel stdio sunuculari baglanti olarak dogrudan takilmaz; relay/Developer Mode gerekir.
Surum: `cmdc-stack/mcp/hostconfigs/CHATGPT.md`

> Merkezi kurulum/senkron icin: `cmdc-stack` deposu (`scripts/install.ps1`) tumunu
> tek manifestten kaydeder (Hermes / CommandCode dahil).
