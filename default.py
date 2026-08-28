"""MattFlix Helper -- widgets for Arctic Fuse, every one of them resolved against the local
Kodi library via JSON-RPC so items carry real watched state, resume points and dbids.

Four kinds of list: the local library itself (library://), MDBList (cached, needs an API
key), the box's own Jellyfin favourites -- the "Want to Watch" queue, which borrows the
login plugin.video.jellyfin already stores and needs no setup -- and date-windowed seasonal
lists. Nothing here writes Jellyfin metadata except the wtw_toggle verb, and no external
cron job or file share is required.

See resources/lib/config.py for the list definitions and resources/settings.xml for the
user-editable "Custom Lists" override.
"""

import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

sys.path.insert(0, str(Path(__file__).resolve().parent / "resources" / "lib"))

import config as cfg_module  # noqa: E402
import jellyfin_client  # noqa: E402
import mdblist_client  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
CACHE_DIR = Path(xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))) / "cache"

MOVIE_PROPERTIES = [
    "title",
    "imdbnumber",
    "uniqueid",
    "art",
    "plot",
    "year",
    "premiered",
    "genre",
    "rating",
    "runtime",
    "mpaa",
    "director",
    "cast",
    "playcount",
    "lastplayed",
    "resume",
    "set",
    "setid",
]
TVSHOW_PROPERTIES = [
    "title",
    "imdbnumber",
    "uniqueid",
    "art",
    "plot",
    "year",
    "premiered",
    "genre",
    "rating",
    "mpaa",
    "playcount",
    "lastplayed",
    "episode",
    "watchedepisodes",
]


def log(message: str) -> None:
    xbmc.log(f"[{ADDON_ID}] {message}", level=xbmc.LOGINFO)


def notify_error(message: str) -> None:
    """Logs AND shows a Kodi popup notification -- for things the user should actually
    notice (missing/wrong API key, HTTP failures, JSON-RPC errors), not routine misses."""
    log(f"ERROR: {message}")
    try:
        xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), message, xbmcgui.NOTIFICATION_ERROR, 6000)
    except Exception:
        pass  # never let a notification failure break the actual directory listing


# -----------------------------------------------------------------------------
# Config loading: built-in LIST_CONFIGS + up to 8 user-authored additions from the
# Lists settings screen (custom_list_<n>_label/type/url), merged (user entries win on a
# key collision). A slot with no label or no URL is treated as unused.
# -----------------------------------------------------------------------------
CUSTOM_LIST_SLOTS = 8


def _setting_bool(setting_id: str, default: bool = True) -> bool:
    """getSettingBool, but a setting this addon version doesn't define yet falls back to
    the default rather than silently reading as False and hiding a list."""
    try:
        return ADDON.getSettingBool(setting_id)
    except Exception:
        return default


def load_list_configs() -> list[dict]:
    merged: dict[str, dict] = {}

    for kind, entries in (("list", cfg_module.LIST_CONFIGS), ("library", cfg_module.LIBRARY_CONFIGS)):
        for entry in entries:
            finalized = cfg_module.finalize_config(entry)
            if not _setting_bool(cfg_module.toggle_setting_id(kind, finalized["key"])):
                continue  # switched off in settings -- hide it from the menu entirely
            merged[finalized["key"]] = finalized

    user_entries = []
    for i in range(1, CUSTOM_LIST_SLOTS + 1):
        label = (ADDON.getSetting(f"custom_list_{i}_label") or "").strip()
        url = (ADDON.getSetting(f"custom_list_{i}_url") or "").strip()
        if not label or not url:
            continue
        list_type = ADDON.getSetting(f"custom_list_{i}_type") or "movies"
        user_entries.append({"label": label, "type": list_type, "url": url})

    for entry in user_entries:
        finalized = cfg_module.finalize_config(entry)
        merged[finalized["key"]] = finalized  # user entries override built-ins on collision

    if user_entries:
        log(f"Loaded {len(user_entries)} user list(s) from settings")

    return list(merged.values())


def load_seasonal_configs() -> list[dict]:
    """Seasonal entries that are switched on.

    Filtering here rather than at the call sites means one toggle covers everything: the
    entry vanishes from the root menu, stops being individually addressable, AND drops out
    of the combined "Seasonals" rotation -- so switching off Halloween makes that widget fall
    through to whatever else is active in October rather than showing nothing.
    """
    enabled = []
    for entry in cfg_module.SEASONAL_CONFIGS:
        finalized = cfg_module.finalize_config(entry)
        if not _setting_bool(cfg_module.toggle_setting_id("seasonal", finalized["key"])):
            continue
        enabled.append(finalized)
    return enabled


# -----------------------------------------------------------------------------
# Local Kodi library lookups via JSON-RPC (all matching happens live, per-box --
# each box resolves its own dbid, so there's no cross-box portability concern).
# -----------------------------------------------------------------------------
def _jsonrpc(method: str, params: dict) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
    if "error" in response:
        notify_error(f"JSON-RPC error in {method}: {response['error'].get('message', response['error'])}")
        return {}
    return response.get("result", {})


def _provider_ids(record: dict) -> dict[str, str]:
    """The imdb/tmdb/tvdb ids a library row carries.

    Both fields are already in MOVIE_PROPERTIES / TVSHOW_PROPERTIES, so carrying them onto
    every item costs nothing extra and is what lets the Want to Watch star be decided by
    comparing ids we already hold, instead of scanning the whole library to turn Jellyfin
    favourites into local dbids.
    """
    unique = record.get("uniqueid") or {}
    return {
        "imdb": (record.get("imdbnumber") or "").strip().lower(),
        "tmdb": str(unique.get("tmdb") or "").strip().lower(),
        "tvdb": str(unique.get("tvdb") or "").strip().lower(),
    }


def _movie_info(movie: dict) -> dict:
    return {
        "kodi_id": movie["movieid"],
        "provider_ids": _provider_ids(movie),
        "title": movie.get("title", ""),
        "art": movie.get("art", {}),
        "plot": movie.get("plot", ""),
        "year": movie.get("year", 0),
        "premiered": movie.get("premiered", ""),
        "genre": movie.get("genre", []),
        "rating": movie.get("rating", 0),
        "runtime": movie.get("runtime", 0),
        "mpaa": movie.get("mpaa", ""),
        "director": movie.get("director", []),
        "cast": movie.get("cast", []),
        "playcount": movie.get("playcount", 0),
        "lastplayed": movie.get("lastplayed", ""),
        "resume": movie.get("resume", {}),
        "set": movie.get("set", ""),
        "setid": movie.get("setid", 0),
    }


