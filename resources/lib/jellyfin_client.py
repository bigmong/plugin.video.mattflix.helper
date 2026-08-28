"""Jellyfin favourites fetching -- the "Want to Watch" queue.

No xbmc imports here: the caller (default.py) resolves special:// and reads the
credentials, then passes them in, so this stays testable outside a Kodi runtime -- same
split as mdblist_client.

Unlike MDBList there is deliberately NO disk cache. This is a per-user queue whose whole
point is reacting to a heart tapped on a phone seconds ago, and it's a LAN call to a
server this box is already talking to constantly. There's no third-party quota to
protect, so caching would only buy staleness.

Endpoint forms below are the post-10.10 ones (verified against a Jellyfin 12.0.0
OpenAPI spec). The older /Users/{userId}/Items and /Users/{uid}/FavoriteItems/{id}
paths are gone in that spec; these work on 10.10+ and 12.
"""

from __future__ import annotations

from typing import Any

import requests

FAVORITES_PATH = "/Items"
USER_DATA_PATH = "/UserItems/{item_id}/UserData"
FAVORITE_PATH = "/UserFavoriteItems/{item_id}"
# Jellyfin's BaseItemKind values for the two things that can be on the list. Episodes are
# never listed directly -- a series goes on the queue, not one of its episodes.
KIND_MOVIE = "Movie"
KIND_SERIES = "Series"


def build_auth_header(token: str, client: str, device: str, device_id: str, version: str) -> str:
    """The spec declares exactly one security scheme -- an apiKey in the Authorization
    header -- so there's no X-Emby-Token fallback to fall back on. Only Token actually
    authenticates; Client/Device/DeviceId/Version are informational, but Jellyfin logs
    them per session, so send something honest rather than blank.
    """
    return (
        f'MediaBrowser Client="{client}", Device="{device}", '
        f'DeviceId="{device_id}", Version="{version}", Token="{token}"'
    )


def _normalize_timestamp(value: str) -> str:
    """Jellyfin timestamps are ISO-8601 UTC, so plain string comparison sorts them
    chronologically -- but only once the fractional-seconds part is a fixed width.

    Not using datetime.fromisoformat on purpose: Jellyfin emits seven fractional digits
    ("2026-08-26T10:30:00.0000000Z"), and fromisoformat accepts three or six, not seven.
    Normalising to a comparable string sidesteps the parse entirely.
    """
    if not value:
        return ""
    ts = value.strip().rstrip("Zz")
    date_part, _, frac = ts.partition(".")
    return f"{date_part}.{(frac or '0').ljust(6, '0')[:6]}"


