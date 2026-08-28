"""Pure config and date-logic module -- no xbmc/xbmcgui/xbmcplugin imports here on purpose,
so this can be unit tested outside a Kodi runtime. All Kodi-specific glue lives in default.py.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse


def slugify(label: str) -> str:
    """Auto-derives a widget key from a label: lowercase, spaces/dashes -> underscores,
    other punctuation stripped. 'Marvel Cinematic Universe: The Sacred Timeline' ->
    'marvel_cinematic_universe_the_sacred_timeline'."""
    slug = label.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "_", slug)
    return slug.strip("_")


def detect_source(url: str) -> str:
    """Auto-detects how to resolve a list from its URL alone.

    "local://<field>/<value>" (e.g. "local://actor/Nicolas Cage", "local://genre/Horror")
    queries the local Kodi library directly, and "library://<key>" runs one of the built-in
    LIBRARY_CONFIGS queries. "jellyfin://<collection>" resolves against the Jellyfin server
    the box is already logged into. Anything on mdblist.com uses the MDBList API.
    imdb.com/themoviedb.org are recognized but not yet implemented -- reserved so those can
    be dropped in as list sources later without changing this schema again.

    Anything returning a source that render_list() doesn't implement yet renders as an empty
    list rather than erroring, which is what makes a reserved scheme safe to ship early.
    """
    url_lower = url.lower()
    if url_lower.startswith("local://"):
        return "local"
    if url_lower.startswith("library://"):
        return "library"
    if url_lower.startswith("jellyfin://"):
        return "jellyfin"  # the "Want to Watch" queue -- see LIST_CONFIGS below
    if "mdblist.com" in url_lower:
        return "mdblist"
    if "imdb.com" in url_lower:
        return "imdb"  # reserved, not yet implemented
    if "themoviedb.org" in url_lower or "tmdb.org" in url_lower:
        return "tmdb"  # reserved, not yet implemented
    return "mdblist"  # sensible fallback -- it's the only fully-implemented remote source today


def parse_local_url(url: str) -> tuple[str, str]:
    """Parses "local://<field>/<value>" -> (field, value). E.g. "local://actor/Nicolas Cage"
    -> ("actor", "Nicolas Cage")."""
    body = url[len("local://"):]
    field, _, value = body.partition("/")
    return field, value


# -----------------------------------------------------------------------------
# Built-in library lists -- the smart playlists, expressed as JSON-RPC queries so they run
# against the local Kodi library directly instead of via a .xsp file.
#
# Each "query" maps 1:1 onto VideoLibrary.GetMovies / GetTVShows:
#   filter -> List.Filter   sort -> List.Sort   limit -> limits.end
# A limit of None means no cap, matching a .xsp with no <limit>. That is load-bearing, not
# an oversight: the skin's shelf shows its own handful of items and then a "More..." entry
# that opens the SAME url as a full listing, so a cap here would silently shorten a browse
# of the whole library. Set a limit only where the list has its own editorial cap.
#
# "group_sets" collapses collections the way Kodi's own Movies node does. Leave "limit" at
# None on any entry that sets it: JSON-RPC applies "limits" before the grouping happens, so
# a capped fetch would both shrink the list (100 movies might collapse to 88 entries) and
# show partial collections containing only the members that made the cut. default.py caps
# grouped lists in Python after collapsing instead.
#
# Every entry can be switched off individually from the addon's settings (a
# "library_<key>" boolean); a disabled list disappears from the root menu and renders empty.
# -----------------------------------------------------------------------------
LIBRARY_CONFIGS: list[dict[str, Any]] = [
    {
        "label": "All Movies", "type": "movies", "url": "library://all_movies",
        "query": {"group_sets": True, "sort": {"method": "title", "order": "ascending"}, "limit": None},
    },
    {
        "label": "All Shows", "type": "shows", "url": "library://all_shows",
        "query": {"sort": {"method": "title", "order": "ascending"}, "limit": None},
    },
    {
        "label": "New Movies", "type": "movies", "url": "library://new_movies",
        "query": {"group_sets": True, "sort": {"method": "dateadded", "order": "descending"}, "limit": None},
    },
    {
        "label": "New Shows", "type": "shows", "url": "library://new_shows",
        "query": {
            "filter": {"field": "numwatched", "operator": "is", "value": "0"},
            "sort": {"method": "dateadded", "order": "descending"}, "limit": 50,
        },
    },
    {
        "label": "Random Movies", "type": "movies", "url": "library://random_movies",
        "query": {
            "filter": {"field": "lastplayed", "operator": "notinthelast", "value": "24 months"},
            "sort": {"method": "random", "order": "ascending"}, "limit": 10,
        },
    },
    {
        "label": "Random Shows", "type": "shows", "url": "library://random_shows",
        "query": {
            "filter": {"and": [
                {"field": "numwatched", "operator": "is", "value": "0"},
                {"field": "inprogress", "operator": "false", "value": ""},
            ]},
            "sort": {"method": "random", "order": "ascending"}, "limit": 10,
        },
    },
    {
        "label": "Top Rated Movies", "type": "movies", "url": "library://top_rated_movies",
        "query": {
            "filter": {"field": "lastplayed", "operator": "notinthelast", "value": "24 months"},
            "sort": {"method": "rating", "order": "descending"}, "limit": 50,
        },
    },
    {
        "label": "Top Rated Shows", "type": "shows", "url": "library://top_rated_shows",
        "query": {
            "filter": {"field": "numwatched", "operator": "is", "value": "0"},
            "sort": {"method": "rating", "order": "descending"}, "limit": 50,
        },
    },
]


# Every built-in list -- regular, seasonal or library -- has an on/off toggle in settings.
# The kind prefix keeps the three namespaces apart (a key only has to be unique within its
# own group) and groups them into separate settings categories.
TOGGLE_KINDS = ("list", "seasonal", "library")


def toggle_setting_id(kind: str, key: str) -> str:
    """Settings id for a built-in list's on/off toggle. kind is one of TOGGLE_KINDS."""
    assert kind in TOGGLE_KINDS, kind
    return f"{kind}_{key}"


