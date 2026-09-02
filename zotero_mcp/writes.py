"""Write tools for the local Zotero API (Zotero 10+).

Read tools live in __init__.py and go through pyzotero; writes go through
the local API directly because pyzotero does not know about Zotero 10+'s
local write authorization flow (POST /api/local/authorize + per-write
Zotero-Server-ID header).
"""

import json
import mimetypes
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from zotero_mcp.client import get_attachment_details, get_zotero_client

LOCAL_API = "http://localhost:23119/api"
LIBRARY = "/users/0"
APP_NAME = "Zotero MCP (Command Code)"

_KEY_CACHE = (
    Path(os.getenv("APPDATA", str(Path.home()))) / "zotero-mcp" / "local-write-key.json"
)


class ZoteroWriteError(RuntimeError):
    """A write could not be completed; message is safe to show the user."""


def _request(
    method: str,
    path: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    """Perform one HTTP request against the local API.

    Returns (status, headers, parsed-body). Non-2xx responses are returned
    rather than raised so the write flow can inspect them (401 -> re-auth).
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{LOCAL_API}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            # Non-JSON responses are legitimate: a bare GET /api/ answers
            # with a plain-text hint, and DELETE returns nothing at all.
            if b"application/json" in resp.headers.get("Content-Type", "").encode():
                parsed: Any = json.loads(raw) if raw else None
            else:
                parsed = raw.decode(errors="replace")
            return resp.status, dict(resp.headers), parsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            parsed = raw.decode(errors="replace")
        return e.code, dict(e.headers), parsed


def get_server_id() -> str:
    """Read the running Zotero instance's server ID from a bare GET /api/."""
    status, headers, _ = _request("GET", "/")
    if status != 200:
        raise ZoteroWriteError(
            f"Zotero local API is not reachable (HTTP {status}). "
            "Make sure Zotero is running."
        )
    server_id = headers.get("Zotero-Server-ID", "")
    if not server_id:
        raise ZoteroWriteError(
            "The running Zotero does not report a Zotero-Server-ID, which "
            "means it is older than Zotero 10. Local writes require Zotero 10+."
        )
    return server_id


def _load_cached_key(server_id: str) -> str | None:
    try:
        cached = json.loads(_KEY_CACHE.read_text())
    except (OSError, ValueError):
        return None
    if cached.get("serverId") == server_id and cached.get("key"):
        return cached["key"]
    return None


def _save_cached_key(server_id: str, key: str) -> None:
    try:
        _KEY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_CACHE.write_text(json.dumps({"serverId": server_id, "key": key}))
    except OSError:
        pass  # The key still works for this session; caching is an optimization.


def _authorize(server_id: str) -> tuple[str, bool]:
    """Ask the user for a local write key via Zotero's confirmation dialog."""
    status, _, body = _request(
        "POST",
        "/local/authorize",
        body={"appName": APP_NAME},
        headers={"Zotero-Server-ID": server_id},
    )
    if status == 403:
        raise ZoteroWriteError(
            "The write authorization was denied in Zotero. If you still want "
            "to write, retry the tool call and choose 'Allow' or 'Always "
            "Allow' in Zotero's dialog."
        )
    if status == 429:
        raise ZoteroWriteError(
            "Zotero limits authorization prompts to five per minute. Wait a "
            "moment and retry."
        )
    if status != 200 or not isinstance(body, dict) or not body.get("key"):
        raise ZoteroWriteError(f"Authorization failed (HTTP {status}): {body!r}")
    return body["key"], bool(body.get("remember"))


def write_key(server_id: str) -> str:
    """Get a usable local write key, authorizing through Zotero if needed."""
    if (key := _load_cached_key(server_id)) is not None:
        return key
    key, remember = _authorize(server_id)
    if remember:
        _save_cached_key(server_id, key)
    return key


def write(
    method: str,
    path: str,
    body: Any = None,
    *,
    version: str | int | None = None,
    token: str | None = None,
    _reauthorized: bool = False,
) -> Any:
    """Perform an authenticated write request, re-authorizing once on 401.

    A remembered key is validated on first use; if Zotero has since dropped
    it ("Clear Write Authorizations"), the 401 path asks for a fresh one.
    `token` sends a Zotero-Write-Token for idempotency on retried requests.
    """
    server_id = get_server_id()
    headers = {"Zotero-Server-ID": server_id, "Zotero-API-Key": write_key(server_id)}
    if version is not None:
        headers["If-Unmodified-Since-Version"] = str(version)
    if token is not None:
        headers["Zotero-Write-Token"] = token
    status, _, resp = _request(method, path, body=body, headers=headers)
    if status == 401 and not _reauthorized:
        # Cached key no longer valid -- drop it and authorize fresh.
        try:
            _KEY_CACHE.unlink()
        except OSError:
            pass
        return write(method, path, body, version=version, token=token, _reauthorized=True)
    if status not in (200, 201, 204):
        detail = resp if resp is not None else "(no response body)"
        raise ZoteroWriteError(f"Zotero rejected the write (HTTP {status}): {detail}")
    return resp


def _top_level_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if not item["data"].get("parentItem")]