def sort_favorites(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Date added, newest first.

    Nothing is ordered by play state on purpose: the server-side reconciler takes watched
    items off the list entirely, so anything still here is by definition unstarted and
    there's no second tier to order.

    Jellyfin is asked to sort this way too, but re-sorting locally makes the order a
    property of this code rather than of whichever server version answered.
    """
    return sorted(items, key=lambda i: _normalize_timestamp(i.get("DateCreated") or ""), reverse=True)


def to_match_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reshape Jellyfin items into what match_mdblist_items() already consumes, so the
    existing provider-index matching is reused as-is rather than duplicated.

    Jellyfin capitalises its provider keys ("Imdb"/"Tmdb"/"Tvdb"), which is exactly the
    sort of thing that silently matches nothing, so fold the case rather than trusting it.
    """
    reshaped = []
    for item in items:
        providers = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
        reshaped.append(
            {
                "title": item.get("Name") or "Unknown Title",
                "imdb_id": providers.get("imdb", ""),
                "tmdb_id": providers.get("tmdb", ""),
                "tvdb_id": providers.get("tvdb", ""),
            }
        )
    return reshaped


def _request(
    method: str,
    server_url: str,
    path: str,
    token: str,
    params: dict | None = None,
    client: str = "MattFlix Helper",
    device: str = "Kodi",
    device_id: str = "mattflix-helper",
    version: str = "0.0.0",
    timeout: int = 15,
    log=None,
    on_error=None,
    what: str = "request",
):
    """One place for auth, timeouts and the 401 special-case. Returns the parsed JSON body,
    True for a successful response with no body, or None on any failure (already reported).
    """
    url = f"{server_url.rstrip('/')}{path}"
    headers = {
        "Authorization": build_auth_header(token, client, device, device_id, version),
        "Accept": "application/json",
    }

    def fail(msg: str):
        if log:
            log(f"[Jellyfin] {msg}")
        if on_error:
            on_error(msg)
        return None

    try:
        response = requests.request(method, url, params=params, headers=headers, timeout=timeout)
    except Exception as exc:
        return fail(f"Could not reach Jellyfin ({what}): {exc}")

    if response.status_code == 401:
        # Distinct from other errors: the token stored by plugin.video.jellyfin has expired
        # or been revoked, and signing that addon in again is the fix -- not anything here.
        return fail("Jellyfin rejected the stored login (401). Sign in again in the Jellyfin addon.")

    if response.status_code not in (200, 204):
        return fail(f"Jellyfin HTTP {response.status_code} ({what})")

    if not response.content:
        return True  # a write that succeeded and returned nothing

    try:
        return response.json()
    except ValueError as exc:
        return fail(f"Jellyfin returned unreadable JSON ({what}): {exc}")


def is_favorite(server_url: str, user_id: str, token: str, item_id: str, **kwargs) -> bool | None:
    """Current favourite state for one item. None means "couldn't tell" -- distinct from
    False, because a toggle must not guess a write direction from a failed read.
    """
    data = _request(
        "GET", server_url, USER_DATA_PATH.format(item_id=item_id), token,
        params={"userId": user_id}, what="reading favourite state", **kwargs
    )
    if not isinstance(data, dict):
        return None
    return bool(data.get("IsFavorite"))


def set_favorite(server_url: str, user_id: str, token: str, item_id: str, favorite: bool, **kwargs) -> bool:
    """POST to favourite, DELETE to un-favourite. Returns whether the write succeeded."""
    result = _request(
        "POST" if favorite else "DELETE",
        server_url, FAVORITE_PATH.format(item_id=item_id), token,
        params={"userId": user_id},
        what=f"{'adding' if favorite else 'removing'} favourite",
        **kwargs,
    )
    return result is not None


def get_favorites(
    server_url: str, user_id: str, token: str, item_kind: str, log=None, **kwargs
) -> list[dict[str, Any]] | None:
    """Every item the user has favourited, of one kind (KIND_MOVIE or KIND_SERIES).

    Returns raw Jellyfin items in server order -- call sort_favorites() to apply the queue
    ordering. **None means the request failed**, which is deliberately distinct from an
    empty list (nothing is favourited): a caller caching the result must not stamp a blip
    as "no favourites", and a caller rendering a badge must not treat it as "unfavourite
    everything". `on_error(message)`, passed through to _request, surfaces transport and
    HTTP failures to the user; a merely-empty list is not an error.
    """
    params = {
        "userId": user_id,
        "isFavorite": "true",
        "recursive": "true",
        "includeItemTypes": item_kind,
        # ProviderIds drives the local-library match, DateCreated the sort. UserData is
        # deliberately NOT requested: play state decides nothing here, the reconciler owns
        # what leaves the list.
        "fields": "ProviderIds,DateCreated",
        "sortBy": "DateCreated",
        "sortOrder": "Descending",
        "enableImages": "false",
    }
    data = _request(
        "GET", server_url, FAVORITES_PATH, token, params=params,
        what="fetching favourites", log=log, **kwargs
    )
    if not isinstance(data, dict):
        return None

    items = data.get("Items") or []
    if log:
        log(f"[Jellyfin] {len(items)} favourite {item_kind.lower()}(s) for this user")
    return items
