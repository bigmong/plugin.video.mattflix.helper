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
    queries the local Kodi library directly. Anything on mdblist.com uses the MDBList API.
    imdb.com/themoviedb.org are recognized but not yet implemented -- reserved so those
    can be dropped in as list sources later without changing this schema again.
    """
    url_lower = url.lower()
    if url_lower.startswith("local://"):
        return "local"
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
