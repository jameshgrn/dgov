"""Zotero API client for the knowledge layer."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError


@dataclass
class ZoteroItem:
    key: str
    title: str
    authors: list[str]
    year: str
    abstract: str
    item_type: str
    tags: list[str]
    collections: list[str]
    pdf_path: str | None = None


def _parse_item(raw: dict) -> ZoteroItem:
    """Parse a Zotero API JSON item into a ZoteroItem."""
    data = raw.get("data", raw)
    creators = data.get("creators", [])
    authors = []
    for c in creators:
        last = c.get("lastName", "")
        first = c.get("firstName", "")
        if last and first:
            authors.append(f"{last}, {first}")
        elif last:
            authors.append(last)
        elif c.get("name"):
            authors.append(c["name"])
    tags = [t.get("tag", "") for t in data.get("tags", [])]
    collections = data.get("collections", [])
    return ZoteroItem(
        key=data.get("key", ""),
        title=data.get("title", ""),
        authors=authors,
        year=data.get("date", "")[:4] if data.get("date") else "",
        abstract=data.get("abstractNote", ""),
        item_type=data.get("itemType", ""),
        tags=tags,
        collections=collections,
    )


class ZoteroClient:
    def __init__(
        self,
        library_id: str,
        api_key: str | None = None,
        library_type: str = "user",
    ) -> None:
        self._library_id = library_id
        self._api_key = api_key
        if library_type not in ("user", "group"):
            raise ValueError(f"library_type must be 'user' or 'group', got {library_type!r}")
        self._library_type = library_type
        encoded_id = urllib.parse.quote(str(library_id), safe="")
        self._base_url = f"https://api.zotero.org/{library_type}s/{encoded_id}"

    def _request(self, path: str, params: dict[str, str] | None = None) -> list[dict] | dict:
        """Send a GET request to the Zotero API and return parsed JSON."""
        url = f"{self._base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        headers = {"Zotero-API-Version": "3"}
        if self._api_key:
            headers["Zotero-API-Key"] = self._api_key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def get_item(self, key: str) -> ZoteroItem | None:
        """Fetch a single item by key. Returns None if not found."""
        try:
            raw = self._request(f"/items/{key}")
        except HTTPError as e:
            if e.code == 404:
                return None
            raise
        if isinstance(raw, dict):
            return _parse_item(raw)
        return None

    def search(self, query: str, limit: int = 10) -> list[ZoteroItem]:
        """Search items by free-text query."""
        raw = self._request("/items", params={"q": query, "limit": str(limit)})
        if isinstance(raw, list):
            return [_parse_item(item) for item in raw]
        return []

    def get_collection(self, collection_key: str) -> list[ZoteroItem]:
        """Get all items in a collection."""
        raw = self._request(f"/collections/{collection_key}/items")
        if isinstance(raw, list):
            return [_parse_item(item) for item in raw]
        return []

    def get_by_tag(self, tag: str) -> list[ZoteroItem]:
        """Get all items with a specific tag."""
        raw = self._request("/items", params={"tag": tag})
        if isinstance(raw, list):
            return [_parse_item(item) for item in raw]
        return []

    def get_all_items(self, limit: int = 100) -> list[ZoteroItem]:
        """Get all items up to limit."""
        raw = self._request("/items", params={"limit": str(limit)})
        if isinstance(raw, list):
            return [_parse_item(item) for item in raw]
        return []