def _register(mcp: Any) -> None:
    """Register the write tools on the given MCPServer instance."""

    @mcp.tool(
        name="zotero_list_collections",
        description=(
            "List all collections (folders) in the Zotero library as a tree, "
            "with each collection's key, name, parent, and item count. Use "
            "these keys with the other write tools to organize your library."
        ),
    )
    def list_collections() -> str:
        zot = get_zotero_client()
        try:
            collections: Any = zot.collections()
        except Exception as e:  # noqa: BLE001
            return f"Error listing collections: {e!s}"
        if not collections:
            return "No collections in the library yet. Create one with zotero_create_collection."

        by_key = {c["key"]: c for c in collections}
        children: dict[str | None, list[dict[str, Any]]] = {}
        for c in collections:
            parent = c["data"].get("parentCollection") or None
            # Guard against a dangling parent reference (deleted parent).
            if parent and parent not in by_key:
                parent = None
            children.setdefault(parent, []).append(c)

        lines = ["# Collections", ""]

        def render(parent: str | None, depth: int) -> None:
            for c in sorted(children.get(parent, []), key=lambda c: c["data"].get("name", "").lower()):
                indent = "  " * depth
                name = c["data"].get("name", "(unnamed)")
                count = c.get("meta", {}).get("numItems", 0)
                lines.append(f"{indent}- {name} — `{c['key']}` ({count} item{'s' if count != 1 else ''})")
                render(c["key"], depth + 1)

        render(None, 0)
        return "\n".join(lines)

    @mcp.tool(
        name="zotero_list_collection_items",
        description=(
            "List the top-level items (works) in a collection, with key, type, "
            "title, and date. Use to review a collection's contents before "
            "moving items in or out. Child attachments/notes are not listed."
        ),
    )
    def list_collection_items(
        collection_key: Annotated[str, Field(description="Collection key from zotero_list_collections.")],
        limit: Annotated[int | None, Field(description="Maximum items to return (default 50).")] = 50,
    ) -> str:
        zot = get_zotero_client()
        try:
            items: Any = zot.collection_items(collection_key)
        except Exception as e:  # noqa: BLE001
            return f"Error listing collection items: {e!s}"
        works = _top_level_items(items)
        if not works:
            return f"Collection `{collection_key}` has no top-level items."
        lines = [
            f"# Items in Collection `{collection_key}`",
            f"{len(works)} top-level item(s)" + (f" (showing first {limit})" if limit and len(works) > limit else ""),
            "",
        ]
        for item in works[: limit or len(works)]:
            data = item["data"]
            lines.append(
                f"- [{data.get('itemType', '?')}] {data.get('title', 'Untitled')} "
                f"({data.get('date', 'no date')}) — `{item['key']}`"
            )
        return "\n".join(lines)

    @mcp.tool(
        name="zotero_create_collection",
        description=(
            "Create a new collection (folder) in the Zotero library, optionally "
            "nested under a parent collection. Returns the new collection's key."
        ),
    )
    def create_collection(
        name: Annotated[str, Field(description="Name of the new collection.", min_length=1)],
        parent_key: Annotated[str | None, Field(description="Parent collection key for nesting; omit for a top-level collection.")] = None,
    ) -> str:
        body = {"name": name, "parentCollection": parent_key or False}
        try:
            resp = write("POST", f"{LIBRARY}/collections", [body])
        except ZoteroWriteError as e:
            return f"Error creating collection: {e!s}"
        # The multi-object write response is a single object whose result
        # maps are indexed by request position ("0", "1", ...).
        new = resp or {}
        # The local API names the result maps "successful"/"failed" where the
        # Web API uses "success"/"failed"; accept both.
        success = new.get("successful") or new.get("success") or {}
        if first := next(iter(success.values()), None):
            location = f" under `{parent_key}`" if parent_key else ""
            return f"Created collection '{name}'{location} — key: `{first['key']}`"
        if failed := new.get("failed", {}):
            return f"Zotero refused to create the collection: {failed!r}"
        return f"Created collection '{name}'."

    @mcp.tool(
        name="zotero_delete_collection",
        description=(
            "Delete a collection. The collection's items are NOT deleted from "
            "the library unless delete_items=true — by default they just lose "
            "this folder."
        ),
    )
    def delete_collection(
        collection_key: Annotated[str, Field(description="Collection key from zotero_list_collections.")],
        delete_items: Annotated[bool, Field(description="Also permanently delete every top-level item inside the collection (their attachments and notes go with them).")] = False,
    ) -> str:
        try:
            if delete_items:
                zot = get_zotero_client()
                items: Any = zot.collection_items(collection_key)
                works = _top_level_items(items)
                for item in works:
                    write(
                        "DELETE",
                        f"{LIBRARY}/items/{item['key']}",
                        version=item["version"],
                    )
                deleted = len(works)
            else:
                deleted = 0
            status, headers, _ = _request(
                "GET",
                f"{LIBRARY}/collections/{collection_key}",
                headers={"Zotero-Server-ID": get_server_id()},
            )
            if status == 404:
                return f"Collection `{collection_key}` does not exist."
            version = headers.get("Last-Modified-Version")
            write("DELETE", f"{LIBRARY}/collections/{collection_key}", version=version)
        except ZoteroWriteError as e:
            return f"Error deleting collection: {e!s}"
        note = f" and {deleted} item(s) inside it" if delete_items else " (items kept in the library)"
        return f"Deleted collection `{collection_key}`{note}."

    def _patch_collections(item_keys: list[str], collection_key: str, add: bool) -> str:
        verb = "Added" if add else "Removed"
        results: list[str] = []
        for key in item_keys:
            try:
                status, _, item = _request(
                    "GET",
                    f"{LIBRARY}/items/{key}",
                    headers={"Zotero-Server-ID": get_server_id()},
                )
                if status == 404:
                    results.append(f"❌ `{key}`: item not found")
                    continue
                data = item["data"]
                if data.get("itemType") in ("attachment", "note", "annotation"):
                    results.append(f"⚠️ `{key}`: child {data.get('itemType')} — move its parent item instead")
                    continue
                collections = list(data.get("collections", []))
                if add:
                    if collection_key in collections:
                        results.append(f"⚠️ `{key}`: already in this collection")
                        continue
                    collections.append(collection_key)
                else:
                    if collection_key not in collections:
                        results.append(f"⚠️ `{key}`: not in this collection")
                        continue
                    collections.remove(collection_key)
                write(
                    "PATCH",
                    f"{LIBRARY}/items/{key}",
                    {"collections": collections},
                    version=item["version"],
                )
                results.append(f"✅ `{key}`")
            except ZoteroWriteError as e:
                results.append(f"❌ `{key}`: {e!s}")
        return f"{verb} items {'to' if add else 'from'} collection `{collection_key}`:\n" + "\n".join(results)

    @mcp.tool(
        name="zotero_add_items_to_collection",
        description=(
            "Add one or more items to a collection. An item can live in "
            "multiple collections; this never removes it from others. Use "
            "zotero_list_collections to get keys."
        ),
    )
    def add_items_to_collection(
        collection_key: Annotated[str, Field(description="Target collection key.")],
        item_keys: Annotated[list[str], Field(description="Item keys to add, e.g. ['ABCD1234', 'WXYZ5678'].", min_length=1)],
    ) -> str:
        try:
            return _patch_collections(item_keys, collection_key, add=True)
        except ZoteroWriteError as e:
            return f"Error adding items to collection: {e!s}"

    @mcp.tool(
        name="zotero_remove_items_from_collection",
        description=(
            "Remove one or more items from a collection. This only takes them "
            "out of that folder; the items stay in the library and in any "
            "other collections they belong to."
        ),
    )
    def remove_items_from_collection(
        collection_key: Annotated[str, Field(description="Collection key to remove items from.")],
        item_keys: Annotated[list[str], Field(description="Item keys to remove.", min_length=1)],
    ) -> str:
        try:
            return _patch_collections(item_keys, collection_key, add=False)
        except ZoteroWriteError as e:
            return f"Error removing items from collection: {e!s}"

    # Fields an LLM should never patch blindly: identity, provenance of the
    # object graph, and the relation to other objects.
    PROTECTED_FIELDS = frozenset(
        {"key", "version", "itemType", "parentItem", "dateAdded", "dateModified", "relations"}
    )

    @mcp.tool(
        name="zotero_update_item_fields",
        description=(
            "Update fields of an item, e.g. to fix a missing title, date, "
            "publication, or abstract, or to set tags (tags: [{'tag': 'x'}]). "
            "Pass only the fields to change; everything else is untouched. "
            "Use zotero_item_metadata first to see the current values. "
            "Collections are NOT edited here — use the dedicated collection tools."
        ),
    )
    def update_item_fields(
        item_key: Annotated[str, Field(description="Item key to update.")],
        fields: Annotated[dict[str, Any], Field(description="Fields to change as a JSON object, e.g. {'title': 'New Title', 'date': '2024'}.")],
    ) -> str:
        if bad := set(fields) & PROTECTED_FIELDS:
            return f"Error: field(s) {sorted(bad)} cannot be changed with this tool."
        try:
            status, _, item = _request(
                "GET",
                f"{LIBRARY}/items/{item_key}",
                headers={"Zotero-Server-ID": get_server_id()},
            )
            if status == 404:
                return f"Item `{item_key}` not found."
            write("PATCH", f"{LIBRARY}/items/{item_key}", fields, version=item["version"])
            changed = ", ".join(sorted(fields))
            return f"Updated `{item_key}`: {changed}"
        except ZoteroWriteError as e:
            return f"Error updating item: {e!s}"

    @mcp.tool(
        name="zotero_delete_items",
        description=(
            "Move items to Zotero's trash (recoverable via "
            "zotero_restore_items). Note: the local API's true DELETE is "
            "permanent, so this tool marks the items deleted instead; to "
            "purge permanently, list the trash and empty it with "
            "zotero_empty_trash. Use for duplicates. Requires confirm=true."
        ),
    )
    def delete_items(
        item_keys: Annotated[list[str], Field(description="Item keys to move to the trash.", min_length=1)],
        confirm: Annotated[bool, Field(description="Must be true to actually trash these items.")] = False,
    ) -> str:
        if not confirm:
            return "Deletion cancelled: pass confirm=true to delete these items."
        results: list[str] = []
        for key in item_keys:
            try:
                item = _fetch_item(key)
                # The local API's DELETE is a PERMANENT purge (verified
                # against Zotero 10: the item never reaches the trash), so
                # trashing goes through the deleted flag like the UI does.
                write("PATCH", f"{LIBRARY}/items/{key}", {"deleted": True}, version=item["version"])
                results.append(f"✅ `{key}`: {item['data'].get('title', 'Untitled')}")
            except ZoteroWriteError as e:
                results.append(f"❌ `{key}`: {e!s}")
        return "Moved items to the Zotero trash:\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# Full-surface tools: items, notes, annotations, saved searches, files, tags,