def parse_jellyfin_url(url: str) -> tuple[str, str]:
    """Parses "jellyfin://<collection>/<kind>" -> (collection, kind).

    e.g. "jellyfin://favorites/movies" -> ("favorites", "movies"). The kind segment is
    optional and comes back "" when absent, in which case the caller falls back to the
    entry's own "type" -- the two must agree, and "type" is the one that picks the render
    path, so it wins by being the fallback rather than the override.
    """
    body = url[len("jellyfin://"):].split("?", 1)[0]
    collection, _, kind = body.partition("/")
    return collection.strip().lower(), kind.strip().strip("/").lower()


def parse_limit(url: str) -> int | None:
    """Extracts ?limit=N from a list URL, if present. The full list is always fetched
    from mdblist regardless (mdblist's own limit param isn't forwarded to the API) --
    this is used to cap the *matched* results afterward, so e.g. a Trending Movies list
    with 100 raw items but only 30 locally-owned isn't further truncated below 30 by a
    limit meant to bound the top-N matches, not the raw fetch."""
    query = parse_qs(urlparse(url).query)
    values = query.get("limit")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Built-in list definitions. Each entry needs "label", "type" ("movies"/"shows"), and
# "url". "key" is auto-derived from "label" via slugify() -- don't set it manually.
# Always active -- no "window" here; that's what SEASONAL_CONFIGS below is for.
# -----------------------------------------------------------------------------
LIST_CONFIGS: list[dict[str, Any]] = [
    # "Want to Watch" -- the per-user queue backed by Jellyfin's IsFavorite flag. Split into
    # two widgets (movies / shows) rather than one mixed list, matching Trending Movies /
    # Trending Shows: each gets its own shelf in the skin, and each renders through the
    # matching content type instead of forcing a single one onto a mixed list.
    #
    # Keys derive from the labels via slugify(), so DON'T rename a label without re-pointing
    # the skin. Both are always-active and render empty when Jellyfin has nothing (or isn't
    # signed in), so a skin's auto-hide keeps them off screen until they have content.
    #
    # The URL's trailing segment must agree with "type", which is what picks the render path
    # and the IncludeItemTypes asked for. A disagreement is logged, not honoured.
    {
        "label": "Want to Watch Movies",  # -> ?list=want_to_watch_movies
        "type": "movies",
        "url": "jellyfin://favorites/movies",
    },
    {
        "label": "Want to Watch Shows",  # -> ?list=want_to_watch_shows
        "type": "shows",
        "url": "jellyfin://favorites/shows",
    },
    {
        "label": "Trending Movies",
        "type": "movies",
        "url": "https://mdblist.com/lists/baloo/tmdb-trending-daily-movies?limit=20",
    },
    {
        "label": "Star Wars (Chronological Order)",
        "type": "movies",
        "url": "https://mdblist.com/lists/oldmankestis/star-wars-chronological-order?sort=usort&sortorder=desc",
    },
    {
        "label": "Marvel Cinematic Universe: The Sacred Timeline",
        "type": "movies",
        "url": "https://mdblist.com/lists/hextv/cinesists-mcu-chronological-protocol-the-sacred-timelinemovies?sort=usort&sortorder=desc",
    },
    {
        "label": "Best Picture Winners",
        "type": "movies",
        "url": "https://mdblist.com/lists/berusca1996/academy-awards-best-picture-winners",
    },
    {
        "label": "Trending Shows",
        "type": "shows",
        "url": "https://mdblist.com/lists/baloo/tmdb-trending-daily-series?limit=20",
    },
]


