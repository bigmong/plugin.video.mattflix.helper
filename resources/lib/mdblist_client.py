"""MDBList fetching with local disk caching.

No xbmc imports here -- the caller (default.py) passes in the cache directory path,
so this stays testable outside a Kodi runtime. Caching matters a lot here: without
it, every widget refresh on every box would hit the MDBList API directly, which
will exhaust a free-tier daily quota fast across multiple boxes.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

MDBLIST_BASE_URL = "https://api.mdblist.com"
PAGE_SIZE = 100
MAX_PAGES = 20  # safety cap: 20 * 100 = 2000 items


def _cache_key(list_id_or_url: str) -> str:
    return hashlib.sha1(list_id_or_url.encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Path, list_id_or_url: str) -> Path:
    return cache_dir / f"mdblist_{_cache_key(list_id_or_url)}.json"


def get_list_items(
    list_id_or_url: str,
    api_key: str,
    cache_dir: Path,
    cache_ttl_hours: float = 12.0,
    log=None,
    on_error=None,
) -> list[dict[str, Any]]:
    """Fetch items from an MDBList list, with local disk caching.

    Handles the same sort=random pitfall as the cron script did: MDBList
    re-randomizes independently per paginated request, so sort=random is never
    forwarded -- the full list is fetched in stable order, then shuffled once
    locally after all pages are collected.

    `on_error(message)`, if given, is called for HTTP failures / exceptions --
    used by the caller to surface a Kodi popup notification, not just a log line.
    """
    cache_file = _cache_path(cache_dir, list_id_or_url)
    now = time.time()

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            age_hours = (now - cached.get("fetched_at", 0)) / 3600
            if age_hours < cache_ttl_hours:
                if log:
                    log(f"[MDBList] Using cached data for '{list_id_or_url}' (age {age_hours:.1f}h)")
                return cached.get("items", [])
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # fall through to a fresh fetch

    items = _fetch_from_api(list_id_or_url, api_key, log=log, on_error=on_error)

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"fetched_at": now, "items": items}), encoding="utf-8")
    except OSError as exc:
        if log:
            log(f"[MDBList] Failed to write cache file: {exc}")

    return items


def _fetch_from_api(list_id_or_url: str, api_key: str, log=None, on_error=None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"apikey": api_key}
    randomize_locally = False

    if list_id_or_url.startswith("http://") or list_id_or_url.startswith("https://"):
        parsed = urlparse(list_id_or_url)
        url_params = parse_qs(parsed.query)
        for k, v in url_params.items():
            if not v:
                continue
            k_lower = k.lower()
            if k_lower == "limit":
                continue  # not forwarded -- caller applies its own cap after matching
            elif k_lower in ("sortorder", "order"):
                params["order"] = v[0]
            elif k_lower == "sort" and v[0].strip().lower() == "random":
                randomize_locally = True
            else:
                params[k_lower] = v[0]
        path = parsed.path.strip("/")
        if path.startswith("lists/"):
            path = path[6:]
        list_path = path
    else:
        list_path = list_id_or_url.strip("/")
        if list_path.startswith("lists/"):
            list_path = list_path[6:]

    url = f"{MDBLIST_BASE_URL}/lists/{list_path}/items"
    items: list[dict[str, Any]] = []

    try:
        for page in range(MAX_PAGES):
            page_params = dict(params)
            page_params["limit"] = PAGE_SIZE
            page_params["offset"] = page * PAGE_SIZE

            response = requests.get(url, params=page_params, timeout=15)
            if response.status_code != 200:
                msg = f"MDBList HTTP {response.status_code} fetching '{list_id_or_url}'"
                if log:
                    log(f"[MDBList] {msg}")
                if on_error:
                    on_error(msg)
                break

            data = response.json()
            page_items: list[dict[str, Any]] = []
            if isinstance(data, list):
                page_items = data
            elif isinstance(data, dict):
                page_items = data.get("movies", []) + data.get("shows", [])

            items.extend(page_items)

            has_more = response.headers.get("X-Has-More", "").strip().lower() == "true"
            if not page_items or (not has_more and len(page_items) < PAGE_SIZE):
                break
    except Exception as exc:
        msg = f"Failed to fetch MDBList items for '{list_id_or_url}': {exc}"
        if log:
            log(f"[MDBList] {msg}")
        if on_error:
            on_error(msg)

    if randomize_locally:
        random.shuffle(items)

    return items