# trash, settings, groups, and bulk/version operations.
# ---------------------------------------------------------------------------


def _get(path: str, **params: Any) -> tuple[int, dict[str, str], Any]:
    """GET a local API path with the current server ID attached."""
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    return _request(
        "GET", f"{path}{qs}", headers={"Zotero-Server-ID": get_server_id()}
    )


def _fetch_item(item_key: str) -> dict[str, Any]:
    """Fetch one item, raising a clean error when it does not exist."""
    status, _, item = _get(f"{LIBRARY}/items/{item_key}")
    if status == 404:
        raise ZoteroWriteError(f"Item `{item_key}` not found.")
    if status != 200:
        raise ZoteroWriteError(f"Could not fetch item `{item_key}` (HTTP {status}).")
    return item


def _extract_new_key(resp: Any, what: str) -> str:
    new = resp or {}
    success = new.get("successful") or new.get("success") or {}
    if first := next(iter(success.values()), None):
        return first["key"]
    if failed := new.get("failed", {}):
        raise ZoteroWriteError(f"Zotero refused to create the {what}: {failed!r}")
    raise ZoteroWriteError(f"Zotero returned no key for the new {what}: {new!r}")


def _short_item_line(item: dict[str, Any]) -> str:
    data = item["data"]
    return (
        f"- [{data.get('itemType', '?')}] {data.get('title') or data.get('note', '')
        or data.get('filename', 'Untitled')} ({data.get('date', 'no date')})"
        f" — `{item['key']}`"
    )


