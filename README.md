# zotero-mcp (write-enabled fork)

Model Context Protocol (MCP) server for [Zotero](https://www.zotero.org/) — based on
[kujenga/zotero-mcp](https://github.com/kujenga/zotero-mcp), extended with a full set of
**write tools** for the local Zotero API: collections, items, notes, annotations, saved
searches, files, tags, trash, settings, groups, and bulk/version operations.

The original project covers reading and formatting; this fork keeps all of that and adds
`writes.py`, so an AI assistant can actually *organize* your library: create collections,
move items, edit metadata, attach files, and more.

## Features

In addition to every tool from the upstream project, this fork adds:

**Collections**
- `list_collections` / `list_collection_items`
- `create_collection` / `delete_collection`
- `add_items_to_collection` / `remove_items_from_collection`

**Items**
- `create_item` / `change_item_type`
- `update_item_fields` / `bulk_update_fields`
- `delete_items` (moves to trash)

**Notes & annotations**
- `create_note` / `update_note`
- `create_annotation`

**Saved searches**
- `create_saved_search` / `update_saved_search` / `delete_saved_search` / `run_saved_search`

**Files**
- `upload_file` / `download_file` / `write_fulltext`

**Tags, trash, settings, groups**
- `list_tags` / `delete_tag`
- `list_trash` / `restore_items` / `empty_trash`
- `list_settings` / `set_setting`
- `list_groups`
- `list_changes` (incremental sync via library version)

Write requests are idempotent (each carries a `Zotero-Write-Token`) and use the local
API's own write-key authorization flow.

## Installation

Requires Python 3.11+.

```sh
pipx install git+https://github.com/ozbayenes123-ops/zotero-mcp-local-write.git
# or, without installing:
uvx --from git+https://github.com/ozbayenes123-ops/zotero-mcp-local-write.git zotero-mcp
```

## Configuration

Set `ZOTERO_LOCAL=true` to use the **local** Zotero API (Zotero 7+ with the local
HTTP server enabled). No API key needed:

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

For the web API instead, set `ZOTERO_LIBRARY_ID` and `ZOTERO_API_KEY` (and optionally
`ZOTERO_LIBRARY_TYPE=group`).

## Credits & license

Built on [kujenga/zotero-mcp](https://github.com/kujenga/zotero-mcp) by Aaron Taylor,
licensed under the [MIT License](LICENSE).