# Building an index means pulling the whole movie or TV library over JSON-RPC, which is by
# far the most expensive thing a widget refresh does. Only the list itself needs one today
# -- the Want to Watch star is decided from ids the items already carry -- but this used to
# be built twice per render, once per call site, and memoizing is three lines against a
# recurrence of exactly that. Kodi re-executes default.py per invocation, which is the
# lifetime wanted: one scan per widget, never a stale index carried into the next refresh.
_PROVIDER_INDEX_MEMO: dict[str, tuple] = {}


def _memoized_index(memo_key: str, build):
    if memo_key not in _PROVIDER_INDEX_MEMO:
        _PROVIDER_INDEX_MEMO[memo_key] = build()
    return _PROVIDER_INDEX_MEMO[memo_key]


def _index_provider_ids(record: dict, info: dict, imdb_index: dict, tmdb_index: dict, tvdb_index: dict | None = None):
    """File one library row into the provider indexes under every id it carries."""
    imdb = (record.get("imdbnumber") or "").strip().lower()
    if imdb:
        imdb_index[imdb] = info
        # Sources disagree about the "tt" prefix, so index both spellings of the same id.
        imdb_index[imdb[2:] if imdb.startswith("tt") else f"tt{imdb}"] = info
    unique = record.get("uniqueid") or {}
    if unique.get("tmdb"):
        tmdb_index[str(unique["tmdb"]).strip().lower()] = info
    if tvdb_index is not None and unique.get("tvdb"):
        tvdb_index[str(unique["tvdb"]).strip().lower()] = info


def build_movie_provider_index() -> tuple[dict[str, dict], dict[str, dict]]:
    """Index the local movie library by imdb id and tmdb id -> item info."""
    def build():
        movies = _jsonrpc("VideoLibrary.GetMovies", {"properties": MOVIE_PROPERTIES}).get("movies", [])
        log(f"Local movie library index: {len(movies)} movies found")
        imdb_index: dict[str, dict] = {}
        tmdb_index: dict[str, dict] = {}
        for movie in movies:
            _index_provider_ids(movie, _movie_info(movie), imdb_index, tmdb_index)
        return imdb_index, tmdb_index

    return _memoized_index("movie", build)


def _tvshow_info(show: dict) -> dict:
    return {
        "kodi_id": show["tvshowid"],
        "provider_ids": _provider_ids(show),
        "title": show.get("title", ""),
        "art": show.get("art", {}),
        "plot": show.get("plot", ""),
        "year": show.get("year", 0),
        "premiered": show.get("premiered", ""),
        "genre": show.get("genre", []),
        "rating": show.get("rating", 0),
        "mpaa": show.get("mpaa", ""),
        "playcount": show.get("playcount", 0),
        "lastplayed": show.get("lastplayed", ""),
        "episode": show.get("episode", 0),
        "watchedepisodes": show.get("watchedepisodes", 0),
    }


