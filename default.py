"""MattFlix Helper -- live MDBList/local-library-powered widgets for Arctic Fuse, resolved
against the local Kodi library via JSON-RPC. No Jellyfin metadata is touched; no external
cron job or file share is required. See resources/lib/config.py for the list definitions
and resources/settings.xml for the user-editable "Custom Lists" override.
"""

import json
import random
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

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
    "genre",
    "rating",
    "runtime",
    "mpaa",
    "director",
    "cast",
    "playcount",
    "lastplayed",
    "resume",
]
TVSHOW_PROPERTIES = [
    "title",
    "imdbnumber",
    "uniqueid",
    "art",
    "plot",
    "year",
    "genre",
    "rating",
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


def load_list_configs() -> list[dict]:
    merged: dict[str, dict] = {}

    for entry in cfg_module.LIST_CONFIGS:
        finalized = cfg_module.finalize_config(entry)
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
    return [cfg_module.finalize_config(entry) for entry in cfg_module.SEASONAL_CONFIGS]


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


def _movie_info(movie: dict) -> dict:
    return {
        "kodi_id": movie["movieid"],
        "title": movie.get("title", ""),
        "art": movie.get("art", {}),
        "plot": movie.get("plot", ""),
        "year": movie.get("year", 0),
        "genre": movie.get("genre", []),
        "rating": movie.get("rating", 0),
        "runtime": movie.get("runtime", 0),
        "mpaa": movie.get("mpaa", ""),
        "director": movie.get("director", []),
        "cast": movie.get("cast", []),
        "playcount": movie.get("playcount", 0),
        "lastplayed": movie.get("lastplayed", ""),
        "resume": movie.get("resume", {}),
    }


def build_movie_provider_index() -> tuple[dict[str, dict], dict[str, dict]]:
    """Index the local movie library by imdb id and tmdb id -> full item info."""
    result = _jsonrpc("VideoLibrary.GetMovies", {"properties": MOVIE_PROPERTIES})
    movies = result.get("movies", [])
    log(f"Local movie library index: {len(movies)} movies found")
    imdb_index: dict[str, dict] = {}
    tmdb_index: dict[str, dict] = {}
    for movie in movies:
        info = _movie_info(movie)
        imdb = (movie.get("imdbnumber") or "").strip().lower()
        if imdb:
            imdb_index[imdb] = info
            if imdb.startswith("tt"):
                imdb_index[imdb[2:]] = info
            else:
                imdb_index[f"tt{imdb}"] = info
        tmdb = movie.get("uniqueid", {}).get("tmdb")
        if tmdb:
            tmdb_index[str(tmdb).strip().lower()] = info
    return imdb_index, tmdb_index


def build_tvshow_provider_index() -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Index the local TV show library by imdb id, tmdb id, and tvdb id -> full item info.

    Unlike movies, a lot of TV libraries are scraped with only a TheTVDB uniqueid set (no
    imdb/tmdb linkage at all) -- imdbnumber can even hold a raw TVDB number in that case,
    not a real "tt..." id. So tvdb has to be a first-class index here, not an afterthought.
    """
    result = _jsonrpc("VideoLibrary.GetTVShows", {"properties": TVSHOW_PROPERTIES})
    shows = result.get("tvshows", [])
    log(f"Local TV show library index: {len(shows)} shows found")
    imdb_index: dict[str, dict] = {}
    tmdb_index: dict[str, dict] = {}
    tvdb_index: dict[str, dict] = {}
    for show in shows:
        info = {
            "kodi_id": show["tvshowid"],
            "title": show.get("title", ""),
            "art": show.get("art", {}),
            "plot": show.get("plot", ""),
            "year": show.get("year", 0),
            "genre": show.get("genre", []),
            "rating": show.get("rating", 0),
            "playcount": show.get("playcount", 0),
            "lastplayed": show.get("lastplayed", ""),
            "episode": show.get("episode", 0),
            "watchedepisodes": show.get("watchedepisodes", 0),
        }

        imdb = (show.get("imdbnumber") or "").strip().lower()
        if imdb:
            imdb_index[imdb] = info
            if imdb.startswith("tt"):
                imdb_index[imdb[2:]] = info
            else:
                imdb_index[f"tt{imdb}"] = info
        tmdb = show.get("uniqueid", {}).get("tmdb")
        if tmdb:
            tmdb_index[str(tmdb).strip().lower()] = info
        tvdb = show.get("uniqueid", {}).get("tvdb")
        if tvdb:
            tvdb_index[str(tvdb).strip().lower()] = info
    return imdb_index, tmdb_index, tvdb_index


def match_mdblist_items(
    raw_items: list[dict], imdb_index: dict, tmdb_index: dict, tvdb_index: dict | None = None
) -> list[dict]:
    """Matches MDBList raw items against a local provider index, in source order."""
    tvdb_index = tvdb_index or {}
    debug = ADDON.getSettingBool("debug_logging")
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


# -----------------------------------------------------------------------------
# Directory builders
# -----------------------------------------------------------------------------
def add_movie_items(matched: list[dict]) -> None:
    for info in matched:
        li = xbmcgui.ListItem(label=info["title"])
        vtag = li.getVideoInfoTag()
        vtag.setTitle(info["title"])
        vtag.setMediaType("movie")
        if info.get("year"):
            vtag.setYear(info["year"])
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

        url = f"videodb://movies/titles/{info['kodi_id']}"
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)


def add_tvshow_items(matched: list[dict]) -> None:
    for info in matched:
        li = xbmcgui.ListItem(label=info["title"])
        vtag = li.getVideoInfoTag()
        vtag.setTitle(info["title"])
        vtag.setMediaType("tvshow")
        if info.get("year"):
            vtag.setYear(info["year"])
        if info.get("plot"):
            vtag.setPlot(info["plot"])
        if info.get("genre"):
            vtag.setGenres(info["genre"])
        if info.get("rating"):
            vtag.setRating(info["rating"])
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
    collection, _kind = cfg_module.parse_jellyfin_url(list_cfg["url"])
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
    )

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
        add_tvshow_items(matched)
    else:
        add_movie_items(matched)


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------
def render_list(list_cfg: dict, ignore_window: bool = False) -> None:
    if not ignore_window and not cfg_module.is_window_active(list_cfg, datetime.now()):
        log(f"'{list_cfg['label']}' has an inactive window right now -- returning empty.")
        add_empty_list(list_cfg)
        return

    limit = int(ADDON.getSettingInt("local_list_item_limit") or 10)
    source = list_cfg["source"]

    if source == "local":
        field, value = cfg_module.parse_local_url(list_cfg["url"])
        matched = query_local_movies_by_filter(field, value, limit)
        add_movie_items(matched)
        log(f"'{list_cfg['label']}': showing {len(matched)} local items (field={field}, value={value})")
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
            matched = random.sample(matched, min(limit, len(matched)))
        elif list_limit is not None:
            matched = matched[:list_limit]
        add_tvshow_items(matched)
    else:
        imdb_index, tmdb_index = build_movie_provider_index()
        matched = match_mdblist_items(raw_items, imdb_index, tmdb_index)
        if is_random_window_entry:
            matched = random.sample(matched, min(limit, len(matched)))
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


def render_root_menu() -> None:
    """A plain browsable menu of every configured list (built-in + user-added), the
    combined seasonal entry, and every individual seasonal entry -- this is what shows up
    when a skin's widget-content-picker browses the addon with no key set, and is handy
    for quick testing directly in the addon browser too. The combined entry's own label
    follows whichever holiday is currently active (e.g. "Christmas" in December), falling
    back to "Seasonals" when none are -- note this only affects browsing the addon's menu
    directly; a skin widget pointed straight at ?list=seasonal shows its own configured
    header text instead, which this addon has no way to override.

    Picking an individual seasonal entry (rather than the combined one) hard-pins it: it
    always shows that holiday's content regardless of today's date, for a shelf you want
    fixed year-round instead of one that rotates in and out with the calendar."""
    active_seasonal = resolve_active_windowed(load_seasonal_configs())
    seasonal_label = active_seasonal["label"] if active_seasonal else SEASONAL_AGGREGATE_LABEL
    li = xbmcgui.ListItem(label=seasonal_label)
    xbmcplugin.addDirectoryItem(HANDLE, f"{BASE_URL}?list={SEASONAL_AGGREGATE_KEY}", li, isFolder=True)

    for list_cfg in load_list_configs():
        li = xbmcgui.ListItem(label=list_cfg["label"])
        url = f"{BASE_URL}?list={list_cfg['key']}"
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    for seasonal_cfg in load_seasonal_configs():
        li = xbmcgui.ListItem(label=seasonal_cfg["label"])
        url = f"{BASE_URL}?list={seasonal_cfg['key']}"
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True)


# -----------------------------------------------------------------------------
# Verb routes (?mode=<verb>)
#
# Invoked via RunPlugin() from a keymap or context menu rather than browsed to, so they get
# handle -1 and must never call into xbmcplugin: there's no directory to build. They just do
# their thing and return.
# -----------------------------------------------------------------------------
WTW_TOGGLE_MODE = "wtw_toggle"


def wtw_toggle() -> None:
    """PLACEHOLDER for the "Want to Watch" toggle -- adds/removes the focused item from the
    Jellyfin-backed queue. Not implemented yet; deliberately a silent no-op so the remote's
    yellow button can be bound (and the keymap deployed) ahead of the backing code.

    Silent is the correct behaviour for the stub specifically: the binding is global, so it
    fires with nothing sensible focused (settings, music, an empty widget) and a toast on
    every stray press would be worse than nothing. Once implemented this DOES need a
    notification on success -- RunPlugin is otherwise completely silent, so a working toggle
    still feels broken without one.
    """
    log("wtw_toggle: 'Want to Watch' is not implemented yet -- ignoring.")


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
            log(f"No ?list= or ?mode= in query {query!r} -- falling back to the root menu.")
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