# -----------------------------------------------------------------------------
# Seasonal list definitions. Same shape as LIST_CONFIGS plus a required "window":
# {"start": (mm, dd), "end": (mm, dd)} (inclusive, wraps the year boundary if
# start > end) or the special string "friday_13th".
#
# Unlike LIST_CONFIGS, these are NOT individually addressable widgets -- they only
# ever surface through the single combined "Seasonals" key (see
# default.py:render_seasonal_aggregate), which scans this list top-down and shows
# whichever entry's window is active first. Add a new holiday here once; nothing
# else needs touching. Order matters if two windows could ever overlap -- whichever
# is listed first wins.
# -----------------------------------------------------------------------------
SEASONAL_CONFIGS: list[dict[str, Any]] = [
    {
        "label": "Christmas",
        "type": "movies",
        "url": "https://mdblist.com/lists/hdlists/christmas-movies?sort=random",
        "window": {"start": (12, 7), "end": (1, 7)},  # matches Arctic Fuse Exp_SeasonalTheme_Christmas
    },
    {
        "label": "Valentine's Day",
        "type": "movies",
        "url": "https://mdblist.com/lists/linvo/valentines-day-popular-movies?sort=random",
        "window": {"start": (2, 7), "end": (2, 14)},
    },
    {
        "label": "May the 4th",
        "type": "movies",
        "url": "https://mdblist.com/lists/oldmankestis/star-wars-chronological-order?sort=random",
        "window": {"start": (5, 1), "end": (5, 7)},
    },
    {
        "label": "Star Trek Day",
        "type": "movies",
        "url": "https://mdblist.com/lists/takeaflick/star-trek-universe?sort=random",
        "window": {"start": (9, 5), "end": (9, 12)},
    },
    {
        "label": "Halloween",
        "type": "movies",
        "url": "https://mdblist.com/lists/hdlists/the-top-100-halloween-movies-of-all-time?sort=random",
        "window": {"start": (10, 1), "end": (11, 1)},  # matches Arctic Fuse Exp_SeasonalTheme_Halloween
    },
    {
        "label": "Nick November",
        "type": "movies",
        "url": "local://actor/Nicolas Cage",
        "window": {"start": (11, 1), "end": (11, 30)},
    },
    {
        "label": "Friday the 13th",
        "type": "movies",
        "url": "local://genre/Horror",
        "window": "friday_13th",
    },
]


def finalize_config(entry: dict[str, Any]) -> dict[str, Any]:
    """Fills in the derived fields (key, source) for any entry that only specified
    label/type/url/window. Built-in and user-authored entries both go through this
    exact same code path -- there's no separate handling for either."""
    entry = dict(entry)
    entry.setdefault("key", slugify(entry["label"]))
    entry["source"] = detect_source(entry["url"])
    return entry


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def describe_window(entry: dict[str, Any]) -> str:
    """Plain-English description of when a seasonal entry is active.

    Used to generate the per-entry help text in settings.xml/strings.po. Those are static
    files, so if a "window" below changes, the help text must be REGENERATED -- deriving it
    here rather than hand-writing it is what makes that a mechanical step instead of a
    silently-stale sentence.
    """
    window = entry.get("window")
    if window is None:
        return "Always active."
    if window == "friday_13th":
        return "Active only on a Friday the 13th."

    (start_month, start_day), (end_month, end_day) = window["start"], window["end"]
    if start_month == end_month:
        return f"Active from {start_day} to {end_day} {_MONTH_NAMES[start_month - 1]}."
    text = (
        f"Active from {start_day} {_MONTH_NAMES[start_month - 1]} "
        f"to {end_day} {_MONTH_NAMES[end_month - 1]}"
    )
    return text + (" (wrapping over the new year)." if start_month > end_month else ".")


def _in_month_day_window(today: datetime, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """Checks whether today's (month, day) falls in a start/end window, inclusive both
    ends, handling wraparound across the year boundary (e.g. start=(12, 7), end=(1, 7))."""
    today_val = (today.month, today.day)
    if start <= end:
        return start <= today_val <= end
    return today_val >= start or today_val <= end


def _is_friday_the_13th(today: datetime) -> bool:
    return today.day == 13 and today.weekday() == 4  # Monday=0 ... Friday=4


def is_window_active(entry: dict[str, Any], today: datetime) -> bool:
    """Entries without a "window" key are always active."""
    window = entry.get("window")
    if window is None:
        return True
    if window == "friday_13th":
        return _is_friday_the_13th(today)
    return _in_month_day_window(today, window["start"], window["end"])