def build_tvshow_provider_index() -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Index the local TV show library by imdb id, tmdb id, and tvdb id -> item info.

    Unlike movies, a lot of TV libraries are scraped with only a TheTVDB uniqueid set (no
    imdb/tmdb linkage at all) -- imdbnumber can even hold a raw TVDB number in that case,
    not a real "tt..." id. So tvdb has to be a first-class index here, not an afterthought.
    """
    def build():
        shows = _jsonrpc("VideoLibrary.GetTVShows", {"properties": TVSHOW_PROPERTIES}).get("tvshows", [])
        log(f"Local TV show library index: {len(shows)} shows found")
        imdb_index: dict[str, dict] = {}
        tmdb_index: dict[str, dict] = {}
        tvdb_index: dict[str, dict] = {}
        for show in shows:
            _index_provider_ids(show, _tvshow_info(show), imdb_index, tmdb_index, tvdb_index)
        return imdb_index, tmdb_index, tvdb_index

    return _memoized_index("tvshow", build)


def match_mdblist_items(
    raw_items: list[dict], imdb_index: dict, tmdb_index: dict, tvdb_index: dict | None = None
) -> list[dict]:
    """Matches MDBList raw items against a local provider index, in source order."""
    tvdb_index = tvdb_index or {}
    debug = _setting_bool("debug_logging", default=False)
    matched = []
    for item in raw_items:
        imdb_id = str(item.get("imdb_id") or item.get("imdb") or "").strip().lower()
        tmdb_id = str(item.get("tmdb_id") or item.get("tmdb") or item.get("id") or "").strip().lower()
        tvdb_id = str(item.get("tvdb_id") or item.get("tvdb") or "").strip().lower()
        title = item.get("title") or item.get("name") or "Unknown Title"

        info = None
        if imdb_id and imdb_id in imdb_index:
            info = imdb_index[imdb_id]
        elif tmdb_id and tmdb_id in tmdb_index:
            info = tmdb_index[tmdb_id]
        elif tvdb_id and tvdb_id in tvdb_index:
            info = tvdb_index[tvdb_id]

        if info is not None:
            matched.append(info)
            if debug:
                log(f"[DEBUG] Matched '{title}' -> '{info['title']}' (imdb={imdb_id}, tmdb={tmdb_id}, tvdb={tvdb_id})")
        elif debug:
            log(f"[DEBUG] No local match for '{title}' (imdb={imdb_id}, tmdb={tmdb_id}, tvdb={tvdb_id})")

    return matched


def query_local_movies_by_filter(field: str, value: str, limit: int) -> list[dict]:
    result = _jsonrpc(
        "VideoLibrary.GetMovies",
        {"properties": MOVIE_PROPERTIES, "filter": {"field": field, "operator": "contains", "value": [value]}},
    )
    movies = result.get("movies", [])
    sample = random.sample(movies, min(limit, len(movies)))
    return [_movie_info(m) for m in sample]


def group_movies_into_sets(movies: list[dict]) -> list[dict]:
    """Collapse movies belonging to a collection into a single set entry, the way Kodi's own
    Movies node does.

    Follows Kodi's rule rather than inventing one: a set containing only ONE movie in this
    result is left as the movie (Kodi's "group single item sets" is off by default), because
    a folder you open to find a single film is just an extra click. The set takes the
    position of its first member, so whatever the list was sorted by still governs order.
    """
    counts: dict[int, int] = {}
    latest_year: dict[int, int] = {}
    for movie in movies:
        setid = int(movie.get("setid") or 0)
        if setid:
            counts[setid] = counts.get(setid, 0) + 1
            # Kodi dates a collection by its most recent entry -- verified against the
            # box: Blair Witch (1999, 2016) reports 2016, Blue Lagoon (1980, 1991) 1991.
            # Taken over the members present in THIS list rather than the whole library,
            # so a filtered list dates its collections by what it actually contains.
            latest_year[setid] = max(latest_year.get(setid, 0), int(movie.get("year") or 0))

    groupable = {setid for setid, count in counts.items() if count > 1}
    if not groupable:
        return movies

    art_by_set = {}
    result = _jsonrpc("VideoLibrary.GetMovieSets", {"properties": ["title", "art", "playcount", "plot"]})
    for movie_set in result.get("sets", []):
        art_by_set[movie_set["setid"]] = movie_set

    grouped: list[dict] = []
    seen: set[int] = set()
    for movie in movies:
        setid = int(movie.get("setid") or 0)
        if setid not in groupable:
            grouped.append(movie)
            continue
        if setid in seen:
            continue  # already represented by its set entry
        seen.add(setid)
        details = art_by_set.get(setid, {})
        grouped.append({
            "is_set": True,
            "kodi_id": setid,
            # Fall back to the movie's own "set" label if GetMovieSets didn't return it.
            "title": details.get("title") or movie.get("set") or "Collection",
            "art": details.get("art") or {},
            "plot": details.get("plot") or "",
            "year": latest_year.get(setid, 0),
            "movie_count": counts[setid],
        })
    return grouped


def query_library(list_cfg: dict) -> list[dict]:
    """Runs a built-in library list's query straight against the Kodi library.

    The query dict maps 1:1 onto JSON-RPC (filter/sort/limits), so these lists behave
    exactly like the smart playlists they replace -- same rules, same ordering -- while
    still being rendered by this addon, which is what lets them carry watched status,
    resume points and the Want to Watch badge.
    """
    query = list_cfg.get("query") or {}
    is_shows = list_cfg["type"] == "shows"
    group_sets = bool(query.get("group_sets"))

    # A limit of None means no cap, and that is load-bearing rather than an oversight: the
    # skin's shelf shows its own handful of items and then a "More..." entry that opens
    # THIS SAME url as a full listing. Capping here would silently shorten that browse to
    # the cap, so a list meant to be the whole library stops being the whole library.
    limit = query.get("limit")

    params: dict = {"properties": TVSHOW_PROPERTIES if is_shows else MOVIE_PROPERTIES}
    if query.get("filter"):
        params["filter"] = query["filter"]
    if query.get("sort"):
        params["sort"] = query["sort"]
    if limit and not group_sets:
        # JSON-RPC wants an explicit window; a .xsp <limit> is always "the first N".
        params["limits"] = {"start": 0, "end": int(limit)}

    if is_shows:
        result = _jsonrpc("VideoLibrary.GetTVShows", params)
        return [_tvshow_info(show) for show in result.get("tvshows", [])]

    result = _jsonrpc("VideoLibrary.GetMovies", params)
    movies = [_movie_info(movie) for movie in result.get("movies", [])]
    if group_sets:
        movies = group_movies_into_sets(movies)
        if limit:
            # Applied here rather than pushed into "limits": JSON-RPC would otherwise cap
            # BEFORE collections are collapsed, which both shrinks the list (100 movies can
            # collapse to 88 entries) and shows partial sets containing only the members
            # that made the cut.
            movies = movies[:int(limit)]
    return movies


# -----------------------------------------------------------------------------
# Directory builders
# -----------------------------------------------------------------------------
def add_movie_items(matched: list[dict], badge: bool = True) -> None:
    badge_keys = want_to_watch_keys("movie") if badge and matched else set()
    for info in matched:
        if info.get("is_set"):
            add_set_item(info)
            continue
        li = xbmcgui.ListItem(label=info["title"])
        vtag = li.getVideoInfoTag()
        vtag.setTitle(info["title"])
        vtag.setMediaType("movie")
        # Without this, ListItem.DBID is empty on every item in this widget: the keymap
        # toggle can't identify what's focused, and the skin can't tell it's a library item.
        vtag.setDbId(info["kodi_id"])
        # Year alone is not enough for the skin. Verified against the box over JSON-RPC:
        # our items already carried a correct year (and rating), yet Arctic Fuse showed
        # neither -- the only field differing from a native library item was premiered,
        # which Kodi populates on everything it builds itself and we never set. Setting
        # year as well because an item with no premiered date still has to show one.
        if info.get("year"):
            vtag.setYear(info["year"])
        if info.get("premiered"):
            vtag.setPremiered(info["premiered"])
        if info.get("plot"):
            vtag.setPlot(info["plot"])
        if info.get("genre"):
            vtag.setGenres(info["genre"])
        if info.get("rating"):
            vtag.setRating(info["rating"])
        if info.get("runtime"):
            vtag.setDuration(info["runtime"])
        if info.get("mpaa"):
            vtag.setMpaa(info["mpaa"])
        if info.get("director"):
            vtag.setDirectors(info["director"])
        if info.get("cast"):
            try:
                actors = [
                    xbmc.Actor(c.get("name", ""), c.get("role", ""), c.get("order", 0), c.get("thumbnail", ""))
                    for c in info["cast"]
                ]
                vtag.setCast(actors)
            except AttributeError:
                pass  # xbmc.Actor / setCast unavailable on this Kodi version -- skip cast, not fatal
        # Watched state has to be set explicitly: this is a hand-built ListItem, not one
        # Kodi handed back from the library, so nothing carries over from the videodb://
        # path on its own. Kodi renders the watched tick/overlay straight off playcount --
        # set it even when 0 so "unwatched" is stated rather than merely absent.
        vtag.setPlaycount(int(info.get("playcount") or 0))
        if info.get("lastplayed"):
            vtag.setLastPlayed(info["lastplayed"])
        resume = info.get("resume") or {}
        if resume.get("position"):
            # Partially-watched progress bar. total can legitimately be 0 if Kodi never
            # stored a duration; setResumePoint tolerates that.
            vtag.setResumePoint(float(resume["position"]), float(resume.get("total") or 0))
        if info.get("art"):
            li.setArt(info["art"])

        if badge_keys and _wtw_lookup_keys(info.get("provider_ids") or {}) & badge_keys:
            li.setProperty(WTW_BADGE_PROPERTY, "1")

        url = f"videodb://movies/titles/{info['kodi_id']}"
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)


def add_set_item(info: dict) -> None:
    """A movie collection, rendered as a folder into Kodi's own set view."""
    li = xbmcgui.ListItem(label=info["title"])
    vtag = li.getVideoInfoTag()
    vtag.setTitle(info["title"])
    vtag.setMediaType("set")  # Kodi's media type for a collection
    vtag.setDbId(info["kodi_id"])
    # Art alone was reaching the skin and no text with it. A native collection carries a
    # year and a plot (premiered is empty on those too, so it is not the culprit here as
    # it was for movies and shows); ours carried neither, so there was nothing to draw.
    if info.get("year"):
        vtag.setYear(info["year"])
    if info.get("plot"):
        vtag.setPlot(info["plot"])
    if info.get("art"):
        li.setArt(info["art"])
    # Skins show the member count on a set the same way they show episode counts on a show.
    li.setProperty("TotalMovies", str(info.get("movie_count", 0)))
    li.setIsFolder(True)
    xbmcplugin.addDirectoryItem(HANDLE, f"videodb://movies/sets/{info['kodi_id']}/", li, isFolder=True)


def add_tvshow_items(matched: list[dict], badge: bool = True) -> None:
    badge_keys = want_to_watch_keys("tvshow") if badge and matched else set()
    for info in matched:
        li = xbmcgui.ListItem(label=info["title"])
        vtag = li.getVideoInfoTag()
        vtag.setTitle(info["title"])
        vtag.setMediaType("tvshow")
        vtag.setDbId(info["kodi_id"])  # see add_movie_items
        # Year alone is not enough for the skin. Verified against the box over JSON-RPC:
        # our items already carried a correct year (and rating), yet Arctic Fuse showed
        # neither -- the only field differing from a native library item was premiered,
        # which Kodi populates on everything it builds itself and we never set. Setting
        # year as well because an item with no premiered date still has to show one.
        if info.get("year"):
            vtag.setYear(info["year"])
        if info.get("premiered"):
            vtag.setPremiered(info["premiered"])
        if info.get("plot"):
            vtag.setPlot(info["plot"])
        if info.get("genre"):
            vtag.setGenres(info["genre"])
        if info.get("rating"):
            vtag.setRating(info["rating"])
        # Shows carry a certificate just as movies do; this was simply never requested or
        # set on the show path, so the skin had nothing to draw.
        if info.get("mpaa"):
            vtag.setMpaa(info["mpaa"])
        # A show counts as watched only once every episode is. Kodi's own tvshow playcount
        # is the watched-episode count rather than a 0/1 flag, so deriving it here keeps
        # the overlay matching what the real library listing shows.
        total_episodes = int(info.get("episode") or 0)
        watched_episodes = int(info.get("watchedepisodes") or 0)
        vtag.setPlaycount(1 if total_episodes and watched_episodes >= total_episodes else 0)
        if total_episodes:
            vtag.setEpisode(total_episodes)
        if info.get("lastplayed"):
            vtag.setLastPlayed(info["lastplayed"])
        # Skins (Arctic Fuse included) read the unwatched-count badge off these properties,
        # not off the info tag. The watched-progress *pie* is a fourth, separate property:
        # Arctic Fuse's Defs_PercentPlayed variable checks WatchedEpisodePercent first for
        # shows, then falls back to PercentPlayed (resume-based, so movies-only) and
        # WatchedProgress. Its value is substituted straight into a texture path,
        # progress/circle/p<value>.png -- leave it unset and that resolves to "p.png", which
        # doesn't exist, so the counts render fine and the pie silently doesn't.
        #
        # Send the honest raw percentage including 0 and 100: the skin excludes both itself
        # (a fully-watched show gets the checkmark indicator instead of a full circle).
        # Clamped anyway, since only p0..p100 exist as textures.
        watched_percent = round(watched_episodes / total_episodes * 100) if total_episodes else 0
        li.setProperties(
            {
                "TotalEpisodes": str(total_episodes),
                "WatchedEpisodes": str(watched_episodes),
                "UnWatchedEpisodes": str(max(total_episodes - watched_episodes, 0)),
                "WatchedEpisodePercent": str(min(100, max(0, watched_percent))),
            }
        )
        if info.get("art"):
            li.setArt(info["art"])
        li.setIsFolder(True)

        if badge_keys and _wtw_lookup_keys(info.get("provider_ids") or {}) & badge_keys:
            li.setProperty(WTW_BADGE_PROPERTY, "1")

        url = f"videodb://tvshows/titles/{info['kodi_id']}/"
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    xbmcplugin.setContent(HANDLE, "tvshows")
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)


