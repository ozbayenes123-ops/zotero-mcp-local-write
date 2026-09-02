# zotero-mcp-local-write

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Model Context Protocol (MCP) server for [Zotero](https://www.zotero.org/) — based on
[kujenga/zotero-mcp](https://github.com/kujenga/zotero-mcp), extended with a full set of
**write tools** so an AI assistant can actually *organize* your library, not just read it.

The upstream project covers searching and formatting. This fork keeps all of that and adds
`writes.py` (~1,200 lines): collections, items, notes, annotations, saved searches, file
uploads, tags, trash, settings, groups, and bulk/version-aware operations — all over the
**local Zotero API**, so nothing leaves your machine.

- **33 tools total**: 3 read (from upstream) + 30 write (new in this fork)
- Works with the **local API** — no API key, no cloud sync required
- **Idempotent writes** (each request carries a `Zotero-Write-Token`)
- Version-aware write-key authorization flow for the local API

## Requirements

- [Zotero 7+](https://www.zotero.org/download/) with the local HTTP server enabled
  (Settings → Advanced → check "Allow other applications on this computer to communicate with Zotero")
- Python 3.11+ (only if installing from source)
- An MCP client: Claude Desktop, Claude Code, Cursor, or any MCP-compatible assistant

## Installation

### Option 1 — Run without installing (recommended)

```sh
uvx --from git+https://github.com/ozbayenes123-ops/zotero-mcp-local-write.git zotero-mcp
```

### Option 2 — Install permanently

```sh
pipx install git+https://github.com/ozbayenes123-ops/zotero-mcp-local-write.git
```

### Option 3 — From source (development)

```sh
git clone https://github.com/ozbayenes123-ops/zotero-mcp-local-write.git
cd zotero-mcp-local-write
uv run zotero-mcp
```

## Configuration

### Local API (recommended, no API key needed)

Set `ZOTERO_LOCAL=true` to use Zotero's local HTTP server. The server authenticates
itself against Zotero's local API and requests a write key on first write.

**Generic `mcp.json` / Claude Code:**

```json
{
  "mcpServers": {
    "zotero": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ozbayenes123-ops/zotero-mcp-local-write.git", "zotero-mcp"],
      "env": { "ZOTERO_LOCAL": "true" }
    }
  }
}
```

**Claude Desktop (`claude_desktop_config.json`):**

```json
{
  "mcpServers": {
    "zotero": {
      "command": "zotero-mcp",
      "args": [],
      "env": { "ZOTERO_LOCAL": "true" }
    }
  }
}
```

### Web API (optional)

If you prefer the Zotero Web API (writes go to zotero.org), set:

| Variable | Required | Description |
|---|---|---|
| `ZOTERO_LIBRARY_ID` | yes (web mode) | Your user or group library ID |
| `ZOTERO_LIBRARY_TYPE` | no (default `user`) | `user` or `group` |
| `ZOTERO_API_KEY` | yes (web mode) | API key from zotero.org settings |
| `ZOTERO_LOCAL` | no | Set `true` to force local API mode |

## Tools

### Reading (upstream)

| Tool | Description |
|---|---|
| `zotero_search_items` | Search the library by text query |
| `zotero_item_metadata` | Full metadata of one item, grouped and formatted |
| `zotero_item_fulltext` | Indexed full text of an item's attachment (e.g. PDF) |

### Writing (this fork)

**Collections**

| Tool | Description |
|---|---|
| `zotero_list_collections` | List all collections as a tree |
| `zotero_list_collection_items` | List top-level items in a collection |
| `zotero_create_collection` | Create a collection (optionally nested) |
| `zotero_delete_collection` | Delete a collection (items stay in the library) |
| `zotero_add_items_to_collection` | Add items to a collection |
| `zotero_remove_items_from_collection` | Remove items from a collection |

**Items**

| Tool | Description |
|---|---|
| `zotero_create_item` | Create a new item (journalArticle, book, …) |
| `zotero_change_item_type` | Change item type (e.g. journalArticle → bookSection) |
| `zotero_update_item_fields` | Update fields of one item |
| `zotero_bulk_update_fields` | Field patch across many items in one call |
| `zotero_delete_items` | Move items to trash (recoverable) |

**Notes & annotations**

| Tool | Description |
|---|---|
| `zotero_create_note` | Create a standalone or child note (HTML) |
| `zotero_update_note` | Replace a note's content |
| `zotero_create_annotation` | Highlight / note / image / ink annotation on a PDF |

**Saved searches**

| Tool | Description |
|---|---|
| `zotero_create_saved_search` | Create a dynamic saved search |
| `zotero_update_saved_search` | Update name and/or conditions |
| `zotero_delete_saved_search` | Delete a saved search |
| `zotero_run_saved_search` | Execute a saved search |

**Files**

| Tool | Description |
|---|---|
| `zotero_upload_file` | Attach a local file (stored-file attachment) |
| `zotero_download_file` | Download a stored attachment to disk |
| `zotero_write_fulltext` | Write indexed full text so `zotero_item_fulltext` can find it |

**Tags, trash, settings, groups, sync**

| Tool | Description |
|---|---|
| `zotero_list_tags` | List all tags with item counts |
| `zotero_delete_tag` | Delete a tag from every item |
| `zotero_list_trash` | List trashed items |
| `zotero_restore_items` | Restore items from trash |
| `zotero_empty_trash` | Permanently delete everything in trash |
| `zotero_list_settings` | List library settings |
| `zotero_set_setting` | Set one library setting |
| `zotero_list_groups` | List group libraries the user belongs to |
| `zotero_list_changes` | Incremental sync — what changed since a library version |

## Example workflow

"Organize my machine-learning papers":

1. `zotero_search_items` — find the papers
2. `zotero_create_collection` — create "Machine Learning"
3. `zotero_add_items_to_collection` — move them in
4. `zotero_create_note` — attach a summary note to each
5. `zotero_create_annotation` — highlight the key claim in the PDF
6. `zotero_list_changes` — confirm what was modified since your last sync

## How local writes work

The Zotero 7 local API requires a **write key** separate from normal read access.
On the first write, the server performs Zotero's local authorization flow, caches the
key locally, and re-uses it; every write request additionally carries a
`Zotero-Write-Token` (a UUID) so retried requests cannot duplicate changes.

Deletes are soft by default: `zotero_delete_items` moves to trash, and
`zotero_restore_items` recovers. Only `zotero_empty_trash` is permanent.

## Troubleshooting

- **"Connection refused" / empty results** — make sure Zotero is running and the local
  API is enabled in Zotero's settings.
- **Permission errors on write** — Zotero shows an authorization prompt the first time;
  accept it, or check the library is not set to read-only.
- **Transport** — use `--transport stdio` (default) for most clients; `sse` is available
  for web-based clients: `zotero-mcp --transport sse`.

## Credits & license

Built on [kujenga/zotero-mcp](https://github.com/kujenga/zotero-mcp) by Aaron Taylor
(read layer: client.py, cli.py and the formatting/search core in `__init__.py`),
licensed under the [MIT License](LICENSE), which is preserved here.

The write layer (`writes.py`) and all local-API integration were added in this fork.