def _register_more(mcp: Any) -> None:
    """Register the full-surface tools on the given MCPServer instance."""

    # -- Items ---------------------------------------------------------------

    @mcp.tool(
        name="zotero_create_item",
        description=(
            "Create a new empty item in the library, e.g. a journalArticle, "
            "book, report, or webpage. Pass only the fields you want set "
            "(title, date, creators, ...). Use zotero_item_metadata on an "
            "existing item to see valid field names for its type. Returns "
            "the new item's key."
        ),
    )
    def create_item(
        item_type: Annotated[str, Field(description="Zotero item type, e.g. 'journalArticle', 'book', 'report'.", min_length=1)],
        fields: Annotated[dict[str, Any], Field(description="Fields to set, e.g. {'title': 'T', 'date': '2024'}. Types use 'creators': [{'creatorType': 'author', 'firstName': 'A', 'lastName': 'B'}].")] = None,
        collection_keys: Annotated[list[str] | None, Field(description="Collections to file the item in.")] = None,
        tags: Annotated[list[str] | None, Field(description="Tags for the item.")] = None,
    ) -> str:
        body: dict[str, Any] = {"itemType": item_type}
        body.update(fields or {})
        if collection_keys:
            body["collections"] = collection_keys
        if tags:
            body["tags"] = [{"tag": t} for t in tags]
        try:
            resp = write("POST", f"{LIBRARY}/items", [body])
            return f"Created {item_type} — key: `{_extract_new_key(resp, 'item')}`"
        except ZoteroWriteError as e:
            return f"Error creating item: {e!s}"

    @mcp.tool(
        name="zotero_change_item_type",
        description=(
            "Change an item's type (e.g. journalArticle -> bookSection). Zotero "
            "silently drops fields that do not exist on the new type, so pass "
            "field_map to move values across, e.g. {'publicationTitle': "
            "'bookTitle'}. The old values stay in the new fields when the "
            "names map."
        ),
    )
    def change_item_type(
        item_key: Annotated[str, Field(description="Item key to change.")],
        new_item_type: Annotated[str, Field(description="Target item type.", min_length=1)],
        field_map: Annotated[dict[str, str] | None, Field(description="Old field name -> new field name, moving values that would otherwise be dropped.")] = None,
    ) -> str:
        try:
            item = _fetch_item(item_key)
            data = item["data"]
            patch: dict[str, Any] = {"itemType": new_item_type}
            dropped = []
            for old, new in (field_map or {}).items():
                if old in data:
                    patch[new] = data.pop(old)
            for field in list(data):
                if field not in ("itemType", "key", "version", "collections", "tags",
                                 "relations", "dateAdded", "dateModified", "creators",
                                 "extra", "abstractNote", "url", "rights", "language",
                                 "shortTitle", "archiveID", "callNumber", "libraryCatalog"):
                    dropped.append(field)
            write("PATCH", f"{LIBRARY}/items/{item_key}", patch, version=item["version"])
            note = f" Moved fields: {', '.join(patch.keys() - {'itemType'})}." if len(patch) > 1 else ""
            warn = (
                f" Zotero dropped fields not valid on '{new_item_type}': {', '.join(dropped)}."
                if dropped
                else ""
            )
            return f"Changed `{item_key}` to '{new_item_type}'.{note}{warn}"
        except ZoteroWriteError as e:
            return f"Error changing item type: {e!s}"

    # -- Notes ---------------------------------------------------------------

    @mcp.tool(
        name="zotero_create_note",
        description=(
            "Create a note. With parent_item_key it becomes a child note of "
            "that item; without it, a standalone note. Content is HTML, e.g. "
            "'<p>text</p>'. Returns the new note's key."
        ),
    )
    def create_note(
        note_html: Annotated[str, Field(description="Note content as HTML.", min_length=1)],
        parent_item_key: Annotated[str | None, Field(description="Item key to attach the note to; omit for a standalone note.")] = None,
        tags: Annotated[list[str] | None, Field(description="Tags for the note.")] = None,
    ) -> str:
        body: dict[str, Any] = {"itemType": "note", "note": note_html}
        if parent_item_key:
            body["parentItem"] = parent_item_key
        if tags:
            body["tags"] = [{"tag": t} for t in tags]
        try:
            resp = write("POST", f"{LIBRARY}/items", [body])
            kind = "child note" if parent_item_key else "standalone note"
            return f"Created {kind} — key: `{_extract_new_key(resp, 'note')}`"
        except ZoteroWriteError as e:
            return f"Error creating note: {e!s}"

    @mcp.tool(
        name="zotero_update_note",
        description=(
            "Replace a note's content with new HTML. The old content is "
            "overwritten; fetch it first with zotero_item_metadata if you "
            "need to edit rather than replace."
        ),
    )
    def update_note(
        note_key: Annotated[str, Field(description="Note item key.")],
        note_html: Annotated[str, Field(description="New note content as HTML.", min_length=1)],
    ) -> str:
        try:
            item = _fetch_item(note_key)
            if item["data"].get("itemType") != "note":
                return f"Error: `{note_key}` is a {item['data'].get('itemType')}, not a note."
            write("PATCH", f"{LIBRARY}/items/{note_key}", {"note": note_html}, version=item["version"])
            return f"Updated note `{note_key}`."
        except ZoteroWriteError as e:
            return f"Error updating note: {e!s}"

    # -- Annotations ---------------------------------------------------------

    @mcp.tool(
        name="zotero_create_annotation",
        description=(
            "Create an annotation on a PDF attachment: a highlight, a note "
            "annotation, an image, or ink. annotation_position is the page "
            "placement as a JSON object, e.g. {'pageIndex': 0, 'rects': "
            "[[x1, y1, x2, y2]]} in PDF coordinates. Attach to the "
            "ATTACHMENT key (the PDF), not the parent item. Returns the new "
            "annotation's key."
        ),
    )
    def create_annotation(
        attachment_key: Annotated[str, Field(description="Key of the PDF attachment item.")],
        annotation_type: Annotated[str, Field(description="One of: highlight, note, image, ink.", pattern="^(highlight|note|image|ink)$")],
        annotation_position: Annotated[dict[str, Any], Field(description="Placement as a JSON object, e.g. {'pageIndex': 0, 'rects': [[0.1, 0.2, 0.8, 0.3]]}.")],
        annotation_text: Annotated[str | None, Field(description="Highlighted text (required for highlight).")] = None,
        annotation_comment: Annotated[str | None, Field(description="Your comment on the annotation.")] = None,
        annotation_color: Annotated[str | None, Field(description="Hex color, e.g. '#ffd400'.")] = None,
        page_label: Annotated[str | None, Field(description="Printed page label to show, e.g. '12'.")] = None,
        sort_index: Annotated[str | None, Field(description="Sort position as 'pageIndex_chars', e.g. '00000_00042'.")] = None,
    ) -> str:
        body: dict[str, Any] = {
            "itemType": "annotation",
            "parentItem": attachment_key,
            "annotationType": annotation_type,
            "annotationPosition": json.dumps(annotation_position),
        }
        if annotation_text:
            body["annotationText"] = annotation_text
        if annotation_comment:
            body["annotationComment"] = annotation_comment
        if annotation_color:
            body["annotationColor"] = annotation_color
        if page_label:
            body["annotationPageLabel"] = page_label
        if sort_index:
            body["annotationSortIndex"] = sort_index
        try:
            resp = write("POST", f"{LIBRARY}/items", [body])
            return f"Created {annotation_type} annotation — key: `{_extract_new_key(resp, 'annotation')}`"
        except ZoteroWriteError as e:
            return f"Error creating annotation: {e!s}"

    # -- Saved searches --------------------------------------------------------

    @mcp.tool(
        name="zotero_create_saved_search",
        description=(
            "Create a saved (dynamic) search that Zotero keeps in the left "
            "sidebar and that re-evaluates automatically. Conditions format: "
            "[{'condition': 'title', 'operator': 'contains', 'value': 'x'}]. "
            "Common conditions: title, creator, date, tag, publicationTitle, "
            "abstractNote, itemType; common operators: contains, "
            "doesNotContain, is, isNot, beginsWith, isBefore, isAfter. "
            "Returns the new search's key."
        ),
    )
    def create_saved_search(
        name: Annotated[str, Field(description="Name shown in Zotero's sidebar.", min_length=1)],
        conditions: Annotated[list[dict[str, str]], Field(description="Conditions, e.g. [{'condition': 'title', 'operator': 'contains', 'value': 'kuwait'}].", min_length=1)],
        join_mode: Annotated[str, Field(description="How conditions combine: 'all' (default) or 'any'.", pattern="^(all|any)$")] = "all",
    ) -> str:
        conds = [dict(c) for c in conditions]
        if join_mode == "any" and len(conds) > 1:
            conds[0] = {**conds[0], "joinMode": "any"}
        body = {"name": name, "conditions": conds}
        try:
            resp = write("POST", f"{LIBRARY}/searches", [body])
            return f"Created saved search '{name}' — key: `{_extract_new_key(resp, 'saved search')}`"
        except ZoteroWriteError as e:
            return f"Error creating saved search: {e!s}"

    @mcp.tool(
        name="zotero_update_saved_search",
        description=(
            "Update a saved search's name and/or its conditions. Only the "
            "parts you pass are changed; conditions are replaced wholesale "
            "when provided."
        ),
    )
    def update_saved_search(
        search_key: Annotated[str, Field(description="Saved search key.")],
        name: Annotated[str | None, Field(description="New name.")] = None,
        conditions: Annotated[list[dict[str, str]] | None, Field(description="Replacement conditions, same format as creation.")] = None,
    ) -> str:
        patch: dict[str, Any] = {}
        if name is not None:
            patch["name"] = name
        if conditions is not None:
            patch["conditions"] = [dict(c) for c in conditions]
        if not patch:
            return "Error: nothing to change — pass name and/or conditions."
        try:
            status, headers, _ = _get(f"{LIBRARY}/searches/{search_key}")
            if status == 404:
                return f"Saved search `{search_key}` not found."
            write("PATCH", f"{LIBRARY}/searches/{search_key}", patch, version=headers.get("Last-Modified-Version"))
            return f"Updated saved search `{search_key}`."
        except ZoteroWriteError as e:
            return f"Error updating saved search: {e!s}"

    @mcp.tool(
        name="zotero_delete_saved_search",
        description="Delete a saved search. Its items are untouched (a saved search is just a query).",
    )
    def delete_saved_search(
        search_key: Annotated[str, Field(description="Saved search key.")],
    ) -> str:
        try:
            status, headers, _ = _get(f"{LIBRARY}/searches/{search_key}")
            if status == 404:
                return f"Saved search `{search_key}` not found."
            write("DELETE", f"{LIBRARY}/searches/{search_key}", version=headers.get("Last-Modified-Version"))
            return f"Deleted saved search `{search_key}`."
        except ZoteroWriteError as e:
            return f"Error deleting saved search: {e!s}"

    @mcp.tool(
        name="zotero_run_saved_search",
        description=(
            "Execute a saved search and return the matching top-level items. "
            "This is a LOCAL-API-ONLY capability: the Zotero Web API exposes "
            "saved searches as metadata but cannot run them."
        ),
    )
    def run_saved_search(
        search_key: Annotated[str, Field(description="Saved search key.")],
        limit: Annotated[int | None, Field(description="Maximum items to return (default 50).")] = 50,
    ) -> str:
        status, _, items = _get(f"{LIBRARY}/searches/{search_key}/items", limit=limit or 50)
        if status == 404:
            return f"Saved search `{search_key}` not found."
        if status != 200:
            return f"Error running saved search (HTTP {status})."
        works = _top_level_items(items or [])
        if not works:
            return "The saved search matched no top-level items."
        header = [f"# Saved search `{search_key}`: {len(works)} top-level item(s)", ""]
        return "\n".join(header + [_short_item_line(i) for i in works])

    # -- Files -----------------------------------------------------------------

    def _upload_file(attachment_key: str, filepath: str, filename: str | None) -> str:
        """Run the three-phase upload against the local API."""
        path = Path(filepath)
        if not path.is_file():
            raise ZoteroWriteError(f"File not found: {filepath}")
        import hashlib

        raw = path.read_bytes()
        md5 = hashlib.md5(raw).hexdigest()
        name = filename or path.name
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        mtime = int(path.stat().st_mtime * 1000)
        body = {"md5": md5, "filename": name, "filesize": len(raw), "mtime": mtime}

        # Phase 1: register the upload. If-None-Match: * means the attachment
        # currently has no stored file; Zotero answers 409 if one already
        # exists, which the caller surfaces instead of silently overwriting.
        status, headers, resp = _request(
            "POST",
            f"{LIBRARY}/items/{attachment_key}/file",
            body=body,
            headers={
                "Zotero-Server-ID": get_server_id(),
                "Zotero-API-Key": write_key(get_server_id()),
                "If-None-Match": "*",
            },
        )
        if status == 409:
            raise ZoteroWriteError(
                "This attachment already has a stored file. Delete the old "
                "attachment (zotero_delete_items) and create a new one instead."
            )
        if status not in (200, 201):
            raise ZoteroWriteError(f"Upload registration failed (HTTP {status}): {resp!r}")
        if isinstance(resp, dict) and resp.get("exists"):
            return "Zotero reports the file already matches (exists=1); nothing uploaded."

        upload_url = resp["url"]
        if upload_url.startswith("/"):
            upload_url = f"{LOCAL_API}{upload_url}"

        # Phase 2: PUT the bytes. No auth needed -- the upload key carries it.
        req = urllib.request.Request(upload_url, data=raw, method="POST")
        req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=300):
            pass

        # Phase 3: finalize.
        write(
            "POST",
            f"{LIBRARY}/items/{attachment_key}/file",
            {"upload": resp["uploadKey"]},
            version=None,
        )
        return f"Uploaded '{name}' ({len(raw)} bytes) to attachment `{attachment_key}`."

    @mcp.tool(
        name="zotero_upload_file",
        description=(
            "Attach a local file to an item: creates a stored-file (imported "
            "file) child attachment and uploads the file into it. If you pass "
            "an attachment key instead, the file is uploaded to that existing "
            "attachment (fails if one is already stored). Supported for "
            "imported-file attachments, not linked URLs."
        ),
    )
    def upload_file(
        item_key: Annotated[str, Field(description="Parent item key (creates a new child attachment) or existing attachment key.")],
        filepath: Annotated[str, Field(description="Absolute path of the file on disk.")],
        filename: Annotated[str | None, Field(description="Name to store it under; defaults to the file's name.")] = None,
        title: Annotated[str | None, Field(description="Attachment title; defaults to the file name.")] = None,
    ) -> str:
        try:
            item = _fetch_item(item_key)
            data = item["data"]
            if data.get("itemType") == "attachment":
                if data.get("linkMode") != "imported_file":
                    return f"Error: `{item_key}` is a {data.get('linkMode')} attachment; only imported_file accepts uploads."
                return _upload_file(item_key, filepath, filename or title)
            body = {
                "itemType": "attachment",
                "parentItem": item_key,
                "linkMode": "imported_file",
                "filename": filename or Path(filepath).name,
                "title": title or Path(filepath).name,
                "contentType": mimetypes.guess_type(filename or filepath)[0]
                or "application/octet-stream",
            }
            resp = write("POST", f"{LIBRARY}/items", [body])
            att_key = _extract_new_key(resp, "attachment")
            return _upload_file(att_key, filepath, filename)
        except ZoteroWriteError as e:
            return f"Error uploading file: {e!s}"

    @mcp.tool(
        name="zotero_download_file",
        description=(
            "Download an item's stored attachment file from Zotero to a path "
            "on disk. Works for imported_file attachments (pass the "
            "attachment key, or a parent item to auto-pick its best "
            "attachment). Link-mode attachments have no stored file."
        ),
    )
    def download_file(
        item_key: Annotated[str, Field(description="Attachment key, or parent item key to auto-select its best attachment.")],
        save_path: Annotated[str, Field(description="Absolute path to save the file to.")],
    ) -> str:
        try:
            item = _fetch_item(item_key)
            if item["data"].get("itemType") != "attachment":
                attachment = get_attachment_details(get_zotero_client(), item)
                if attachment is None:
                    return f"Error: `{item_key}` has no suitable attachment to download."
                key = attachment.key
            else:
                key = item_key
            status, _, url = _get(f"{LIBRARY}/items/{key}/file/view/url")
            if status != 200 or not isinstance(url, str):
                return f"Error: no stored file for `{key}` (HTTP {status})."
            if url.strip().lower().startswith("file:"):
                # A file:// URL to Zotero's own storage directory; copy it
                # out so the attachment stays untouched. Zotero writes the
                # drive colon as '|' (file|///C|/...) and url2pathname
                # rejects that, so normalize by hand: put the colon back,
                # unquote, and drop the scheme's leading slashes.
                rest = urllib.parse.unquote(url.strip().split(":", 1)[1])
                rest = rest.replace("|", ":")
                drive = re.match(r"^/([A-Za-z]:/.*)$", rest)
                if drive:
                    src = Path(drive.group(1))
                elif rest.startswith("//"):
                    src = Path(rest.lstrip("/"))
                else:
                    src = Path(urllib.request.url2pathname(rest))
                if not src.is_file():
                    return f"Error: Zotero points at {src} but the file is missing on disk."
                out = Path(save_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out)
                return f"Saved attachment `{key}` to {out} ({out.stat().st_size} bytes)."
            # A http(s) URL: stream it down.
            with urllib.request.urlopen(url, timeout=300) as resp:
                out = Path(save_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(resp.read())
            return f"Saved attachment `{key}` to {out} ({out.stat().st_size} bytes)."
        except ZoteroWriteError as e:
            return f"Error downloading file: {e!s}"

    @mcp.tool(
        name="zotero_write_fulltext",
        description=(
            "Write (or replace) an attachment's indexed full-text content so "
            "zotero_item_fulltext and full-text search can read it. Use for "
            "attachments whose text Zotero could not extract (scans, exotic "
            "formats) — pass the text you extracted yourself."
        ),
    )
    def write_fulltext(
        item_key: Annotated[str, Field(description="Attachment key, or parent item key to auto-select its best attachment.")],
        content: Annotated[str, Field(description="The full text content to index.", min_length=1)],
        indexed_pages: Annotated[int | None, Field(description="Number of pages indexed; estimated from form feeds if omitted.")] = None,
    ) -> str:
        try:
            item = _fetch_item(item_key)
            if item["data"].get("itemType") != "attachment":
                attachment = get_attachment_details(get_zotero_client(), item)
                if attachment is None:
                    return f"Error: `{item_key}` has no suitable attachment."
                key = attachment.key
                if key != item_key:
                    item = _fetch_item(key)
            else:
                key = item_key
            body = {
                "content": content,
                "indexedChars": len(content),
                "indexedPages": indexed_pages or max(content.count("\x0c"), 1),
            }
            status, _, resp = _request(
                "PUT",
                f"{LIBRARY}/items/{key}/fulltext",
                body=body,
                headers={
                    "Zotero-Server-ID": get_server_id(),
                    "Zotero-API-Key": write_key(get_server_id()),
                    "If-Unmodified-Since-Version": str(item["version"]),
                },
            )
            if status not in (200, 204):
                return f"Error writing full text (HTTP {status}): {resp!r}"
            return f"Indexed {body['indexedChars']} characters of full text for attachment `{key}`."
        except ZoteroWriteError as e:
            return f"Error writing full text: {e!s}"

    # -- Tags ------------------------------------------------------------------

    @mcp.tool(
        name="zotero_list_tags",
        description=(
            "List tags used across the library, with how many items carry "
            "each. Use 'prefix' to list only tags starting with a string."
        ),
    )
    def list_tags(
        prefix: Annotated[str | None, Field(description="Only tags starting with this string.")] = None,
        limit: Annotated[int | None, Field(description="Maximum tags to return (default 100).")] = 100,
    ) -> str:
        status, _, tags = _get(f"{LIBRARY}/tags", limit=limit or 100, **({"tagPrefix": prefix} if prefix else {}))
        if status != 200:
            return f"Error listing tags (HTTP {status})."
        if not tags:
            return "No tags in the library."
        lines = [f"# Tags ({len(tags)})", ""]
        for t in tags:
            count = t.get("meta", {}).get("numItems")
            count_s = f" ({count})" if count is not None else ""
            lines.append(f"- {t['tag']}{count_s}")
        return "\n".join(lines)

    @mcp.tool(
        name="zotero_delete_tag",
        description=(
            "Delete a tag from every item that carries it. Implemented by "
            "rewriting each affected item's tags (the local API has no "
            "direct tag-delete endpoint); a few hundred items is fine, "
            "tens of thousands is not."
        ),
    )
    def delete_tag(tag: Annotated[str, Field(description="Exact tag text to delete.", min_length=1)]) -> str:
        try:
            status, _, items = _get(f"{LIBRARY}/items", tag=tag)
            if status != 200:
                return f"Error finding items with the tag (HTTP {status})."
            changed = 0
            for item in items or []:
                data = item["data"]
                old_tags = data.get("tags", [])
                new_tags = [t for t in old_tags if t.get("tag") != tag]
                if len(new_tags) == len(old_tags):
                    continue
                write(
                    "PATCH",
                    f"{LIBRARY}/items/{item['key']}",
                    {"tags": new_tags},
                    version=item["version"],
                    token=str(uuid.uuid4()),
                )
                changed += 1
            return f"Deleted tag '{tag}' from {changed} item(s)."
        except ZoteroWriteError as e:
            return f"Error deleting tag: {e!s}"

    # -- Trash -----------------------------------------------------------------

    @mcp.tool(
        name="zotero_list_trash",
        description="List items currently in Zotero's trash (deleted but recoverable).",
    )
    def list_trash(
        limit: Annotated[int | None, Field(description="Maximum items to return (default 50).")] = 50,
    ) -> str:
        status, _, items = _get(f"{LIBRARY}/items/trash", limit=limit or 50)
        if status != 200:
            return f"Error listing trash (HTTP {status})."
        if not items:
            return "Trash is empty."
        header = [f"# Trash: {len(items)} item(s)", ""]
        return "\n".join(header + [_short_item_line(i) for i in items])

    @mcp.tool(
        name="zotero_restore_items",
        description="Restore items from the trash back into the library.",
    )
    def restore_items(
        item_keys: Annotated[list[str], Field(description="Trashed item keys to restore.", min_length=1)],
    ) -> str:
        results: list[str] = []
        for key in item_keys:
            try:
                item = _fetch_item(key)
                write("PATCH", f"{LIBRARY}/items/{key}", {"deleted": False}, version=item["version"])
                results.append(f"✅ `{key}`: {item['data'].get('title', 'Untitled')}")
            except ZoteroWriteError as e:
                results.append(f"❌ `{key}`: {e!s}")
        return "Restored items:\n" + "\n".join(results)

    @mcp.tool(
        name="zotero_empty_trash",
        description=(
            "Permanently delete EVERY item in the trash. This cannot be "
            "undone. Requires confirm=true."
        ),
    )
    def empty_trash(
        confirm: Annotated[bool, Field(description="Must be true to permanently delete everything in the trash.")] = False,
    ) -> str:
        if not confirm:
            return "Cancelled: pass confirm=true to permanently empty the trash."
        try:
            status, _, items = _get(f"{LIBRARY}/items/trash", limit=100)
            if status != 200 or not items:
                return "Trash is empty."
            purged = 0
            for item in items:
                write("DELETE", f"{LIBRARY}/items/{item['key']}", version=item["version"])
                purged += 1
            return f"Permanently deleted {purged} item(s) from the trash."
        except ZoteroWriteError as e:
            return f"Error emptying trash: {e!s}"

    # -- Settings and groups -----------------------------------------------------

    @mcp.tool(
        name="zotero_list_settings",
        description="List all library settings stored in Zotero, as JSON.",
    )
    def list_settings() -> str:
        status, _, settings = _get(f"{LIBRARY}/settings")
        if status != 200:
            return f"Error listing settings (HTTP {status})."
        return json.dumps(settings, ensure_ascii=False, indent=2)

    @mcp.tool(
        name="zotero_set_setting",
        description=(
            "Set one library setting (a key under /settings), e.g. syncing "
            "metadata used by plugins. Overwrites the previous value."
        ),
    )
    def set_setting(
        setting: Annotated[str, Field(description="Setting name.", min_length=1)],
        value: Annotated[Any, Field(description="New value (arbitrary JSON).")],
    ) -> str:
        try:
            status, headers, _ = _get(f"{LIBRARY}/settings/{setting}")
            version = headers.get("Last-Modified-Version")
            write("PUT", f"{LIBRARY}/settings/{setting}", value, version=version)
            return f"Setting '{setting}' written."
        except ZoteroWriteError as e:
            return f"Error writing setting: {e!s}"

    @mcp.tool(
        name="zotero_list_groups",
        description=(
            "List the Zotero group libraries the logged-in user belongs to "
            "(read-only metadata; group CONTENTS are maintained by the "
            "server, not locally)."
        ),
    )
    def list_groups() -> str:
        status, _, groups = _get(f"{LIBRARY}/groups")
        if status != 200:
            return f"Error listing groups (HTTP {status})."
        if not groups:
            return "The user belongs to no group libraries."
        lines = [f"# Group libraries ({len(groups)})", ""]
        for g in groups:
            lines.append(f"- {g.get('data', {}).get('name', '?')} — id {g.get('id')} (version {g.get('version')})")
        return "\n".join(lines)

    # -- Bulk / version operations ----------------------------------------------

    @mcp.tool(
        name="zotero_list_changes",
        description=(
            "Track what changed since a version: returns key -> version for "
            "every item, collection, or search modified since the given "
            "library version. Use since=0 (or omit) for everything, and feed "
            "the returned highest version back next time."
        ),
    )
    def list_changes(
        object_type: Annotated[str, Field(description="What to track: 'items', 'collections', or 'searches'.", pattern="^(items|collections|searches)$")] = "items",
        since: Annotated[int, Field(description="Library version to diff from (0 = all).")] = 0,
    ) -> str:
        status, _, versions = _get(f"{LIBRARY}/{object_type}", format="versions", since=since)
        if status != 200:
            return f"Error listing changes (HTTP {status})."
        if not versions:
            return f"No changes to {object_type} since version {since}."
        highest = max(versions.values())
        lines = [f"# {object_type.capitalize()} changed since version {since}: {len(versions)}", f"(highest version: {highest} — pass this as `since` next time)", ""]
        lines += [f"- `{key}` (v{v})" for key, v in sorted(versions.items())]
        return "\n".join(lines)

    @mcp.tool(
        name="zotero_bulk_update_fields",
        description=(
            "Apply a field patch to MANY items in one call (e.g. fixing a "
            "hundred titles). Each entry: {'key': 'ABCD1234', 'fields': "
            "{...}}. Same semantics as zotero_update_item_fields per item; "
            "each request carries its own Zotero-Write-Token for idempotency."
        ),
    )
    def bulk_update_fields(
        updates: Annotated[list[dict[str, Any]], Field(description="Entries of {'key': item key, 'fields': fields to change}.", min_length=1)],
    ) -> str:
        results: list[str] = []
        for entry in updates:
            key = entry.get("key", "")
            fields = entry.get("fields") or {}
            if not key or not fields:
                results.append(f"❌ {key or '(no key)'}: entry needs 'key' and 'fields'")
                continue
            try:
                item = _fetch_item(key)
                write(
                    "PATCH",
                    f"{LIBRARY}/items/{key}",
                    fields,
                    version=item["version"],
                    token=str(uuid.uuid4()),
                )
                results.append(f"✅ `{key}`")
            except ZoteroWriteError as e:
                results.append(f"❌ `{key}`: {e!s}")
        return f"Bulk update finished ({sum(1 for r in results if r.startswith('✅'))}/{len(updates)} ok):\n" + "\n".join(results)