def add_empty_list(list_cfg: dict) -> None:
    """Renders an empty directory for a list that has nothing to show -- an inactive seasonal
    window, a missing API key, an unimplemented source.

    Still goes through the matching builder rather than short-circuiting, because the content
    type it sets ("movies"/"tvshows") is what the skin keys its widget layout off -- an empty
    shows widget announcing itself as movies gets styled wrong before it ever has an item in it.
    """
    if list_cfg["type"] == "shows":
        add_tvshow_items([])
    else:
        add_movie_items([])


# -----------------------------------------------------------------------------
# "Want to Watch": the Jellyfin-backed queue
# -----------------------------------------------------------------------------
JELLYFIN_DATA_PATH = "special://profile/addon_data/plugin.video.jellyfin/data.json"


def read_jellyfin_credentials() -> dict | None:
    """Server URL, user id and token, borrowed from plugin.video.jellyfin on this box.

    This is why Want to Watch needs no API key and no per-box setup: the Jellyfin addon is
    already logged in, and "the current user" is simply whoever it's logged in as. Key
    names match what that addon's own entrypoint reads out of the same file.

    It's an internal file with no compatibility promise, so every failure is caught and
    reported as "not configured" rather than raised -- an unconfigured or restructured
    Jellyfin addon must degrade to an empty widget, never break the directory listing.
    """
    try:
        raw = Path(xbmcvfs.translatePath(JELLYFIN_DATA_PATH)).read_text(encoding="utf-8")
        server = (json.loads(raw).get("Servers") or [])[0]
    except (OSError, ValueError, IndexError, TypeError, AttributeError) as exc:
        log(f"[Jellyfin] No usable credentials in {JELLYFIN_DATA_PATH} ({exc}) -- is the Jellyfin addon signed in?")
        return None

    # "address" is normalised on load by the Jellyfin addon itself, which migrates these
    # two older key names into it -- a profile that predates that migration can still have
    # them in the file on disk.
    address = server.get("address") or server.get("ManualAddress") or server.get("LocalAddress") or ""
    user_id = server.get("UserId") or ""
    token = server.get("AccessToken") or ""

    if not (address and user_id and token):
        log("[Jellyfin] Credentials file is present but incomplete -- is the Jellyfin addon signed in?")
        return None

    return {"address": address, "user_id": user_id, "token": token}


def render_jellyfin_list(list_cfg: dict) -> None:
    """Renders one Want to Watch widget: the user's Jellyfin favourites of a single kind,
    resolved against the local library exactly like every other list here."""
    collection, kind = cfg_module.parse_jellyfin_url(list_cfg["url"])
    if kind and kind != list_cfg["type"]:
        # "type" picks the render path and the IncludeItemTypes asked for, so a disagreeing
        # URL segment changes nothing -- which is precisely why it needs saying out loud.
        log(
            f"'{list_cfg['label']}': URL says '{kind}' but type is '{list_cfg['type']}'. "
            f"'type' decides, so the URL segment is being ignored -- correct one of them."
        )
    if collection != "favorites":
        log(f"'{list_cfg['label']}': jellyfin collection '{collection}' is not implemented -- returning empty.")
        add_empty_list(list_cfg)
        return

    credentials = read_jellyfin_credentials()
    if credentials is None:
        # Logged, not notified: a box where the Jellyfin addon isn't signed in would
        # otherwise pop a toast on every single widget refresh.
        add_empty_list(list_cfg)
        return

    is_shows = list_cfg["type"] == "shows"
    # None means the fetch failed (and has already notified); [] means nothing is
    # favourited. Both render empty here, but the distinction matters to the badge cache.
    raw_items = jellyfin_client.get_favorites(
        credentials["address"],
        credentials["user_id"],
        credentials["token"],
        jellyfin_client.KIND_SERIES if is_shows else jellyfin_client.KIND_MOVIE,
        client=ADDON.getAddonInfo("name"),
        device_id=ADDON_ID,
        version=ADDON.getAddonInfo("version"),
        log=log,
        on_error=notify_error,
    ) or []

    # Sort BEFORE matching: match_mdblist_items preserves source order, so ordering the
    # Jellyfin items once here carries all the way through to the rendered directory.
    # Note there's no watched filter: removal is the server-side reconciler's job, so
    # whatever is still favourited is shown. Nothing here decides what leaves the queue.
    ordered = jellyfin_client.to_match_items(jellyfin_client.sort_favorites(raw_items))

    if is_shows:
        imdb_index, tmdb_index, tvdb_index = build_tvshow_provider_index()
        matched = match_mdblist_items(ordered, imdb_index, tmdb_index, tvdb_index)
    else:
        imdb_index, tmdb_index = build_movie_provider_index()
        matched = match_mdblist_items(ordered, imdb_index, tmdb_index)

    list_limit = cfg_module.parse_limit(list_cfg["url"])
    if list_limit is not None:
        matched = matched[:list_limit]

    log(f"'{list_cfg['label']}': {len(raw_items)} favourite(s) -> {len(matched)} matched locally")

    if is_shows:
        add_tvshow_items(matched, badge=False)
    else:
        add_movie_items(matched, badge=False)


# Arctic Fuse draws its favourite badge purely on "is this ListItem property non-empty"
# (Object_TraktOverlays -> favorites_rank -> flags/color/trakt/favorite.png). The name is
# historical -- nothing about the skin's condition is Trakt-specific -- so setting it marks
# our items with the star. The skin's own Indicator.TraktFavourites toggle is the on/off
# switch, which is why this addon deliberately adds no setting of its own.
WTW_BADGE_PROPERTY = "favorites_rank"

# Short TTL: every widget render would otherwise re-ask Jellyfin. This does NOT contradict
# the Want to Watch widget being uncached -- that list must show a heart tapped seconds ago,
# whereas a star on some other shelf can lag a minute. wtw_toggle drops the cache on write
# so your own presses show up immediately anyway.
WTW_KEYS_CACHE_TTL_SECONDS = 60.0
_WTW_KEYS_MEMO: dict[str, set[str]] = {}


def _wtw_keys_cache_path(media: str) -> Path:
    return CACHE_DIR / f"wtw_ids_{media}.json"


def invalidate_want_to_watch_keys() -> None:
    """Called after a toggle so the star reflects the press immediately."""
    _WTW_KEYS_MEMO.clear()
    for media in ("movie", "tvshow"):
        try:
            _wtw_keys_cache_path(media).unlink()
        except OSError:
            pass


def _wtw_lookup_keys(provider_ids: dict) -> set[str]:
    """Comparable keys for one item's provider ids.

    Namespaced, because a tmdb id and an imdb id are both bare numbers and a flat set would
    happily match tmdb 348 against imdb 348. The "tt" prefix is stripped rather than stored
    both ways, since sources disagree about it and one canonical spelling is enough when
    both sides go through here.
    """
    keys = set()
    imdb = (provider_ids.get("imdb") or "").strip().lower()
    if imdb:
        keys.add(f"imdb:{imdb[2:] if imdb.startswith('tt') else imdb}")
    for name in ("tmdb", "tvdb"):
        value = (provider_ids.get(name) or "").strip().lower()
        if value:
            keys.add(f"{name}:{value}")
    return keys


def _fetch_want_to_watch_keys(media: str) -> set[str] | None:
    """Provider-id keys for everything currently favourited, or None if Jellyfin couldn't
    be asked.

    Note what this does NOT do: touch the local library. It used to resolve favourites into
    kodi dbids, which meant a full VideoLibrary.GetMovies per widget purely to draw stars.
    Every item already carries its own provider ids, so the comparison happens on those and
    the scan is gone.

    None is not the same as an empty set: empty is "nothing is on the list", which is worth
    caching; None is "we don't know", which must not be.
    """
    credentials = read_jellyfin_credentials()
    if credentials is None:
        return set()  # signed out is a real, stable answer: no Jellyfin, no stars

    raw_items = jellyfin_client.get_favorites(
        credentials["address"], credentials["user_id"], credentials["token"],
        jellyfin_client.KIND_SERIES if media == "tvshow" else jellyfin_client.KIND_MOVIE,
        client=ADDON.getAddonInfo("name"), device_id=ADDON_ID, version=ADDON.getAddonInfo("version"),
        log=log,
        on_error=None,  # a star is decoration: never pop a toast because it couldn't be drawn
    )
    if raw_items is None:
        return None

    keys: set[str] = set()
    for item in jellyfin_client.to_match_items(raw_items):
        keys |= _wtw_lookup_keys(
            {"imdb": item["imdb_id"], "tmdb": item["tmdb_id"], "tvdb": item["tvdb_id"]}
        )
    return keys


def want_to_watch_keys(media: str) -> set[str]:
    """Provider-id keys currently on the Want to Watch list, for starring other widgets.

    Always degrades to an empty set: no Jellyfin, no star, no error. Nothing here is
    allowed to break a listing over decoration.
    """
    if media in _WTW_KEYS_MEMO:
        return _WTW_KEYS_MEMO[media]

    cache_file = _wtw_keys_cache_path(media)
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        keys = cached["provider_keys"]
        if not isinstance(keys, list):
            raise TypeError(keys)
        if time.time() - float(cached.get("fetched_at", 0)) < WTW_KEYS_CACHE_TTL_SECONDS:
            memo = {str(k) for k in keys}
            _WTW_KEYS_MEMO[media] = memo
            return memo
    except (OSError, ValueError, TypeError, KeyError):
        pass  # missing, stale, corrupt, or written by an older payload shape -- refetch

    try:
        keys = _fetch_want_to_watch_keys(media)
    except Exception as exc:
        log(f"[Jellyfin] Want to Watch star lookup failed ({exc}) -- rendering without stars.")
        keys = None

    if keys is None:
        # Don't cache what we couldn't find out. Writing an empty set with a fresh
        # timestamp would suppress every star for the full TTL over one failed call.
        _WTW_KEYS_MEMO[media] = set()
        return set()

    _WTW_KEYS_MEMO[media] = keys
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"fetched_at": time.time(), "provider_keys": sorted(keys)}), encoding="utf-8"
        )
    except OSError:
        pass
    return keys


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------
def render_list(list_cfg: dict, ignore_window: bool = False) -> None:
    if not ignore_window and not cfg_module.is_window_active(list_cfg, datetime.now()):
        log(f"'{list_cfg['label']}' has an inactive window right now -- returning empty.")
        add_empty_list(list_cfg)
        return

    # Seasonals only, despite the generic-sounding setting id: the two places this is used
    # are the local:// query and the random sample applied to windowed entries, and every
    # windowed entry is a seasonal. Library and MDBList lists reach neither.
    seasonal_limit = int(ADDON.getSettingInt("local_list_item_limit") or 10)
    source = list_cfg["source"]

    if source == "local":
        field, value = cfg_module.parse_local_url(list_cfg["url"])
        matched = query_local_movies_by_filter(field, value, seasonal_limit)
        add_movie_items(matched)
        log(f"'{list_cfg['label']}': showing {len(matched)} local items (field={field}, value={value})")
        return

    if source == "library":
        matched = query_library(list_cfg)
        if list_cfg["type"] == "shows":
            add_tvshow_items(matched)
        else:
            add_movie_items(matched)
        log(f"'{list_cfg['label']}': {len(matched)} item(s) from the local library")
        return

    if source == "jellyfin":
        render_jellyfin_list(list_cfg)
        return

    if source not in ("mdblist",):
        log(f"'{list_cfg['label']}': source '{source}' is not yet implemented -- returning empty.")
        add_empty_list(list_cfg)
        return

    api_key = ADDON.getSetting("mdblist_api_key")
    if not api_key:
        notify_error(f"MDBList API key is not set (needed for '{list_cfg['label']}')")
        add_empty_list(list_cfg)
        return

    cache_hours = float(ADDON.getSettingInt("cache_hours") or 12)
    raw_items = mdblist_client.get_list_items(
        list_cfg["url"], api_key, CACHE_DIR, cache_ttl_hours=cache_hours, log=log, on_error=notify_error
    )
    log(f"Fetched {len(raw_items)} raw items for '{list_cfg['label']}'")

    # Windowed mdblist entries were built for random rotation (sort=random in the URL) --
    # cap them locally too, same as before. Always-on ordered lists (Trending Movies, MCU,
    # etc.) keep their matched order untouched, but if their URL specifies ?limit=N, that
    # caps the matched results (not the raw mdblist fetch, which always pulls the full
    # list -- a 100-item trending list with only 30 local matches shouldn't be truncated
    # further by a limit meant to bound the top-N matches).
    is_random_window_entry = "window" in list_cfg
    list_limit = cfg_module.parse_limit(list_cfg["url"])

    if list_cfg["type"] == "shows":
        imdb_index, tmdb_index, tvdb_index = build_tvshow_provider_index()
        matched = match_mdblist_items(raw_items, imdb_index, tmdb_index, tvdb_index)
        if is_random_window_entry:
            matched = random.sample(matched, min(seasonal_limit, len(matched)))
        elif list_limit is not None:
            matched = matched[:list_limit]
        add_tvshow_items(matched)
    else:
        imdb_index, tmdb_index = build_movie_provider_index()
        matched = match_mdblist_items(raw_items, imdb_index, tmdb_index)
        if is_random_window_entry:
            matched = random.sample(matched, min(seasonal_limit, len(matched)))
        elif list_limit is not None:
            matched = matched[:list_limit]
        add_movie_items(matched)

    log(f"Matched {len(matched)} items locally for '{list_cfg['label']}'")


SEASONAL_AGGREGATE_KEY = "seasonal"
SEASONAL_AGGREGATE_LABEL = "Seasonals"


def resolve_active_windowed(configs: list[dict]) -> dict | None:
    """Returns the first currently-active entry from a list of windowed configs, in list
    order (top = highest priority). Pure top-to-bottom scan, no persisted state."""
    today = datetime.now()
    for cfg in configs:
        if cfg_module.is_window_active(cfg, today):
            return cfg
    return None


def render_seasonal_aggregate() -> None:
    """The single combined "Seasonals" widget: add just this one to your skin and it
    shows whichever entry in SEASONAL_CONFIGS (Christmas, Halloween, Nick November, etc.)
    is currently active, top-down first match. Each entry is also individually reachable
    by its own key (see main()) if you'd rather hard-pin one instead of letting it rotate
    with the window."""
    active_cfg = resolve_active_windowed(load_seasonal_configs())
    if active_cfg is None:
        log("[Seasonals] No seasonal list is currently active.")
        add_movie_items([])
        return
    log(f"[Seasonals] Active: {active_cfg['label']}")
    render_list(active_cfg)


# Browsing the addon used to be one long flat list of every configured entry. These group it
# by where the list comes from, which is the distinction that actually matters when picking
# one: a library list is instant and always works, an MDBList one needs an API key and only
# shows what you already own, Want to Watch needs Jellyfin. An empty group is skipped rather
# than shown as an empty folder.
MENU_GROUPS = (
    ("library", "Library Lists"),
    ("mdblist", "MDBList Lists"),
    ("jellyfin", "Want to Watch"),
    ("seasonal", "Seasonals"),
    ("custom", "Custom Lists"),
)


def _grouped_menu_entries() -> dict[str, list[dict]]:
    """Every visible entry, bucketed by menu group. Seasonals come from their own config, so
    they're bucketed by origin rather than by source (they're mostly mdblist underneath)."""
    grouped: dict[str, list[dict]] = {key: [] for key, _label in MENU_GROUPS}

    builtin_keys = {cfg_module.finalize_config(e)["key"]
                    for group in (cfg_module.LIST_CONFIGS, cfg_module.LIBRARY_CONFIGS) for e in group}
    for list_cfg in load_list_configs():
        # Anything not in the built-in definitions came from a user's Custom Lists slot.
        group = list_cfg["source"] if list_cfg["key"] in builtin_keys else "custom"
        grouped.setdefault(group if group in grouped else "custom", []).append(list_cfg)

    grouped["seasonal"] = load_seasonal_configs()
    return grouped


def render_menu(group: str) -> None:
    """One group's lists. Seasonals lead with the combined rotating entry."""
    entries = _grouped_menu_entries().get(group, [])

    if group == "seasonal" and entries:
        active = resolve_active_windowed(entries)
        li = xbmcgui.ListItem(label=active["label"] if active else SEASONAL_AGGREGATE_LABEL)
        xbmcplugin.addDirectoryItem(HANDLE, f"{BASE_URL}?list={SEASONAL_AGGREGATE_KEY}", li, isFolder=True)

    for entry in entries:
        li = xbmcgui.ListItem(label=entry["label"])
        xbmcplugin.addDirectoryItem(HANDLE, f"{BASE_URL}?list={entry['key']}", li, isFolder=True)

    if not entries:
        log(f"Menu group {group!r} has no visible entries.")
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True)


def render_root_menu() -> None:
    """One folder per MENU_GROUPS group -- this is what a skin's widget-content-picker
    shows when it browses the addon with nothing selected, and it's the quickest way to
    try a list by hand in the addon browser. Each group's own lists are one level down,
    in render_menu(); a group with nothing configured (or everything switched off) is
    skipped rather than shown as an empty folder."""
    grouped = _grouped_menu_entries()
    for group, label in MENU_GROUPS:
        if not grouped.get(group):
            continue  # nothing configured (or everything switched off) -- don't show an empty folder
        li = xbmcgui.ListItem(label=label)
        xbmcplugin.addDirectoryItem(HANDLE, f"{BASE_URL}?menu={group}", li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True)


# -----------------------------------------------------------------------------
# Verb routes (?mode=<verb>)
#
# Invoked via RunPlugin() from a keymap or context menu rather than browsed to, so they get
# handle -1 and must never call into xbmcplugin: there's no directory to build. They just do
# their thing and return.
# -----------------------------------------------------------------------------
WTW_TOGGLE_MODE = "wtw_toggle"


# A Jellyfin item id is a 32-char hex GUID, and it reaches Kodi in several shapes:
#   art     : image://http%3a%2f%2fhost%2fItems%2f{id}%2fImages%2f...  (both playback modes)
#   file    : plugin://plugin.video.jellyfin/{libraryId}/{id}/         (tv shows)
#   file    : plugin://plugin.video.jellyfin/...?id={id}&mode=play     (add-on playback)
#
# Art is wrapped in image:// with the inner URL PERCENT-ENCODED, so "/Items/" arrives as
# "%2fItems%2f" -- everything here decodes first, or the id is simply invisible.
JELLYFIN_ART_ID_RE = re.compile(r"/Items/([0-9a-f]{32})", re.IGNORECASE)
JELLYFIN_QUERY_ID_RE = re.compile(r"[?&]id=([0-9a-f]{32})", re.IGNORECASE)
# Trailing path segment. The greedy prefix means the LAST 32-hex segment is captured, which
# matters: a tv show path leads with the library id and only ends with the item id.
JELLYFIN_SEGMENT_ID_RE = re.compile(
    r"plugin://plugin\.video\.jellyfin/(?:[^/?#]+/)*([0-9a-f]{32})", re.IGNORECASE
)


def extract_jellyfin_id(value: str) -> str | None:
    """Pull a Jellyfin item id out of any of the shapes above, or None."""
    if not value:
        return None
    decoded = unquote(value)
    for pattern in (JELLYFIN_ART_ID_RE, JELLYFIN_QUERY_ID_RE, JELLYFIN_SEGMENT_ID_RE):
        match = pattern.search(decoded)
        if match:
            return match.group(1).lower()
    return None

# ListItem.DBTYPE -> what actually goes on the list. Only movies and series are ever
# favourited, so an episode or season resolves up to its show first.
_DBTYPE_TO_MEDIA = {
    "movie": "movie",
    "tvshow": "tvshow",
    "season": "tvshow",
    "episode": "tvshow",
}


def focused_library_item() -> tuple[int, str] | None:
    """(dbid, "movie"|"tvshow") for whatever is focused, or None if it isn't a library item.

    Reads InfoLabels rather than taking an id parameter: the keymap binding is global, and
    ListItem.Property(jellyfinid) only exists inside plugin.video.jellyfin's own listings --
    on native library items, which is where you actually browse, it comes back empty.
    """
    dbtype = (xbmc.getInfoLabel("ListItem.DBTYPE") or "").strip().lower()
    media = _DBTYPE_TO_MEDIA.get(dbtype)
    if media is None:
        log(f"wtw_toggle: DBTYPE={dbtype!r} is not a movie/tvshow/season/episode -- ignoring.")
        return None

    if media == "tvshow" and dbtype != "tvshow":
        # Focused on an episode or season -- favourite the series it belongs to.
        raw_id = xbmc.getInfoLabel("ListItem.TvShowDBID")
    else:
        raw_id = xbmc.getInfoLabel("ListItem.DBID")

    try:
        dbid = int((raw_id or "").strip())
    except ValueError:
        log(f"wtw_toggle: DBTYPE={dbtype!r} but no usable DBID (got {raw_id!r}) -- ignoring.")
        return None
    if dbid <= 0:
        log(f"wtw_toggle: DBTYPE={dbtype!r} but DBID={dbid} -- ignoring.")
        return None
    log(f"wtw_toggle: focused {dbtype} dbid={dbid} -> looking up as {media}")
    return (dbid, media)


def find_jellyfin_id(dbid: int, media: str) -> str | None:
    """The Jellyfin item id for a Kodi library item, dug out of data Kodi already holds.

    Artwork first: the Jellyfin addon serves art from the server in BOTH of its playback
    modes, so /Items/<id>/Images/... is present whether or not direct paths are in use. The
    file path only carries the id in add-on playback mode, so it's the fallback, not the
    primary. Returns None if this simply isn't a Jellyfin-sourced item.
    """
    if media == "movie":
        result = _jsonrpc("VideoLibrary.GetMovieDetails", {"movieid": dbid, "properties": ["art", "file"]})
        details = result.get("moviedetails", {})
    else:
        result = _jsonrpc("VideoLibrary.GetTVShowDetails", {"tvshowid": dbid, "properties": ["art", "file"]})
        details = result.get("tvshowdetails", {})

    for candidate in list((details.get("art") or {}).values()) + [details.get("file", "")]:
        found = extract_jellyfin_id(candidate)
        if found:
            return found

    # Nothing matched -- dump exactly what Kodi returned, because the two ways this fails
    # (no art from Jellyfin vs. the JSON-RPC call itself returning nothing) look identical
    # from the outside and need completely different fixes.
    if not details:
        log(f"wtw_toggle: JSON-RPC returned no details for {media} dbid={dbid}")
    else:
        log(f"wtw_toggle: no Jellyfin id in {media} dbid={dbid}; art={details.get('art')} file={details.get('file')!r}")
    return None


def wtw_toggle() -> None:
    """Adds or removes the focused item from the Jellyfin-backed "Want to Watch" queue.

    Same function behind all three triggers (remote button, context menu, any widget action),
    so toggle semantics are settled in exactly one place: pressing it on something already on
    the list takes it off.
    """
    item = focused_library_item()
    if item is None:
        # The binding is global, so this fires constantly with nothing sensible focused --
        # settings, music, an empty widget. Staying silent here is the whole reason it can
        # be bound globally at all.
        log("wtw_toggle: nothing library-shaped is focused -- ignoring.")
        return

    dbid, media = item
    label = xbmc.getInfoLabel("ListItem.Label") or "this item"

    credentials = read_jellyfin_credentials()
    if credentials is None:
        notify_error("Jellyfin isn't signed in, so Want to Watch can't be updated.")
        return

    jellyfin_id = find_jellyfin_id(dbid, media)
    if jellyfin_id is None:
        # Worth a popup: the button visibly did nothing, and "this item didn't come from
        # Jellyfin" is the actionable explanation.
        log(f"wtw_toggle: no Jellyfin id found for {media} dbid={dbid} ({label})")
        notify_error(f"'{label}' doesn't look like a Jellyfin item.")
        return

    args = dict(
        client=ADDON.getAddonInfo("name"),
        device_id=ADDON_ID,
        version=ADDON.getAddonInfo("version"),
        log=log,
        on_error=notify_error,
    )
    currently = jellyfin_client.is_favorite(
        credentials["address"], credentials["user_id"], credentials["token"], jellyfin_id, **args
    )
    if currently is None:
        return  # the read failed and already notified -- never guess a write direction

    if not jellyfin_client.set_favorite(
        credentials["address"], credentials["user_id"], credentials["token"],
        jellyfin_id, not currently, **args
    ):
        return  # already notified

    invalidate_want_to_watch_keys()
    added = not currently
    log(f"wtw_toggle: {'added' if added else 'removed'} '{label}' ({media} dbid={dbid}, jellyfin {jellyfin_id})")
    # RunPlugin is completely silent, so without this the button feels broken even when it
    # worked. This is the only feedback the user gets.
    xbmcgui.Dialog().notification(
        ADDON.getAddonInfo("name"),
        f"{'Added to' if added else 'Removed from'} Want to Watch: {label}",
        xbmcgui.NOTIFICATION_INFO,
        5000,
    )
    # The queue widget is now stale wherever it's on screen. Refresh only refreshes the
    # active container, which is usually the list you're standing in rather than the widget,
    # so this is a best-effort nudge, not a guarantee.
    xbmc.executebuiltin("Container.Refresh")


# Verb name -> handler, reached only via ?mode=.
VERB_ROUTES = {
    WTW_TOGGLE_MODE: wtw_toggle,
}


def main() -> None:
    query = dict(parse_qsl(sys.argv[2].lstrip("?"))) if len(sys.argv) > 2 else {}

    # One param per route kind: ?list=<key> browses a directory, ?mode=<verb> fires a
    # fire-and-forget action. Keeping them in separate namespaces is what makes the two
    # structurally un-confusable -- a verb can't be reached with a directory handle and a
    # list can't be fired as a verb, whatever the value happens to say. (An earlier revision
    # used ?key= for lists and folded verbs into the same param; both are gone rather than
    # aliased, so a stale binding fails visibly here instead of half-working forever.)
    menu = query.get("menu")
    if menu is not None:
        render_menu(menu)
        return

    mode = query.get("mode")
    if mode is not None:
        verb = VERB_ROUTES.get(mode)
        if verb is None:
            log(f"Unknown mode: {mode!r}")
            return  # no endOfDirectory: RunPlugin gave us handle -1, there's nothing to close
        verb()
        return

    list_key = query.get("list")

    if not list_key:
        if query:
            # Almost certainly a binding still on the old ?key=/?action= names. Falling back
            # to the root menu renders *something*, which reads as a broken widget rather than
            # an empty one -- so say why in the log, since that's the only clue available.
            log(f"No ?list=, ?menu= or ?mode= in query {query!r} -- falling back to the root menu.")
        render_root_menu()
        return

    if list_key == SEASONAL_AGGREGATE_KEY:
        render_seasonal_aggregate()
        xbmcplugin.endOfDirectory(HANDLE, succeeded=True)
        return

    list_cfg = next((c for c in load_list_configs() if c["key"] == list_key), None)
    if list_cfg is not None:
        render_list(list_cfg)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=True)
        return

    # Not a regular list -- check for a direct pin to one specific seasonal entry. Unlike
    # the "seasonal" aggregate key, this always shows that holiday's content regardless of
    # today's date (ignore_window=True): a hard-set shelf, not one that rotates with the
    # window.
    seasonal_cfg = next((c for c in load_seasonal_configs() if c["key"] == list_key), None)
    if seasonal_cfg is not None:
        render_list(seasonal_cfg, ignore_window=True)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=True)
        return

    log(f"Unknown list: {list_key!r}")
    # A typo'd verb fired via RunPlugin() lands here with handle -1, where endOfDirectory is
    # an invalid call -- only close a directory if we were actually given one to close.
    if HANDLE >= 0:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    main()
